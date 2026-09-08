"""Local Git repositories and tool definitions for updater tests."""

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest
import tomlkit

from astral_dev_toolchain import _update


@pytest.fixture
def now():
    return datetime(2026, 9, 8, 6, 17, tzinfo=UTC)


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
    spec = {
        "name": name,
        "backend": "go" if go else "rust",
        "rebuild": tomlkit.integer(2).comment("Reset for the next upstream release."),
    }
    if go:
        spec.update(version="1.9.0", directory="cmd/example")
    path.write_text(
        tomlkit.dumps(
            {
                "version": 1,
                "tool": spec,
                "source": {
                    "repo": repo,
                    "tag": tomlkit.string(f"{prefix}v1.9.0").comment(
                        "Keep this comment."
                    ),
                    "commit": commit,
                },
                "update": {"enabled": True, "tag-prefix": prefix},
                "test": [{"name": "help", "run": ["example", "--help"]}],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def repo_factory(tmp_path: Path):
    # Each test can move/delete tags or make its repository unavailable.
    def create(name: str = "Upstream Repository.git", *, object_format: str = "sha1"):
        path = tmp_path / name
        path.mkdir()
        repo = GitRepo(path)
        repo.run(
            "-c",
            "init.templateDir=",
            "init",
            "--bare",
            f"--object-format={object_format}",
        )
        tree = repo.run("mktree", input="")
        commits = []
        for index in range(4):
            parents = ["-p", commits[-1]] if commits else []
            commits.append(
                repo.run("commit-tree", tree, *parents, "-m", f"Commit {index}")
            )
        repo.commits = tuple(commits)
        for tag, commit in {
            "v1.9.0": commits[0],
            "v1.10.0": commits[1],
            "v2.0.0": commits[2],
            "v3.0.0-rc.1": commits[3],
            "another-tool/v99.0.0": commits[3],
            "nightly": commits[3],
        }.items():
            repo.tag(tag, commit)
        return repo

    return create


@pytest.fixture
def upstream(repo_factory):
    return repo_factory()


@pytest.fixture
def tool_factory(tmp_path, upstream):
    def create(name="example", *, repo=None, go=False, prefix=""):
        repo = repo or upstream
        return definition(
            tmp_path, name, repo=repo.url, commit=repo.commits[0], go=go, prefix=prefix
        )

    return create


@pytest.fixture
def tool_file(tool_factory):
    return tool_factory()


@pytest.fixture
def update(tmp_path, now):
    return partial(_update.update_local, tmp_path / "toolchain", now=now)


@pytest.fixture
def observed_tool(tool_file, update):
    assert not update().errors
    return tool_file


@pytest.fixture
def before_recheck(monkeypatch):
    recheck = _update.recheck

    def install(action):
        def checking(*args, **kwargs):
            action()
            return recheck(*args, **kwargs)

        monkeypatch.setattr(_update, "recheck", checking)

    return install
