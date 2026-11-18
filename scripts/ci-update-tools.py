"""Keep update observations in per-tool branches and open eligible PRs."""

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
from uuid import uuid4

from astral_dev_toolchain._git import Git, GitError
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

# Honor observations and rejections from the previous GITHUB_TOKEN updater.
_BOTS = {"astral-automations-bot[bot]", "github-actions[bot]"}
# Historical bot-closed PRs must not be counted as rejected updates.
_SUPERSEDED = "<!-- astral-dev-toolchain-superseded -->"
_COMMIT = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid } }
}
"""
_UPDATE_REFS = """
mutation($input: UpdateRefsInput!) {
  updateRefs(input: $input) { clientMutationId }
}
"""
_NULL_OID = "0" * 40  # GitHub's updateRefs sentinel for a missing or deleted ref.


def gh_api(
    method: str, endpoint: str, *, data: object = None, paginate: bool = False
) -> Any:
    command = ["gh", "api", endpoint.lstrip("/"), "--method", method]
    if data is not None:
        command.extend(["--input", "-"])
    if paginate:
        command.extend(["--paginate", "--slurp"])
    try:
        result = subprocess.run(
            command,
            input=json.dumps(data) if data is not None else None,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"gh api {method} {endpoint}: {error.stderr.strip()}"
        ) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"gh api {method} {endpoint}: {error}") from error
    return json.loads(result.stdout) if result.stdout else None


def gh_pages(endpoint: str) -> list[dict[str, Any]]:
    separator = "&" if "?" in endpoint else "?"
    pages = gh_api("GET", f"{endpoint}{separator}per_page=100", paginate=True)
    if not all(
        isinstance(page, list) and all(isinstance(entry, dict) for entry in page)
        for page in pages
    ):
        raise ValueError(f"expected lists from {endpoint}")
    return [entry for page in pages for entry in page]


def branch_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError(f"tool name cannot be used in an update branch: {name}")
    return f"update/{name}"


def ref_head(repo: str, branch: str) -> str | None:
    refs = gh_api(
        "GET", f"/repos/{repo}/git/matching-refs/heads/{quote(branch, safe='')}"
    )
    for ref in refs:
        if ref["ref"] == f"refs/heads/{branch}":
            if ref["object"]["type"] != "commit":
                raise ValueError(f"unexpected branch reference: {branch}")
            return ref["object"]["sha"]
    return None


@dataclass
class UpdateBranch:
    name: str
    head: str | None = None
    definition: Definition | None = None
    ignored: Version | None = None
    frozen: bool = False


@dataclass
class PullRequests:
    repo: str
    repo_id: str
    base_branch: str
    base_head: str
    root: Path

    def read_definition(self, definition: Definition, head: str) -> Definition:
        path = definition.path.resolve().relative_to(self.root).as_posix()
        raw = gh_api(
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
        if _SUPERSEDED in (pr.get("body") or ""):
            return None
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
        return tag_version(closed.tool.source.tag, definition.tool.update.tag_prefix)

    def load(self, definition: Definition) -> UpdateBranch:
        name = definition.tool.tool.name
        branch = branch_name(name)
        head = ref_head(self.repo, branch)
        query = urlencode(
            {
                "state": "all",
                "head": f"{self.repo.split('/')[0]}:{branch}",
                "base": self.base_branch,
                "sort": "created",
                "direction": "desc",
            }
        )
        prs = gh_pages(f"/repos/{self.repo}/pulls?{query}")
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
        if opened:
            return UpdateBranch(branch, head, frozen=True)
        rejected = [
            version
            for closed in prs
            if closed["state"] == "closed"
            and not closed.get("merged_at")
            and closed["user"]["login"] in _BOTS
            if (version := self.closed_version(closed, definition)) is not None
        ]
        frozen = False
        if head is not None:
            commit = gh_api("GET", f"/repos/{self.repo}/commits/{head}")
            author = (commit.get("author") or {}).get("login")
            owned = (
                author in _BOTS
                and (commit.get("committer") or {}).get("login") in {author, "web-flow"}
                and commit["commit"]["verification"]["verified"]
                and commit["commit"]["message"].splitlines()[0]
                == f"toolchain: update {name}"
            )
            if not owned:
                # A resolved PR may be reused; an interrupted first run may also
                # have created a branch that still points into main's history.
                resolved = False
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
                        for change in gh_pages(
                            f"/repos/{self.repo}/pulls/{closed['number']}/files"
                        )
                    )
                if not resolved:
                    comparison = gh_api(
                        "GET", f"/repos/{self.repo}/compare/{head}...{self.base_head}"
                    )
                    resolved = comparison["status"] in {"ahead", "identical"}
                frozen = not resolved
        loaded = (
            self.read_definition(definition, head)
            if head is not None and not frozen
            else None
        )
        return UpdateBranch(branch, head, loaded, max(rejected, default=None), frozen)

    def check_base_head(self) -> None:
        if ref_head(self.repo, self.base_branch) != self.base_head:
            raise ValueError(
                "default branch changed during the run; retry against its new head"
            )

    def save(self, discovery: Discovery, branch: UpdateBranch, content: str) -> None:
        self.check_base_head()
        if branch.head is None:
            gh_api(
                "POST",
                f"/repos/{self.repo}/git/refs",
                data={"ref": f"refs/heads/{branch.name}", "sha": self.base_head},
            )
            branch.head = self.base_head
        path = discovery.definition.path.resolve().relative_to(self.root).as_posix()
        # GitHub signs the commit and checks the expected head atomically.
        response = gh_api(
            "POST",
            "/graphql",
            data={
                "query": _COMMIT,
                "variables": {
                    "input": {
                        "branch": {
                            "repositoryNameWithOwner": self.repo,
                            "branchName": branch.name,
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
        branch.head = response["data"]["createCommitOnBranch"]["commit"]["oid"]

    def force_update_refs(self, *changes: tuple[UpdateBranch, str]) -> None:
        """Update refs atomically only if each expected head still matches."""
        updates = [
            {
                "name": f"refs/heads/{branch.name}",
                "beforeOid": branch.head or _NULL_OID,
                "afterOid": target,
                "force": True,
            }
            for branch, target in changes
        ]
        gh_api(
            "POST",
            "/graphql",
            data={
                "query": _UPDATE_REFS,
                "variables": {
                    "input": {"repositoryId": self.repo_id, "refUpdates": updates}
                },
            },
        )

    def prepare(self, discovery: Discovery, branch: UpdateBranch, content: str) -> None:
        # Keep the observation branch intact until a signed commit is ready;
        # resetting it first could lose cooldown timestamps if the run fails.
        staging = UpdateBranch(f"update-staging/{discovery.name}-{uuid4().hex}")
        delete_staging = (staging, _NULL_OID)
        try:
            self.save(discovery, staging, content)
            assert staging.head is not None
            self.force_update_refs((branch, staging.head), delete_staging)
        except Exception as error:
            if staging.head is not None:
                try:
                    self.force_update_refs(delete_staging)
                except RuntimeError as cleanup_error:
                    raise RuntimeError(
                        f"{error}; failed to clean up {staging.name}: {cleanup_error}"
                    ) from error
            raise
        branch.head = staging.head

    def publish(self, discovery: Discovery, branch: UpdateBranch, content: str) -> str:
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
            f"Upstream repository: `{discovery.repo}`.\n\n"
            "Apply the `publish-after-merge` label before merging to publish the updated tool automatically. "
            "The updater leaves this PR unchanged until it is merged or closed. "
            "Manually closing without merging suppresses this version and older versions.\n\n"
            f"<!-- astral-dev-toolchain-update: {marker} -->\n"
        )
        self.prepare(discovery, branch, content)
        self.check_base_head()
        created = gh_api(
            "POST",
            f"/repos/{self.repo}/pulls",
            data={
                "title": title,
                "body": body,
                "head": branch.name,
                "base": self.base_branch,
                "draft": False,
            },
        )
        return f"{discovery.name}: created PR #{created['number']}"


def run_updates(
    repo: str,
    root: Path,
    base_head: str,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> Scan:
    git = Git()
    info = gh_api("GET", f"/repos/{repo}")
    base_branch = info["default_branch"]
    if ref_head(repo, base_branch) != base_head:
        raise ValueError("checkout must match the default branch head")
    manager = PullRequests(
        repo, info["node_id"], base_branch, base_head, root.resolve()
    )
    branches, selected = {}, []
    result = Scan()
    for definition in definitions(root / "toolchain"):
        name = definition.tool.tool.name
        if not definition.tool.update.enabled:
            result.messages.append(f"{name}: automatic updates disabled")
            continue
        try:
            branch = manager.load(definition)
            if branch.frozen:
                result.messages.append(
                    f"{name}: open update PR or branch with human edits; skipping"
                )
                continue
            branches[name] = branch
            selected.append(definition)
        except (RuntimeError, ValueError) as error:
            result.errors.append(f"{name}: {error}")
    scanned = scan(
        selected,
        git,
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
            try:
                recheck(discovery, git, now)
            except (GitError, ValueError) as error:
                discovery.proposal = None
                result.errors.append(f"{discovery.name}: {error}")
            content = discovery.render()
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
                        f"{discovery.name}: would record observations in {branch.name}"
                    )
                continue
            if discovery.proposal is not None:
                result.messages.append(manager.publish(discovery, branch, content))
            elif content != old:
                manager.save(discovery, branch, content)
                result.messages.append(
                    f"{discovery.name}: recorded build.toml on {branch.name}"
                )
        except (RuntimeError, ValueError) as error:
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
        result = run_updates(repo, Path.cwd(), head, dry_run=args.dry_run)
        messages, errors = result.messages, result.errors
    except (RuntimeError, ValueError, OSError) as error:
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
