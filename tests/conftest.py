from functools import partial
from pathlib import Path

import pytest
from helpers import NOW, GitRepo, definition

from astral_dev_toolchain import _update


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
def update(tmp_path):
    return partial(_update.update_local, tmp_path / "toolchain", now=NOW)


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
