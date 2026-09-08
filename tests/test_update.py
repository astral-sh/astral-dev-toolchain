import io
import subprocess
import sys
import tomllib
from contextlib import redirect_stderr
from datetime import timedelta
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from astral_dev_toolchain._github import GitHub, GitHubError, _Redirects
from astral_dev_toolchain._update import (
    COOLDOWN,
    definitions,
    repository,
    scan,
    tag_version,
    update_local,
)
from astral_dev_toolchain_core import Tool, ToolUpdate
from helpers import NOW, A, B, C, D, MemoryGitHub, definition
from packaging.version import Version
from pydantic import ValidationError

from astral_dev_toolchain import _cli


class Updater:
    def __init__(self, root: Path):
        self.root = root
        self.path = definition(self.root)
        self.api = MemoryGitHub()

    def run_update(self, now=NOW, *, dry_run=False):
        return update_local(
            self.root / "toolchain", github=self.api, now=now, dry_run=dry_run
        )

    def observe(self):
        result = self.run_update()
        assert not result.errors
        assert not any(d.proposal for d in result.discoveries)
        return self.observations()

    def observations(self):
        tool = Tool.load(self.path)
        assert tool.update is not None
        return tool.update.observed

    def proposal(self, result):
        found = [d.proposal for d in result.discoveries if d.proposal is not None]
        assert len(found) == 1
        return found[0]


@pytest.fixture
def updater(tmp_path: Path):
    return Updater(tmp_path)


def test_configuration_defaults():
    assert ToolUpdate(github=True).tag_prefix == ""


@pytest.mark.parametrize(
    "options",
    [
        {"github": "true"},
        {"github": True, "rss": True},
        {"github": True, "tag-prefix": 3},
    ],
)
def test_invalid_configuration(options):
    with pytest.raises(ValidationError):
        ToolUpdate.model_validate(options)


def test_configuration_is_optional(updater: Updater):
    raw = tomllib.loads(updater.path.read_text())
    del raw["update"]
    assert Tool.model_validate(raw).update is None


@pytest.mark.parametrize(
    "tag, prefix, expected",
    (
        ("1.9.0", "", "1.9.0"),
        ("v1.10.0", "", "1.10.0"),
        ("cargo-audit/v0.22.2", "cargo-audit/", "0.22.2"),
        ("cargo-nextest-0.9.143", "cargo-nextest-", "0.9.143"),
        ("release-plz-v0.3.162", "release-plz-", "0.3.162"),
        ("cargo-lock/v1.0.0", "cargo-audit/", None),
        ("v2.0.0-rc.1", "", None),
        ("2.0.0a1", "", None),
        ("2.0.0+local", "", None),
        ("2.0.0.post1", "", None),
        ("2.0.0.dev1", "", None),
        ("2.0", "", None),
        ("02.0.0", "", None),
        ("vv2.0.0", "", None),
        (" 2.0.0", "", None),
        ("2.0.0\n", "", None),
    ),
)
def test_tag_versions(tag, prefix, expected):
    assert tag_version(tag, prefix) == (Version(expected) if expected else None)


def test_repository_normalization():
    assert repository("https://github.com/Owner/Repo.git/") == "owner/repo"


@pytest.mark.parametrize(
    "url",
    [
        "https://codeberg.org/o/r",
        "http://github.com/o/r",
        "https://github.com/o/r/tags",
        "https://user@github.com/o/r",
        "https://github.com/o/r?token=secret",
    ],
)
def test_invalid_repository(url):
    with pytest.raises(ValueError):
        repository(url)


def test_observations_are_stored_in_build_toml(updater: Updater):
    observed = updater.observe()
    assert set(observed) == {"v1.10.0", "v2.0.0"}
    assert (observed["v2.0.0"].commit, observed["v2.0.0"].first_seen) == (C, NOW)
    assert '[update.observed."v2.0.0"]' in updater.path.read_text()
    assert Tool.load(updater.path).source.tag == "v1.9.0"
    assert list(updater.root.rglob("*.json")) == []


def test_first_run_and_exact_seven_day_boundary(updater: Updater):
    updater.observe()
    result = updater.run_update(
        NOW + COOLDOWN - timedelta(microseconds=1), dry_run=True
    )
    assert not any(d.proposal for d in result.discoveries)
    result = updater.run_update(NOW + COOLDOWN)
    proposal = updater.proposal(result)
    assert (proposal.tag, proposal.commit) == ("v2.0.0", C)
    before = updater.path.read_text()
    assert not any(d.proposal for d in updater.run_update(NOW + COOLDOWN).discoveries)
    assert updater.path.read_text() == before


def test_highest_eligible_version_not_highest_tag(updater: Updater):
    del updater.api.upstreams["upstream/example"]["v2.0.0"]
    updater.observe()
    updater.api.upstreams["upstream/example"]["v2.0.0"] = C
    result = updater.run_update(NOW + COOLDOWN)
    assert updater.proposal(result).tag == "v1.10.0"
    assert any("v2.0.0 cooling down" in message for message in result.messages)


def test_all_tags_are_scanned_in_one_git_advertisement_without_order_assumptions(
    updater: Updater,
):
    updater.api.upstreams["upstream/example"] = (
        {"v1.9.0": A} | {f"unrelated-{i}": A for i in range(999)} | {"v1.10.0": B}
    )
    updater.observe()
    updater.api.calls.clear()
    updater.api.git_calls.clear()
    assert (
        updater.proposal(updater.run_update(NOW + COOLDOWN, dry_run=True)).tag
        == "v1.10.0"
    )
    assert updater.api.git_calls == [("upstream/example", "")]
    assert not any("page=" in path for _, path, _ in updater.api.calls)


def test_failed_discovery_preserves_observations_and_continues_other_repos(
    updater: Updater,
):
    definition(updater.root, "other", "upstream/other")
    updater.api.upstreams["upstream/other"] = {"v1.9.0": A, "v1.10.0": B}
    updater.observe()
    before = updater.path.read_bytes()
    updater.api.upstreams["upstream/example"] = {"v1.9.0": A} | {
        f"unrelated-{i}": A for i in range(100)
    }
    updater.api.failures.append(("git ls-remote", "upstream/example", "before"))
    result = updater.run_update(NOW + COOLDOWN)
    assert updater.path.read_bytes() == before
    assert [d.name for d in result.discoveries if d.proposal] == ["other"]
    assert len(result.errors) == 1


def test_annotated_tags_use_advertised_peeled_commits(updater: Updater):
    for tag in ("v1.0.0", "v99.0.0-rc.1", "unrelated/v99.0.0"):
        updater.api.upstreams["upstream/example"][tag] = C
        updater.api.annotated["upstream/example", tag] = A
    updater.api.annotated["upstream/example", "v2.0.0"] = D
    updater.api.tag_objects[D] = {"type": "tag", "sha": B}
    updater.api.tag_objects[B] = {"type": "commit", "sha": C}
    updater.observe()
    assert set(updater.observations()) == {"v1.10.0", "v2.0.0"}
    assert updater.observations()["v2.0.0"].commit == C
    assert [path for _, path, _ in updater.api.calls] == [
        "/repos/upstream/example/git/ref/tags/v1.9.0",
    ]
    assert updater.proposal(updater.run_update(NOW + COOLDOWN)).commit == C


@pytest.mark.parametrize(
    "prefixes, common_prefix",
    [
        (("project/a/", "project/b/"), "project/"),
        (("project/a/", "other/"), ""),
        (("", "other/"), ""),
    ],
)
def test_tools_sharing_a_repository_share_discovery(
    updater: Updater, prefixes, common_prefix
):
    paths = (updater.path, definition(updater.root, "other"))
    updater.api.upstreams["upstream/example"] = {}
    for path, prefix in zip(paths, prefixes, strict=True):
        path.write_text(
            path.read_text()
            .replace('tag = "v1.9.0"', f'tag = "{prefix}v1.9.0"')
            .replace("github = true", f'github = true\ntag-prefix = "{prefix}"')
        )
        updater.api.upstreams["upstream/example"].update(
            {f"{prefix}v1.9.0": A, f"{prefix}v2.0.0": C}
        )
    result = updater.run_update()
    assert not result.errors
    assert len(result.discoveries) == 2
    for discovery, prefix in zip(result.discoveries, prefixes, strict=True):
        assert set(discovery.observed) == {f"{prefix}v2.0.0"}
    assert updater.api.git_calls == [("upstream/example", common_prefix)]
    result = updater.run_update(NOW + COOLDOWN)
    assert not result.errors
    assert all(
        d.proposal is not None and d.proposal.commit == C for d in result.discoveries
    )


@pytest.mark.parametrize(
    "output",
    [
        "invalid output",
        "bad-sha\trefs/tags/v2.0.0\n",
        f"{C}\trefs/heads/main\n",
        f"{C}\trefs/tags/v2.0.0^{{}}\n",
        f"{C}\trefs/tags/v2.0.0\n{D}\trefs/tags/v2.0.0\n",
    ],
)
def test_invalid_git_advertisements_preserve_observations(updater: Updater, output):
    updater.observe()
    before = updater.path.read_bytes()
    with patch.object(updater.api, "_ls_remote", return_value=output):
        result = updater.run_update(NOW + COOLDOWN)
    assert result.errors
    assert not result.discoveries
    assert updater.path.read_bytes() == before


def test_peeled_ids_can_precede_raw_tag_refs():
    api = GitHub(token="unused-test-token")
    output = (
        f"{C}\trefs/tags/v2.0.0^{{}}\n{A}\trefs/tags/v1.9.0\n{B}\trefs/tags/v2.0.0\n"
    )
    with (
        patch.object(api, "_ls_remote", return_value=output),
        patch.object(api, "request", side_effect=AssertionError("No API reads needed")),
    ):
        assert api.tags("upstream/example") == {"v1.9.0": A, "v2.0.0": C}


@pytest.mark.parametrize("token", [None, "unused-test-token"])
def test_git_discovery_is_bounded_and_credentials_stay_out_of_argv(monkeypatch, token):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    api = GitHub(token=token)
    response = subprocess.CompletedProcess(
        ["git"], 0, stdout=f"{C}\trefs/tags/project/v2.0.0\n", stderr=""
    )
    with patch(
        "astral_dev_toolchain._github.subprocess.run", return_value=response
    ) as run:
        assert api.tags("upstream/example", "project/") == {"project/v2.0.0": C}
    command = run.call_args.args[0]
    assert command[-4:] == [
        "ls-remote",
        "--tags",
        "https://github.com/upstream/example.git",
        "refs/tags/project/*",
    ]
    assert run.call_args.kwargs["timeout"] == 30
    assert run.call_args.kwargs["check"]
    env = run.call_args.kwargs["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    if token:
        assert all(token not in arg for arg in command)
        assert env["ASTRAL_DEV_TOOLCHAIN_GIT_AUTHORIZATION"].startswith(
            "Authorization: Basic "
        )
    else:
        assert not any("extraheader" in arg for arg in command)


@pytest.mark.parametrize(
    "error, message",
    [
        (subprocess.TimeoutExpired(["git"], 30), "timed out after 30 seconds"),
        (
            subprocess.CalledProcessError(128, ["git"], stderr="connection failed"),
            "connection failed",
        ),
        (FileNotFoundError("git"), "cannot run git ls-remote"),
    ],
)
def test_git_failures_are_reported(error, message):
    api = GitHub(token="unused-test-token")
    with (
        patch("astral_dev_toolchain._github.subprocess.run", side_effect=error),
        pytest.raises(GitHubError, match=message) as failure,
    ):
        api.tags("upstream/example")
    assert failure.value.__cause__ is error


def test_candidate_target_must_still_be_a_commit(updater: Updater):
    updater.api.annotated["upstream/example", "v2.0.0"] = D
    updater.api.tag_objects[D] = {"type": "tree", "sha": C}
    updater.observe()
    result = updater.run_update(NOW + COOLDOWN)
    assert any("does not resolve to a commit" in error for error in result.errors)
    assert not any(d.proposal for d in result.discoveries)
    assert Tool.load(updater.path).source.commit == A


def test_moved_deleted_and_reappearing_candidates_restart_cooldown(updater: Updater):
    updater.observe()
    updater.api.upstreams["upstream/example"]["v2.0.0"] = D
    del updater.api.upstreams["upstream/example"]["v1.10.0"]
    result = updater.run_update(NOW + COOLDOWN)
    assert not any(d.proposal for d in result.discoveries)
    assert updater.observations()["v2.0.0"].first_seen == NOW + COOLDOWN
    assert "v1.10.0" not in updater.observations()
    updater.api.upstreams["upstream/example"]["v1.10.0"] = B
    assert not any(d.proposal for d in updater.run_update(NOW + COOLDOWN).discoveries)
    assert updater.observations()["v1.10.0"].first_seen == NOW + COOLDOWN


def test_deleting_all_candidates_clears_the_manifest(updater: Updater):
    updater.observe()
    updater.api.upstreams["upstream/example"] = {"v1.9.0": A}
    updater.run_update(NOW + COOLDOWN)
    assert updater.observations() == {}
    assert "[update.observed" not in updater.path.read_text()


@pytest.mark.parametrize("sha", (D, None))
def test_changed_and_missing_pins_require_manual_review(updater: Updater, sha):
    updater.observe()
    before = updater.path.read_bytes()
    if sha:
        updater.api.upstreams["upstream/example"]["v1.9.0"] = sha
    else:
        del updater.api.upstreams["upstream/example"]["v1.9.0"]
    result = updater.run_update(NOW + COOLDOWN)
    assert "review manually" in result.errors[0]
    assert updater.path.read_bytes() == before


def test_annotated_tag_peeling_and_encoded_prefix(updater: Updater):
    updater.api.upstreams["upstream/example"]["project/v2.0.0"] = C
    updater.api.annotated["upstream/example", "project/v2.0.0"] = A
    updater.api.tag_objects[A] = {"type": "tag", "sha": B}
    updater.api.tag_objects[B] = {"type": "commit", "sha": C}
    assert updater.api.resolve_tag("upstream/example", "project/v2.0.0") == C
    assert "project%2Fv2.0.0" in updater.api.calls[0][1]
    updater.api.tag_objects[B] = {"type": "tag", "sha": A}
    with pytest.raises(GitHubError, match="does not resolve"):
        updater.api.resolve_tag("upstream/example", "project/v2.0.0")


def test_comment_preserving_go_edit_resets_rebuild(updater: Updater):
    definition(updater.root, go=True)
    updater.observe()
    result = updater.run_update(NOW + COOLDOWN)
    assert not result.errors
    text = updater.path.read_text()
    assert "# Keep this comment." in text
    assert "rebuild" not in text
    tool = Tool.load(updater.path)
    assert (tool.source.tag, tool.source.commit, tool.tool.version) == (
        "v2.0.0",
        C,
        "2.0.0",
    )
    assert tool.tests[0].run == ("example", "--help")


def test_mismatching_explicit_version_is_not_overwritten(updater: Updater):
    definition(updater.root, go=True)
    updater.path.write_text(
        updater.path.read_text().replace('version = "1.9.0"', 'version = "9.0.0"')
    )
    updater.observe()
    before = updater.path.read_bytes()
    result = updater.run_update(NOW + COOLDOWN)
    assert "explicit tool.version" in result.errors[0]
    assert updater.path.read_bytes() == before


def test_inline_tables_remain_valid(updater: Updater):
    updater.path.write_text(
        f'tool = {{name="example", backend="rust"}}\nsource = {{repo="https://github.com/upstream/example", tag="v1.9.0", commit="{A}"}}\nupdate = {{github=true}}\n'
    )
    updater.observe()
    assert not updater.run_update(NOW + COOLDOWN).errors
    assert Tool.load(updater.path).source.commit == C


def test_dry_run_does_not_create_or_modify_files(updater: Updater):
    before = updater.path.read_bytes()
    updater.run_update(dry_run=True)
    assert updater.path.read_bytes() == before
    updater.observe()
    before = updater.path.read_bytes()
    result = updater.run_update(NOW + COOLDOWN, dry_run=True)
    assert any("would update" in message for message in result.messages)
    assert updater.path.read_bytes() == before
    assert list(updater.root.rglob("*.json")) == []
    assert not updater.api.writes


def test_candidate_changes_at_write_boundary(updater: Updater):
    updater.observe()
    with patch.object(updater.api, "resolve_tag", side_effect=[A, A, D]):
        result = updater.run_update(NOW + COOLDOWN)
    assert "cooldown restarted" in result.errors[0]
    assert Tool.load(updater.path).source.commit == A
    observed = updater.observations()["v2.0.0"]
    assert (observed.commit, observed.first_seen) == (D, NOW + COOLDOWN)


def test_candidate_deletion_at_write_boundary(updater: Updater):
    updater.observe()
    with patch.object(
        updater.api, "resolve_tag", side_effect=[A, A, GitHubError("gone", 404)]
    ):
        result = updater.run_update(NOW + COOLDOWN)
    assert result.errors
    assert Tool.load(updater.path).source.commit == A
    assert "v2.0.0" not in updater.observations()


def test_definition_changed_during_update_is_preserved(updater: Updater):
    updater.observe()
    original_resolve = updater.api.resolve_tag

    def resolving(repo, tag):
        if tag == "v2.0.0":
            updater.path.write_text(updater.path.read_text() + "\n# Human edit\n")
        return original_resolve(repo, tag)

    with patch.object(updater.api, "resolve_tag", side_effect=resolving):
        result = updater.run_update(NOW + COOLDOWN)
    assert "definition changed" in result.errors[0]
    assert "# Human edit" in updater.path.read_text()
    assert Tool.load(updater.path).source.tag == "v1.9.0"


def test_atomic_write_failure_preserves_pin_and_observations(updater: Updater):
    updater.observe()
    before = updater.path.read_bytes()
    with patch(
        "astral_dev_toolchain._update.os.fsync", side_effect=OSError("disk full")
    ):
        result = updater.run_update(NOW + COOLDOWN)
    assert "disk full" in result.errors[0]
    assert updater.path.read_bytes() == before
    assert list(updater.path.parent.iterdir()) == [updater.path]


def test_selection_and_disabled_tools(updater: Updater):
    path = definition(updater.root, "manual", "codeberg.org/manual")
    path.write_text(path.read_text().replace("github = true", "github = false"))
    result = update_local(
        updater.root / "toolchain", ["manual"], github=updater.api, now=NOW
    )
    assert result.messages == ["manual: automatic updates disabled"]
    assert not updater.api.calls
    with pytest.raises(ValueError, match="unknown tools"):
        definitions(updater.root / "toolchain", ["missing"])
    assert len(definitions(updater.root / "toolchain", ["example", "example"])) == 1


@pytest.mark.parametrize(
    "field",
    (
        'commit = "bad"\nfirst-seen = 2026-09-08T00:00:00Z',
        f'commit = "{B}"\nfirst-seen = 2026-09-08T00:00:00',
        f'commit = "{B}"\nfirst-seen = "invalid"',
    ),
)
def test_invalid_observations_stop_before_discovery(updater: Updater, field):
    original = updater.path.read_text()
    updater.path.write_text(original + '\n[update.observed."v1.10.0"]\n' + field + "\n")
    with pytest.raises(ValueError):
        updater.run_update()
    assert not updater.api.calls


def test_new_repository_does_not_inherit_branch_observations(updater: Updater):
    updater.observe()
    previous = Tool.load(updater.path)
    definition(updater.root, repo="upstream/new")
    updater.api.upstreams["upstream/new"] = {"v1.9.0": A, "v2.0.0": C}
    result = scan(
        definitions(updater.root / "toolchain"),
        updater.api,
        NOW + COOLDOWN,
        previous={"example": previous},
    )
    assert not result.discoveries[0].proposal
    assert result.discoveries[0].observed["v2.0.0"].first_seen == NOW + COOLDOWN


def test_cli_update_needs_no_state_file_or_docker(updater: Updater):
    before = updater.path.read_bytes()
    argv = [
        "astral-dev-toolchain",
        "--toolchain-dir",
        str(updater.root / "toolchain"),
        "update",
        "--dry-run",
        "--select",
        "example",
    ]
    with (
        patch.object(sys, "argv", argv),
        patch("astral_dev_toolchain._update.GitHub", return_value=updater.api),
        patch("shutil.which", side_effect=AssertionError("Docker is unnecessary")),
        redirect_stderr(io.StringIO()),
    ):
        _cli.run()
    assert updater.path.read_bytes() == before


def test_cli_update_interrupt_exits_without_a_traceback(updater: Updater):
    argv = [
        "astral-dev-toolchain",
        "--toolchain-dir",
        str(updater.root / "toolchain"),
        "update",
        "--dry-run",
    ]
    with (
        patch.object(sys, "argv", argv),
        patch("astral_dev_toolchain._cli.update_local", side_effect=KeyboardInterrupt),
        pytest.raises(SystemExit) as failure,
    ):
        _cli.run()
    assert failure.value.code == 130


@pytest.mark.parametrize("method, attempts", (("GET", 3), ("POST", 1)))
def test_retries_gets_with_timeout_and_never_retries_writes(method, attempts):
    api = GitHub(token="unused-test-token")
    with (
        patch.object(api.opener, "open", side_effect=URLError("offline")) as opening,
        patch("astral_dev_toolchain._github.time.sleep"),
        pytest.raises(GitHubError),
    ):
        api.request(method, "/repos/o/r/tags")
    assert opening.call_count == attempts
    assert opening.call_args.kwargs["timeout"] == 30


@pytest.mark.parametrize("method, attempts", [("GET", 3), ("POST", 1)])
@pytest.mark.parametrize("error", [TimeoutError("timed out"), IncompleteRead(b"[", 10)])
def test_response_read_failures_are_bounded_and_close_the_response(
    method, attempts, error
):
    api = GitHub(token="unused-test-token")
    with (
        patch.object(api.opener, "open") as opening,
        patch("astral_dev_toolchain._github.time.sleep"),
        pytest.raises(GitHubError) as failure,
    ):
        opening.return_value.__enter__.return_value.read.side_effect = error
        api.request(method, "/repos/o/r/tags")
    assert failure.value.__cause__ is error
    assert opening.call_count == attempts
    assert opening.return_value.__exit__.call_count == attempts


@pytest.mark.parametrize("token", [None, "unused-test-token"])
def test_exhausted_rate_limit_explains_authentication(monkeypatch, token):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    api = GitHub(token=token)
    headers = Message()
    headers["X-RateLimit-Remaining"] = "0"
    error = HTTPError(
        "https://api.github.com/repos/o/r", 403, "Forbidden", headers, None
    )
    with (
        patch.object(api.opener, "open", side_effect=error) as opening,
        pytest.raises(GitHubError, match="API rate limit exhausted") as failure,
    ):
        api.request("GET", "/repos/o/r")
    assert failure.value.status == 403
    assert ("set GH_TOKEN" in str(failure.value)) == (token is None)
    opening.assert_called_once()


def test_server_error_then_success():
    api = GitHub(token="unused-test-token")
    error = HTTPError(
        "https://api.github.com/repos/o/r", 503, "unavailable", Message(), None
    )
    with (
        patch.object(api.opener, "open", side_effect=[error, io.BytesIO(b"[]")]),
        patch("astral_dev_toolchain._github.time.sleep"),
    ):
        assert api.request("GET", "/repos/o/r") == []


def test_long_retry_after_is_reported_without_sleep():
    api = GitHub(token="unused-test-token")
    headers = Message()
    headers["Retry-After"] = "900"
    error = HTTPError(
        "https://api.github.com/repos/o/r", 429, "rate limited", headers, None
    )
    with (
        patch.object(api.opener, "open", side_effect=error),
        patch("astral_dev_toolchain._github.time.sleep") as sleeping,
        pytest.raises(GitHubError),
    ):
        api.request("GET", "/repos/o/r")
    sleeping.assert_not_called()


def test_pagination_cap_fails_closed():
    api = GitHub(token="unused-test-token")
    with (
        patch.object(api, "request", return_value=[{}] * 100) as request,
        pytest.raises(GitHubError, match="scan incomplete"),
    ):
        list(api.paginate("/repos/o/r/tags"))
    assert request.call_count == 100


def test_redirects_do_not_leak_credentials():
    with pytest.raises(GitHubError, match="another origin"):
        _Redirects().redirect_request(
            Request("https://api.github.com/repos/o/r"),
            None,
            302,
            "Found",
            {},
            "https://example.org/leak",
        )
