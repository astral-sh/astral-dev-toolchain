"""Local Git repositories and tool definitions for updater tests."""

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

NOW = datetime(2026, 9, 8, 6, 17, tzinfo=UTC)


@dataclass
class GitRepo:
    path: Path
    commits: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return self.path.as_uri()

    def run(self, *args: str, input: str | None = None) -> str:
        env = os.environ | {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
        return subprocess.run(
            ["git", "-C", str(self.path), "-c", "tag.gpgSign=false", *args],
            input=input,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    def tag(self, name: str, target: str, *, message: str | None = None) -> None:
        args = ["tag", "--force"]
        if message is not None:
            args.extend(["--annotate", "--message", message])
        self.run(*args, name, target)

    def delete_tag(self, name: str) -> None:
        self.run("tag", "--delete", name)


def definition(
    root: Path,
    name: str = "example",
    *,
    repo: str,
    commit: str,
    go: bool = False,
    prefix: str = "",
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
tag = "{prefix}v1.9.0" # Keep this comment.
commit = "{commit}"

[update]
enabled = true
tag-prefix = "{prefix}"

[[test]]
name = "help"
run = ["example", "--help"]
''',
        encoding="utf-8",
    )
    return path
