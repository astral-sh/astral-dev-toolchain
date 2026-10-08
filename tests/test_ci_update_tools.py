"""Exercise updater ownership against GitHub-shaped API responses."""

import base64
import importlib.util
import sys
from pathlib import Path

import pytest
from astral_dev_toolchain._update import Definition
from packaging.version import Version


@pytest.fixture
def updater(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "ci-update-tools.py"
    spec = importlib.util.spec_from_file_location("ci_update_tools", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "author, committer, verified, message, frozen",
    [
        (
            "astral-automations-bot[bot]",
            "web-flow",
            True,
            "toolchain: update example",
            False,
        ),
        ("github-actions[bot]", "web-flow", True, "toolchain: update example", False),
        (
            "astral-automations-bot[bot]",
            "astral-automations-bot[bot]",
            True,
            "toolchain: update example",
            False,
        ),
        (
            "github-actions[bot]",
            "github-actions[bot]",
            True,
            "toolchain: update example",
            False,
        ),
        ("contributor", "web-flow", True, "toolchain: update example", True),
        (
            "astral-automations-bot[bot]",
            "web-flow",
            False,
            "toolchain: update example",
            True,
        ),
        (
            "astral-automations-bot[bot]",
            "contributor",
            True,
            "toolchain: update example",
            True,
        ),
        ("astral-automations-bot[bot]", "web-flow", True, "manual change", True),
    ],
)
def test_update_branch_ownership(
    updater,
    monkeypatch,
    tmp_path,
    tool_file,
    author,
    committer,
    verified,
    message,
    frozen,
):
    definition = Definition.parse(tool_file, tool_file.read_text())

    def api(method, endpoint, *, data=None, paginate=False):
        assert method == "GET"
        if "/git/matching-refs/" in endpoint:
            return [
                {
                    "ref": "refs/heads/update/example",
                    "object": {"type": "commit", "sha": "branch"},
                }
            ]
        if "/pulls?" in endpoint:
            return [[]]
        if "/commits/" in endpoint:
            return {
                "author": {"login": author},
                "committer": {"login": committer},
                "commit": {"verification": {"verified": verified}, "message": message},
            }
        if "/compare/" in endpoint:
            # A non-updater commit here has not been merged into main.
            return {"status": "diverged"}
        if "/contents/" in endpoint:
            return {
                "encoding": "base64",
                "content": base64.b64encode(definition.text.encode()).decode(),
            }
        raise AssertionError(endpoint)

    monkeypatch.setattr(updater, "gh_api", api)
    branch = updater.PullRequests(
        "astral-sh/astral-dev-toolchain", "main", "base", tmp_path
    ).load(definition)
    assert branch.frozen is frozen
    assert (branch.definition is None) is frozen


@pytest.mark.parametrize(
    "author, expected",
    [
        ("astral-automations-bot[bot]", Version("2.0.0")),
        ("github-actions[bot]", Version("2.0.0")),
        ("contributor", None),
    ],
)
def test_rejections_from_old_and_new_updaters(
    updater, monkeypatch, tmp_path, tool_file, author, expected
):
    definition = Definition.parse(tool_file, tool_file.read_text())

    def api(method, endpoint, *, data=None, paginate=False):
        assert method == "GET"
        if "/git/matching-refs/" in endpoint:
            return []
        if "/pulls?" in endpoint:
            return [
                [
                    {
                        "state": "closed",
                        "merged_at": None,
                        "user": {"login": author},
                        "head": {
                            "ref": "update/example",
                            "repo": {"full_name": "astral-sh/astral-dev-toolchain"},
                            "sha": "closed-head",
                        },
                        "body": '<!-- astral-dev-toolchain-update: {"tool":"example","version":"2.0.0"} -->',
                    }
                ]
            ]
        raise AssertionError(endpoint)

    monkeypatch.setattr(updater, "gh_api", api)
    branch = updater.PullRequests(
        "astral-sh/astral-dev-toolchain", "main", "base", tmp_path
    ).load(definition)
    assert branch.ignored == expected
    assert not branch.frozen
