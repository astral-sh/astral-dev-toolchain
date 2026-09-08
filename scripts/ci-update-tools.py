"""Keep update observations in per-tool branches and open eligible draft PRs."""

import argparse
import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from astral_dev_toolchain._github import GitHub, GitHubError
from astral_dev_toolchain._update import (
    Definition,
    Discovery,
    Scan,
    definitions,
    recheck,
    scan,
    tag_version,
)
from packaging.version import Version

BOT = "github-actions[bot]"
_COMMIT = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid } }
}
"""


def branch_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError(f"tool name cannot be used in an update branch: {name}")
    return f"codex/update/{name}"


def ref_head(github: GitHub, repo: str, branch: str) -> str | None:
    try:
        raw = github.request(
            "GET", f"/repos/{repo}/git/ref/heads/{quote(branch, safe='')}"
        )
    except GitHubError as error:
        if error.status == 404:
            return None
        raise
    if raw["ref"] != f"refs/heads/{branch}" or raw["object"]["type"] != "commit":
        raise ValueError(f"unexpected branch reference: {branch}")
    return raw["object"]["sha"]


@dataclass
class UpdateBranch:
    head: str | None
    definition: Definition | None
    pr: dict[str, Any] | None
    ignored: Version | None
    frozen: bool


@dataclass
class PullRequests:
    github: GitHub
    repo: str
    base_branch: str
    base_head: str
    root: Path

    def read_definition(self, definition: Definition, head: str) -> Definition:
        path = definition.path.resolve().relative_to(self.root).as_posix()
        raw = self.github.request(
            "GET", f"/repos/{self.repo}/contents/{quote(path, safe='/')}?ref={head}"
        )
        if raw["encoding"] != "base64":
            raise ValueError(f"{path}: expected base64 content")
        text = base64.b64decode(raw["content"].replace("\n", ""), validate=True).decode(
            "utf-8"
        )
        loaded = Definition.parse(definition.path, text)
        if loaded.tool.tool.name != definition.tool.tool.name:
            raise ValueError(f"{path}: unexpected tool on update branch")
        return loaded

    def closed_version(
        self, pr: dict[str, Any], definition: Definition
    ) -> Version | None:
        marker = re.search(
            r"<!-- astral-dev-toolchain-update: (.*?) -->", pr.get("body") or ""
        )
        if marker:
            try:
                value = json.loads(marker[1])
                if (
                    value["tool"] == definition.tool.tool.name
                    and isinstance(value["version"], str)
                    and (version := tag_version(value["version"])) is not None
                ):
                    return version
            except ValueError, KeyError, TypeError:
                pass
        # Human edits to the description do not undo a rejected version.
        closed = self.read_definition(definition, pr["head"]["sha"])
        assert definition.tool.update is not None
        return tag_version(closed.tool.source.tag, definition.tool.update.tag_prefix)

    def handed_off(self, pr: dict[str, Any], head: str | None) -> bool:
        if (
            pr["state"] != "open"
            or not pr["draft"]
            or pr["user"]["login"] != BOT
            or pr["head"]["sha"] != head
        ):
            return True
        return any(
            event["event"] == "ready_for_review"
            for event in self.github.paginate(
                f"/repos/{self.repo}/issues/{pr['number']}/timeline"
            )
        )

    def load(self, definition: Definition) -> UpdateBranch:
        name = definition.tool.tool.name
        branch = branch_name(name)
        head = ref_head(self.github, self.repo, branch)
        query = urlencode(
            {
                "state": "all",
                "head": f"{self.repo.split('/')[0]}:{branch}",
                "base": self.base_branch,
                "sort": "created",
                "direction": "desc",
            }
        )
        prs = list(self.github.paginate(f"/repos/{self.repo}/pulls?{query}"))
        if any(
            pr["head"]["ref"] != branch
            or pr["head"]["repo"] is None
            or pr["head"]["repo"]["full_name"].lower() != self.repo.lower()
            for pr in prs
        ):
            raise ValueError(f"{name}: unexpected PR head repository or branch")
        opened = [pr for pr in prs if pr["state"] == "open"]
        if len(opened) > 1:
            raise ValueError(f"{name}: multiple open update PRs")
        pr = opened[0] if opened else None
        rejected = [
            version
            for closed in prs
            if closed["state"] == "closed"
            and not closed.get("merged_at")
            and closed["user"]["login"] == BOT
            if (version := self.closed_version(closed, definition)) is not None
        ]
        frozen = pr is not None and self.handed_off(pr, head)
        if head is not None and not frozen:
            commit = self.github.request("GET", f"/repos/{self.repo}/commits/{head}")
            owned = (
                (commit.get("author") or {}).get("login") == BOT
                and (commit.get("committer") or {}).get("login") in {BOT, "web-flow"}
                and commit["commit"]["verification"]["verified"]
                and commit["commit"]["message"].splitlines()[0]
                == f"toolchain: update {name}"
            )
            if not owned:
                # A resolved PR may be reused; an interrupted first run may also
                # have created a branch that still points into main's history.
                resolved = False
                if pr is None:
                    path = definition.path.resolve().relative_to(self.root).as_posix()
                    closed = next(
                        (
                            p
                            for p in prs
                            if p["state"] == "closed" and p["head"]["sha"] == head
                        ),
                        None,
                    )
                    if closed is not None:
                        # Do not carry unrelated human changes into the next PR.
                        resolved = all(
                            change["filename"] == path
                            for change in self.github.paginate(
                                f"/repos/{self.repo}/pulls/{closed['number']}/files"
                            )
                        )
                if pr is None and not resolved:
                    comparison = self.github.request(
                        "GET", f"/repos/{self.repo}/compare/{head}...{self.base_head}"
                    )
                    resolved = comparison["status"] in {"ahead", "identical"}
                frozen = not resolved
        loaded = (
            self.read_definition(definition, head)
            if head is not None and not frozen
            else None
        )
        return UpdateBranch(head, loaded, pr, max(rejected, default=None), frozen)

    def writable(self, branch: UpdateBranch) -> bool:
        if branch.pr is None:
            return True
        current = self.github.request(
            "GET", f"/repos/{self.repo}/pulls/{branch.pr['number']}"
        )
        return not self.handed_off(current, branch.head)

    def save(self, discovery: Discovery, branch: UpdateBranch, content: str) -> None:
        if ref_head(self.github, self.repo, self.base_branch) != self.base_head:
            raise ValueError(
                "default branch changed during the run; retry against its new head"
            )
        name = branch_name(discovery.name)
        if branch.head is None:
            self.github.request(
                "POST",
                f"/repos/{self.repo}/git/refs",
                data={"ref": f"refs/heads/{name}", "sha": self.base_head},
            )
            branch.head = self.base_head
        path = discovery.definition.path.resolve().relative_to(self.root).as_posix()
        # GitHub signs the commit and checks the expected head atomically. A
        # later run can recover from an uncertain response by reading this file.
        response = self.github.request(
            "POST",
            "/graphql",
            data={
                "query": _COMMIT,
                "variables": {
                    "input": {
                        "branch": {
                            "repositoryNameWithOwner": self.repo,
                            "refName": name,
                        },
                        "expectedHeadOid": branch.head,
                        "message": {"headline": f"toolchain: update {discovery.name}"},
                        "fileChanges": {
                            "additions": [
                                {
                                    "path": path,
                                    "contents": base64.b64encode(
                                        content.encode()
                                    ).decode(),
                                }
                            ]
                        },
                    }
                },
            },
        )
        if response.get("errors"):
            raise GitHubError(f"could not update {name}: {response['errors']}")
        branch.head = response["data"]["createCommitOnBranch"]["commit"]["oid"]

    def publish(self, discovery: Discovery, branch: UpdateBranch) -> str:
        proposal = discovery.proposal
        assert proposal is not None
        old = discovery.definition.tool.source
        title = f"toolchain: update {discovery.name} to {proposal.version}"
        marker = json.dumps(
            {"tool": discovery.name, "version": str(proposal.version)}, sort_keys=True
        )
        body = (
            f"Update **{discovery.name}** from `{proposal.old_version}` to `{proposal.version}`.\n\n"
            f"| | Current | Proposed |\n| --- | --- | --- |\n"
            f"| Tag | `{old.tag}` | `{proposal.tag}` |\n"
            f"| Commit | `{old.commit}` | `{proposal.commit}` |\n\n"
            f"First observed: {proposal.first_seen.isoformat()}  \n"
            f"Eligible after seven days: {proposal.eligible_at.isoformat()}\n\n"
            f"[Compare upstream changes](https://github.com/{discovery.repo}/compare/{old.commit}...{proposal.commit}).\n\n"
            "Mark this PR **ready for review** to start CI, including tool builds and smoke tests. "
            "The updater refreshes untouched drafts and stops after review begins or a human pushes commits. "
            "Closing without merging suppresses this version and older versions.\n\n"
            f"<!-- astral-dev-toolchain-update: {marker} -->\n"
        )
        if branch.pr is None:
            created = self.github.request(
                "POST",
                f"/repos/{self.repo}/pulls",
                data={
                    "title": title,
                    "body": body,
                    "head": branch_name(discovery.name),
                    "base": self.base_branch,
                    "draft": True,
                },
            )
            return f"{discovery.name}: created draft PR #{created['number']}"
        if branch.pr["title"] != title or branch.pr["body"] != body:
            self.github.request(
                "PATCH",
                f"/repos/{self.repo}/pulls/{branch.pr['number']}",
                data={"title": title, "body": body},
            )
            return f"{discovery.name}: refreshed draft PR #{branch.pr['number']}"
        return f"{discovery.name}: draft PR #{branch.pr['number']} already current"


def run_updates(
    github: GitHub,
    repo: str,
    root: Path,
    base_head: str,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> Scan:
    info = github.request("GET", f"/repos/{repo}")
    base_branch = info["default_branch"]
    if ref_head(github, repo, base_branch) != base_head:
        raise ValueError("checkout must match the default branch head")
    manager = PullRequests(github, repo, base_branch, base_head, root.resolve())
    branches, selected = {}, []
    result = Scan()
    for definition in definitions(root / "toolchain"):
        name = definition.tool.tool.name
        if definition.tool.update is None or not definition.tool.update.github:
            result.messages.append(f"{name}: automatic updates disabled")
            continue
        try:
            branch = manager.load(definition)
            if branch.frozen:
                result.messages.append(
                    f"{name}: PR or branch frozen for human review or edits"
                )
                continue
            branches[name] = branch
            selected.append(definition)
        except (GitHubError, ValueError) as error:
            result.errors.append(f"{name}: {error}")
    scanned = scan(
        selected,
        github,
        now,
        previous={
            name: branch.definition.tool
            for name, branch in branches.items()
            if branch.definition is not None
        },
    )
    result.discoveries = scanned.discoveries
    result.messages.extend(scanned.messages)
    result.errors.extend(scanned.errors)
    for discovery in result.discoveries:
        branch = branches[discovery.name]
        try:
            proposal = discovery.proposal
            if (
                proposal
                and branch.ignored is not None
                and proposal.version <= branch.ignored
            ):
                result.messages.append(
                    f"{discovery.name}: {proposal.version} suppressed by a closed PR"
                )
                discovery.proposal = None
            if (
                discovery.proposal is not None
                and branch.pr is not None
                and branch.definition is not None
            ):
                assert discovery.definition.tool.update is not None
                current = tag_version(
                    branch.definition.tool.source.tag,
                    discovery.definition.tool.update.tag_prefix,
                )
                if current is None:
                    raise ValueError("open PR does not pin a stable version")
                if discovery.proposal.version < current:
                    result.messages.append(
                        f"{discovery.name}: keeping newer open proposal {current}"
                    )
                    discovery.proposal = None
            try:
                recheck(discovery, github, now)
            except (GitHubError, ValueError) as error:
                discovery.proposal = None
                result.errors.append(f"{discovery.name}: {error}")
            keep = (
                branch.definition
                if branch.pr is not None and discovery.proposal is None
                else None
            )
            content = discovery.render(keep=keep)
            old = (
                branch.definition.text
                if branch.definition
                else discovery.definition.text
            )
            if dry_run:
                if discovery.proposal:
                    result.messages.append(
                        f"{discovery.name}: would propose {discovery.proposal.version}"
                    )
                elif content != old:
                    result.messages.append(
                        f"{discovery.name}: would record observations in {branch_name(discovery.name)}"
                    )
                continue
            if not manager.writable(branch):
                result.messages.append(
                    f"{discovery.name}: PR changed; waiting for reconciliation"
                )
                continue
            if content != old:
                manager.save(discovery, branch, content)
                result.messages.append(
                    f"{discovery.name}: recorded build.toml on {branch_name(discovery.name)}"
                )
            if discovery.proposal is not None and manager.writable(branch):
                result.messages.append(manager.publish(discovery, branch))
        except (GitHubError, ValueError) as error:
            result.errors.append(f"{discovery.name}: {error}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    messages, errors = [], []
    try:
        repo = os.environ["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("invalid GITHUB_REPOSITORY")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        result = run_updates(GitHub(), repo, Path.cwd(), head, dry_run=args.dry_run)
        messages, errors = result.messages, result.errors
    except (GitHubError, ValueError, OSError) as error:
        errors.append(str(error))
    summary = "## Tool updates\n\n" + "\n".join(f"- {message}" for message in messages)
    if errors:
        summary += "\n\n### Failures\n\n" + "\n".join(f"- {error}" for error in errors)
    print(summary)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(summary + "\n")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
