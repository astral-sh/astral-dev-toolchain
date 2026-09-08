import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from astral_dev_toolchain._git import Git
from astral_dev_toolchain._update import COOLDOWN, tag_version, update_local
from astral_dev_toolchain_core import Tool
from helpers import NOW, A, definition


@pytest.fixture(params=["sha1", "sha256"])
def bare_repo(tmp_path: Path, request):
    path = tmp_path / "Upstream Repository.git"
    subprocess.run(
        ["git", "init", "--bare", f"--object-format={request.param}", str(path)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    env = os.environ | {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }

    def run(*args, input=None):
        return subprocess.run(
            ["git", "-c", "tag.gpgSign=false", "-C", str(path), *args],
            input=input,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    tree = run("mktree", input="")
    first = run("commit-tree", tree, "-m", "First commit")
    second = run("commit-tree", tree, "-p", first, "-m", "Second commit")
    run("tag", "v1.9.0", first)
    run("tag", "v1.10.0", first)
    run("tag", "-a", "inner", second, "-m", "Inner annotation")
    run("tag", "-a", "v2.0.0", "inner", "-m", "Outer annotation")
    run("tag", "v99.0.0-rc.1", second)
    run("tag", "project/v3.0.0", second)
    return path, run, first, second


def test_remote_tag_listing_and_exact_rechecks(bare_repo):
    path, _, first, second = bare_repo
    git = Git()
    assert git.tags(
        path.as_uri(), include=lambda tag: tag_version(tag) is not None
    ) == {
        "v1.9.0": first,
        "v1.10.0": first,
        "v2.0.0": second,
    }
    assert git.tags(str(path), "project/") == {"project/v3.0.0": second}
    assert git.resolve_tags(path.as_uri(), ("v1.9.0", "v2.0.0", "v2.0")) == {
        "v1.9.0": first,
        "v2.0.0": second,
    }


def test_observation_time_controls_cooldown_without_a_clone(
    bare_repo, tmp_path, monkeypatch
):
    remote, _, first, second = bare_repo
    workspace = tmp_path / "workspace"
    path = definition(workspace, repo=remote.as_uri())
    path.write_text(path.read_text().replace(A, first))
    run = subprocess.run
    commands = []

    def read_tags(command, **kwargs):
        assert command[:5] == ["git", "ls-remote", "--tags", "--sort=v:refname", "--"]
        commands.append(command)
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", read_tags)
    result = update_local(workspace / "toolchain", now=NOW)
    assert not result.errors
    assert not any(d.proposal for d in result.discoveries)
    tool = Tool.load(path)
    assert tool.update is not None
    assert tool.update.observed["v2.0.0"].first_seen == NOW
    assert tool.update.observed["v2.0.0"].commit == second

    result = update_local(
        workspace / "toolchain", now=NOW + COOLDOWN - timedelta(microseconds=1)
    )
    assert not result.errors
    assert not any(d.proposal for d in result.discoveries)
    result = update_local(workspace / "toolchain", now=NOW + COOLDOWN)
    assert not result.errors
    assert Tool.load(path).source.tag == "v2.0.0"
    assert Tool.load(path).source.commit == second
    assert len(commands) == 4  # Three discoveries and one check before writing.
    assert not list(workspace.rglob(".git"))


def test_reannotating_a_tag_preserves_observation_until_its_target_moves(
    bare_repo, tmp_path
):
    remote, run, first, second = bare_repo
    workspace = tmp_path / "workspace"
    path = definition(workspace, repo=str(remote))
    path.write_text(path.read_text().replace(A, first))
    assert not update_local(workspace / "toolchain", now=NOW).errors
    run("tag", "-f", "-a", "v2.0.0", second, "-m", "Different annotation")
    result = update_local(workspace / "toolchain", now=NOW + timedelta(days=1))
    assert not result.errors
    assert result.discoveries[0].observed["v2.0.0"].first_seen == NOW

    run("tag", "-f", "-a", "v2.0.0", first, "-m", "Changed target")
    result = update_local(workspace / "toolchain", now=NOW + timedelta(days=2))
    assert not result.errors
    observed = result.discoveries[0].observed["v2.0.0"]
    assert (observed.commit, observed.first_seen) == (first, NOW + timedelta(days=2))
