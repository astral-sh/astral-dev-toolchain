"""In-memory GitHub responses; tests never contact upstream or write remote refs."""

import base64
import copy
import hashlib
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from astral_dev_toolchain._github import GitHub, GitHubError
from astral_dev_toolchain_core import Tool

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40
NOW = datetime(2026, 9, 8, 6, 17, tzinfo=UTC)


def definition(
    root: Path,
    name: str = "example",
    repo: str = "upstream/example",
    *,
    go: bool = False,
) -> Path:
    path = root / "toolchain" / name / "build.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    spec = (
        'backend = "go"\nversion = "1.9.0"\ndirectory = "cmd/example"'
        if go
        else 'backend = "rust"'
    )
    path.write_text(
        f'''version = 1

[tool]
name = "{name}"
{spec}
rebuild = 2 # Reset for the next upstream release.

[source]
repo = "https://github.com/{repo}"
tag = "v1.9.0" # Keep this comment.
commit = "{A}"

[update]
github = true

[[test]]
name = "help"
run = ["example", "--help"]
''',
        encoding="utf-8",
    )
    return path


class MemoryGitHub(GitHub):
    def __init__(self):
        super().__init__(token="unused-test-token")
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/github-tags.json").read_text()
        )
        self.upstreams = {
            "upstream/example": {tag["name"]: tag["commit"]["sha"] for tag in fixture}
        }
        self.annotated: dict[tuple[str, str], str] = {}
        self.tag_objects: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.failures: list[tuple[str, str, str]] = []
        self.repo = "astral-sh/test-toolchain"
        self.refs: dict[str, str] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.prs: dict[int, dict[str, Any]] = {}
        self.events: dict[int, list[dict[str, str]]] = {}

    @staticmethod
    def sha(value: object) -> str:
        return hashlib.sha1(
            json.dumps(value, sort_keys=True).encode(), usedforsecurity=False
        ).hexdigest()

    def tree(self, files: dict[str, str]) -> str:
        sha = self.sha(files)
        self.trees[sha] = files.copy()
        return sha

    def commit(
        self,
        files: dict[str, str],
        parents: list[str],
        message: str = "test",
        *,
        author: str = "human",
        verified: bool = False,
    ) -> str:
        raw = {
            "tree": {"sha": self.tree(files)},
            "parents": parents,
            "message": message,
            "author_login": author,
            "verified": verified,
        }
        sha = self.sha(raw)
        self.commits[sha] = raw
        return sha

    def initialize(self, root: Path) -> str:
        files = {
            path.relative_to(root).as_posix(): path.read_text()
            for path in root.glob("toolchain/*/build.toml")
        }
        files["README.md"] = "Existing project content\n"
        self.refs["main"] = self.commit(files, [])
        return self.refs["main"]

    def files(self, head: str) -> dict[str, str]:
        return self.trees[self.commits[head]["tree"]["sha"]].copy()

    def ancestor(self, old: str, new: str) -> bool:
        if old == new:
            return True
        return any(
            self.ancestor(old, parent) for parent in self.commits[new]["parents"]
        )

    def set_head(self, branch: str, sha: str) -> None:
        self.refs[branch] = sha
        for pr in self.prs.values():
            if pr["state"] == "open" and pr["head"]["ref"] == branch:
                pr["head"]["sha"] = sha

    def observations(self, name: str = "example"):
        text = self.files(self.refs[f"codex/update/{name}"])[
            f"toolchain/{name}/build.toml"
        ]
        tool = Tool.model_validate(tomllib.loads(text))
        assert tool.update is not None
        return tool.update.observed

    def failure(self, method: str, path: str, when: str) -> None:
        match = (method, path, when)
        if match in self.failures:
            self.failures.remove(match)
            raise GitHubError("injected API failure", 503)

    def request(self, method: str, endpoint: str, *, data: object = None) -> Any:
        self.calls.append((method, endpoint, copy.deepcopy(data)))
        self.failure(method, endpoint, "before")
        result = self.respond(method, endpoint, data)
        self.failure(method, endpoint, "after")
        return copy.deepcopy(result)

    def respond(self, method: str, endpoint: str, data: Any) -> Any:
        parsed = urlsplit(endpoint)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        for repo, tags in self.upstreams.items():
            prefix = f"/repos/{repo}"
            if method == "GET" and path == f"{prefix}/tags":
                page = int(query["page"][0])
                return [
                    {"name": name, "commit": {"sha": sha}}
                    for name, sha in list(tags.items())[(page - 1) * 100 : page * 100]
                ]
            if method == "GET" and path.startswith(f"{prefix}/git/ref/tags/"):
                tag = path.removeprefix(f"{prefix}/git/ref/tags/")
                if tag not in tags:
                    raise GitHubError("tag missing", 404)
                annotation = self.annotated.get((repo, tag))
                return {
                    "ref": f"refs/tags/{tag}",
                    "object": {
                        "type": "tag" if annotation else "commit",
                        "sha": annotation or tags[tag],
                    },
                }
            if method == "GET" and path.startswith(f"{prefix}/git/tags/"):
                return {"object": self.tag_objects[path.rsplit("/", 1)[1]]}

        prefix = f"/repos/{self.repo}"
        if path == prefix and method == "GET":
            return {"default_branch": "main"}
        if path.startswith(f"{prefix}/git/ref/heads/") and method == "GET":
            branch = path.removeprefix(f"{prefix}/git/ref/heads/")
            if branch not in self.refs:
                raise GitHubError("ref missing", 404)
            return {
                "ref": f"refs/heads/{branch}",
                "object": {"sha": self.refs[branch], "type": "commit"},
            }
        if path == f"{prefix}/git/refs" and method == "POST":
            branch = data["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                raise GitHubError("ref exists", 422)
            self.set_head(branch, data["sha"])
            return {"object": {"sha": data["sha"]}}
        if path == "/graphql" and method == "POST":
            values = data["variables"]["input"]
            assert values["branch"]["repositoryNameWithOwner"] == self.repo
            branch = values["branch"]["refName"]
            expected = values["expectedHeadOid"]
            if self.refs[branch] != expected:
                return {"errors": [{"message": "Head moved"}]}
            files = self.files(expected)
            for addition in values["fileChanges"]["additions"]:
                files[addition["path"]] = base64.b64decode(
                    addition["contents"]
                ).decode()
            head = self.commit(
                files,
                [expected],
                values["message"]["headline"],
                author="github-actions[bot]",
                verified=True,
            )
            self.set_head(branch, head)
            return {"data": {"createCommitOnBranch": {"commit": {"oid": head}}}}
        if path.startswith(f"{prefix}/commits/") and method == "GET":
            raw = self.commits[path.rsplit("/", 1)[1]]
            return {
                "author": {"login": raw["author_login"]},
                "committer": {
                    "login": "web-flow" if raw["verified"] else raw["author_login"]
                },
                "commit": {
                    "message": raw["message"],
                    "verification": {"verified": raw["verified"]},
                },
            }
        if path.startswith(f"{prefix}/compare/") and method == "GET":
            base, head = path.rsplit("/", 1)[1].split("...")
            status = (
                "identical"
                if base == head
                else "ahead"
                if self.ancestor(base, head)
                else "behind"
                if self.ancestor(head, base)
                else "diverged"
            )
            return {"status": status}
        if path.startswith(f"{prefix}/contents/") and method == "GET":
            content = self.files(query["ref"][0])[
                path.removeprefix(f"{prefix}/contents/")
            ]
            return {
                "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode(),
            }
        if path == f"{prefix}/pulls" and method == "GET":
            branch = query["head"][0].split(":", 1)[1]
            return [
                pr
                for pr in reversed(self.prs.values())
                if pr["head"]["ref"] == branch and pr["base"]["ref"] == query["base"][0]
            ]
        if path == f"{prefix}/pulls" and method == "POST":
            number = len(self.prs) + 1
            pr = {
                "number": number,
                "title": data["title"],
                "body": data["body"],
                "draft": data["draft"],
                "state": "open",
                "merged_at": None,
                "user": {"login": "github-actions[bot]"},
                "base": {"ref": data["base"], "sha": self.refs[data["base"]]},
                "head": {
                    "ref": data["head"],
                    "sha": self.refs[data["head"]],
                    "repo": {"full_name": self.repo},
                },
            }
            self.prs[number] = pr
            self.events[number] = []
            return pr
        if path.startswith(f"{prefix}/pulls/"):
            if path.endswith("/files") and method == "GET":
                pr = self.prs[int(path.split("/")[-2])]
                base = self.files(pr["base"]["sha"])
                head = self.files(pr["head"]["sha"])
                return [
                    {"filename": path}
                    for path in base.keys() | head.keys()
                    if base.get(path) != head.get(path)
                ]
            pr = self.prs[int(path.rsplit("/", 1)[1])]
            if method == "GET":
                return pr
            if method == "PATCH":
                pr.update(data)
                return pr
        if (
            path.startswith(f"{prefix}/issues/")
            and path.endswith("/timeline")
            and method == "GET"
        ):
            return self.events[int(path.split("/")[-2])]
        raise AssertionError(f"unexpected API call: {method} {endpoint}")

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[0] != "GET"]
