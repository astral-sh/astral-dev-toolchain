"""In-memory GitHub responses; tests never contact upstream or write remote refs."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from astral_dev_toolchain._github import GitHub, GitHubError

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
        self.git_calls: list[tuple[str, str]] = []
        self.failures: list[tuple[str, str, str]] = []

    def failure(self, method: str, path: str, when: str) -> None:
        match = (method, path, when)
        if match in self.failures:
            self.failures.remove(match)
            raise GitHubError("injected API failure", 503)

    def request(self, method: str, endpoint: str, *, data: object = None) -> Any:
        self.calls.append((method, endpoint, copy.deepcopy(data)))
        self.failure(method, endpoint, "before")
        result = self.respond(method, endpoint)
        self.failure(method, endpoint, "after")
        return copy.deepcopy(result)

    def _ls_remote(self, repo: str, prefix: str) -> str:
        self.git_calls.append((repo, prefix))
        self.failure("git ls-remote", repo, "before")
        lines = []
        for tag, commit in self.upstreams[repo].items():
            if not tag.startswith(prefix):
                continue
            annotation = self.annotated.get((repo, tag))
            lines.append(f"{annotation or commit}\trefs/tags/{tag}")
            if annotation:
                lines.append(f"{commit}\trefs/tags/{tag}^{{}}")
        self.failure("git ls-remote", repo, "after")
        return "\n".join(lines)

    def respond(self, method: str, endpoint: str) -> Any:
        parsed = urlsplit(endpoint)
        path = unquote(parsed.path)
        for repo, tags in self.upstreams.items():
            prefix = f"/repos/{repo}"
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

        raise AssertionError(f"unexpected API call: {method} {endpoint}")

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[0] != "GET"]
