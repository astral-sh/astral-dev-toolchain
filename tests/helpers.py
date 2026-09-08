"""In-memory Git tag advertisements; tests never contact upstream."""

import json
from collections.abc import Collection
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path

from astral_dev_toolchain._git import Git, GitError

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40
NOW = datetime(2026, 9, 8, 6, 17, tzinfo=UTC)
REMOTE = "https://example.test/upstream/example.git"


def definition(
    root: Path,
    name: str = "example",
    repo: str = REMOTE,
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
repo = "{repo}"
tag = "v1.9.0" # Keep this comment.
commit = "{A}"

[update]
enabled = true

[[test]]
name = "help"
run = ["example", "--help"]
''',
        encoding="utf-8",
    )
    return path


class MemoryGit(Git):
    def __init__(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/tags.json").read_text())
        self.upstreams = {
            REMOTE: {tag["name"]: tag["commit"]["sha"] for tag in fixture}
        }
        self.annotated: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.failures: list[str] = []

    def _ls_remote(self, repo: str, patterns: Collection[str]) -> str:
        self.calls.append((repo, tuple(patterns)))
        if repo in self.failures:
            self.failures.remove(repo)
            raise GitError("injected Git failure")
        lines = []
        for tag, commit in self.upstreams[repo].items():
            annotation = self.annotated.get((repo, tag))
            refs = {f"refs/tags/{tag}": annotation or commit}
            if annotation:
                refs[f"refs/tags/{tag}^{{}}"] = commit
            for ref, target in refs.items():
                if not patterns or any(
                    fnmatchcase(ref, pattern) for pattern in patterns
                ):
                    lines.append(f"{target}\t{ref}")
        return "\n".join(lines)
