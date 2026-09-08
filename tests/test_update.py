import io
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

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


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = definition(self.root)
        self.api = MemoryGitHub()

    def run_update(self, now=NOW, *, dry_run=False):
        return update_local(
            self.root / "toolchain", github=self.api, now=now, dry_run=dry_run
        )

    def observe(self):
        result = self.run_update()
        self.assertFalse(result.errors)
        self.assertFalse(any(d.proposal for d in result.discoveries))
        return self.observations()

    def observations(self):
        tool = Tool.load(self.path)
        assert tool.update is not None
        return tool.update.observed

    def proposal(self, result):
        found = [d.proposal for d in result.discoveries if d.proposal is not None]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_configuration_is_optional_and_strict(self):
        self.assertEqual(ToolUpdate(github=True).tag_prefix, "")
        for options in (
            {"github": "true"},
            {"github": True, "rss": True},
            {"github": True, "tag-prefix": 3},
        ):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                ToolUpdate.model_validate(options)
        raw = tomllib.loads(self.path.read_text())
        del raw["update"]
        self.assertIsNone(Tool.model_validate(raw).update)

    def test_tag_versions(self):
        for tag, prefix, expected in (
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
        ):
            with self.subTest(tag=tag):
                self.assertEqual(
                    tag_version(tag, prefix), Version(expected) if expected else None
                )

    def test_repository_normalization(self):
        self.assertEqual(repository("https://github.com/Owner/Repo.git/"), "owner/repo")
        for url in (
            "https://codeberg.org/o/r",
            "http://github.com/o/r",
            "https://github.com/o/r/tags",
            "https://user@github.com/o/r",
            "https://github.com/o/r?token=secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                repository(url)

    def test_observations_are_stored_in_build_toml(self):
        observed = self.observe()
        self.assertEqual(set(observed), {"v1.10.0", "v2.0.0"})
        self.assertEqual(
            (observed["v2.0.0"].commit, observed["v2.0.0"].first_seen), (C, NOW)
        )
        self.assertIn('[update.observed."v2.0.0"]', self.path.read_text())
        self.assertEqual(Tool.load(self.path).source.tag, "v1.9.0")
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_first_run_and_exact_seven_day_boundary(self):
        self.observe()
        result = self.run_update(
            NOW + COOLDOWN - timedelta(microseconds=1), dry_run=True
        )
        self.assertFalse(any(d.proposal for d in result.discoveries))
        result = self.run_update(NOW + COOLDOWN)
        proposal = self.proposal(result)
        self.assertEqual((proposal.tag, proposal.commit), ("v2.0.0", C))
        before = self.path.read_text()
        self.assertFalse(
            any(d.proposal for d in self.run_update(NOW + COOLDOWN).discoveries)
        )
        self.assertEqual(self.path.read_text(), before)

    def test_highest_eligible_version_not_highest_tag(self):
        del self.api.upstreams["upstream/example"]["v2.0.0"]
        self.observe()
        self.api.upstreams["upstream/example"]["v2.0.0"] = C
        result = self.run_update(NOW + COOLDOWN)
        self.assertEqual(self.proposal(result).tag, "v1.10.0")
        self.assertTrue(
            any("v2.0.0 cooling down" in message for message in result.messages)
        )

    def test_all_pages_are_scanned_without_order_assumptions(self):
        self.api.upstreams["upstream/example"] = (
            {"v1.9.0": A} | {f"unrelated-{i}": A for i in range(99)} | {"v1.10.0": B}
        )
        self.observe()
        self.assertEqual(
            self.proposal(self.run_update(NOW + COOLDOWN, dry_run=True)).tag, "v1.10.0"
        )
        self.assertTrue(any("page=2" in path for _, path, _ in self.api.calls))

    def test_failed_page_preserves_observations_and_continues_other_repos(self):
        definition(self.root, "other", "upstream/other")
        self.api.upstreams["upstream/other"] = {"v1.9.0": A, "v1.10.0": B}
        self.observe()
        before = self.path.read_bytes()
        self.api.upstreams["upstream/example"] = {"v1.9.0": A} | {
            f"unrelated-{i}": A for i in range(100)
        }
        self.api.failures.append(
            ("GET", "/repos/upstream/example/tags?per_page=100&page=2", "before")
        )
        result = self.run_update(NOW + COOLDOWN)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([d.name for d in result.discoveries if d.proposal], ["other"])
        self.assertEqual(len(result.errors), 1)

    def test_moved_deleted_and_reappearing_candidates_restart_cooldown(self):
        self.observe()
        self.api.upstreams["upstream/example"]["v2.0.0"] = D
        del self.api.upstreams["upstream/example"]["v1.10.0"]
        result = self.run_update(NOW + COOLDOWN)
        self.assertFalse(any(d.proposal for d in result.discoveries))
        self.assertEqual(self.observations()["v2.0.0"].first_seen, NOW + COOLDOWN)
        self.assertNotIn("v1.10.0", self.observations())
        self.api.upstreams["upstream/example"]["v1.10.0"] = B
        self.assertFalse(
            any(d.proposal for d in self.run_update(NOW + COOLDOWN).discoveries)
        )
        self.assertEqual(self.observations()["v1.10.0"].first_seen, NOW + COOLDOWN)

    def test_deleting_all_candidates_clears_the_manifest(self):
        self.observe()
        self.api.upstreams["upstream/example"] = {"v1.9.0": A}
        self.run_update(NOW + COOLDOWN)
        self.assertEqual(self.observations(), {})
        self.assertNotIn("[update.observed", self.path.read_text())

    def test_changed_and_missing_pins_require_manual_review(self):
        self.observe()
        before = self.path.read_bytes()
        for sha in (D, None):
            with self.subTest(sha=sha):
                if sha:
                    self.api.upstreams["upstream/example"]["v1.9.0"] = sha
                else:
                    del self.api.upstreams["upstream/example"]["v1.9.0"]
                result = self.run_update(NOW + COOLDOWN)
                self.assertIn("review manually", result.errors[0])
                self.assertEqual(self.path.read_bytes(), before)

    def test_annotated_tag_peeling_and_encoded_prefix(self):
        self.api.upstreams["upstream/example"]["project/v2.0.0"] = C
        self.api.annotated["upstream/example", "project/v2.0.0"] = A
        self.api.tag_objects[A] = {"type": "tag", "sha": B}
        self.api.tag_objects[B] = {"type": "commit", "sha": C}
        self.assertEqual(self.api.resolve_tag("upstream/example", "project/v2.0.0"), C)
        self.assertIn("project%2Fv2.0.0", self.api.calls[0][1])
        self.api.tag_objects[B] = {"type": "tag", "sha": A}
        with self.assertRaisesRegex(GitHubError, "does not resolve"):
            self.api.resolve_tag("upstream/example", "project/v2.0.0")

    def test_comment_preserving_go_edit_resets_rebuild(self):
        definition(self.root, go=True)
        self.observe()
        result = self.run_update(NOW + COOLDOWN)
        self.assertFalse(result.errors)
        text = self.path.read_text()
        self.assertIn("# Keep this comment.", text)
        self.assertNotIn("rebuild", text)
        tool = Tool.load(self.path)
        self.assertEqual(
            (tool.source.tag, tool.source.commit, tool.tool.version),
            ("v2.0.0", C, "2.0.0"),
        )
        self.assertEqual(tool.tests[0].run, ("example", "--help"))

    def test_mismatching_explicit_version_is_not_overwritten(self):
        definition(self.root, go=True)
        self.path.write_text(
            self.path.read_text().replace('version = "1.9.0"', 'version = "9.0.0"')
        )
        self.observe()
        before = self.path.read_bytes()
        result = self.run_update(NOW + COOLDOWN)
        self.assertIn("explicit tool.version", result.errors[0])
        self.assertEqual(self.path.read_bytes(), before)

    def test_inline_tables_remain_valid(self):
        self.path.write_text(
            'tool = {name="example", backend="rust"}\n'
            f'source = {{repo="https://github.com/upstream/example", tag="v1.9.0", commit="{A}"}}\n'
            "update = {github=true}\n"
        )
        self.observe()
        self.assertFalse(self.run_update(NOW + COOLDOWN).errors)
        self.assertEqual(Tool.load(self.path).source.commit, C)

    def test_dry_run_does_not_create_or_modify_files(self):
        before = self.path.read_bytes()
        self.run_update(dry_run=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.observe()
        before = self.path.read_bytes()
        result = self.run_update(NOW + COOLDOWN, dry_run=True)
        self.assertTrue(any("would update" in message for message in result.messages))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.rglob("*.json")), [])
        self.assertFalse(self.api.writes)

    def test_candidate_changes_at_write_boundary(self):
        self.observe()
        with patch.object(self.api, "resolve_tag", side_effect=[A, A, D]):
            result = self.run_update(NOW + COOLDOWN)
        self.assertIn("cooldown restarted", result.errors[0])
        self.assertEqual(Tool.load(self.path).source.commit, A)
        observed = self.observations()["v2.0.0"]
        self.assertEqual((observed.commit, observed.first_seen), (D, NOW + COOLDOWN))

    def test_candidate_deletion_at_write_boundary(self):
        self.observe()
        with patch.object(
            self.api, "resolve_tag", side_effect=[A, A, GitHubError("gone", 404)]
        ):
            result = self.run_update(NOW + COOLDOWN)
        self.assertTrue(result.errors)
        self.assertEqual(Tool.load(self.path).source.commit, A)
        self.assertNotIn("v2.0.0", self.observations())

    def test_definition_changed_during_update_is_preserved(self):
        self.observe()
        original_resolve = self.api.resolve_tag

        def resolving(repo, tag):
            if tag == "v2.0.0":
                self.path.write_text(self.path.read_text() + "\n# Human edit\n")
            return original_resolve(repo, tag)

        with patch.object(self.api, "resolve_tag", side_effect=resolving):
            result = self.run_update(NOW + COOLDOWN)
        self.assertIn("definition changed", result.errors[0])
        self.assertIn("# Human edit", self.path.read_text())
        self.assertEqual(Tool.load(self.path).source.tag, "v1.9.0")

    def test_atomic_write_failure_preserves_pin_and_observations(self):
        self.observe()
        before = self.path.read_bytes()
        with patch(
            "astral_dev_toolchain._update.os.fsync", side_effect=OSError("disk full")
        ):
            result = self.run_update(NOW + COOLDOWN)
        self.assertIn("disk full", result.errors[0])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_selection_and_disabled_tools(self):
        path = definition(self.root, "manual", "codeberg.org/manual")
        path.write_text(path.read_text().replace("github = true", "github = false"))
        result = update_local(
            self.root / "toolchain", ["manual"], github=self.api, now=NOW
        )
        self.assertEqual(result.messages, ["manual: automatic updates disabled"])
        self.assertFalse(self.api.calls)
        with self.assertRaisesRegex(ValueError, "unknown tools"):
            definitions(self.root / "toolchain", ["missing"])
        self.assertEqual(
            len(definitions(self.root / "toolchain", ["example", "example"])), 1
        )

    def test_invalid_observations_stop_before_discovery(self):
        original = self.path.read_text()
        for field in (
            'commit = "bad"\nfirst-seen = 2026-09-08T00:00:00Z',
            f'commit = "{B}"\nfirst-seen = 2026-09-08T00:00:00',
            f'commit = "{B}"\nfirst-seen = "invalid"',
        ):
            with self.subTest(field=field):
                self.path.write_text(
                    original + '\n[update.observed."v1.10.0"]\n' + field + "\n"
                )
                with self.assertRaises(ValueError):
                    self.run_update()
                self.assertFalse(self.api.calls)

    def test_new_repository_does_not_inherit_branch_observations(self):
        self.observe()
        previous = Tool.load(self.path)
        definition(self.root, repo="upstream/new")
        self.api.upstreams["upstream/new"] = {"v1.9.0": A, "v2.0.0": C}
        result = scan(
            definitions(self.root / "toolchain"),
            self.api,
            NOW + COOLDOWN,
            previous={"example": previous},
        )
        self.assertFalse(result.discoveries[0].proposal)
        self.assertEqual(
            result.discoveries[0].observed["v2.0.0"].first_seen, NOW + COOLDOWN
        )

    def test_cli_update_needs_no_state_file_or_docker(self):
        before = self.path.read_bytes()
        argv = [
            "astral-dev-toolchain",
            "--toolchain-dir",
            str(self.root / "toolchain"),
            "update",
            "--dry-run",
            "--select",
            "example",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("astral_dev_toolchain._update.GitHub", return_value=self.api),
            patch("shutil.which", side_effect=AssertionError("Docker is unnecessary")),
            redirect_stderr(io.StringIO()),
        ):
            _cli.run()
        self.assertEqual(self.path.read_bytes(), before)


class GitHubRequestTests(unittest.TestCase):
    def test_retries_gets_with_timeout_and_never_retries_writes(self):
        api = GitHub(token="unused-test-token")
        for method, attempts in (("GET", 3), ("POST", 1)):
            with (
                self.subTest(method=method),
                patch.object(
                    api.opener, "open", side_effect=URLError("offline")
                ) as opening,
                patch("astral_dev_toolchain._github.time.sleep"),
                self.assertRaises(GitHubError),
            ):
                api.request(method, "/repos/o/r/tags")
            self.assertEqual(opening.call_count, attempts)
            self.assertEqual(opening.call_args.kwargs["timeout"], 30)

    def test_server_error_then_success(self):
        api = GitHub(token="unused-test-token")
        error = HTTPError(
            "https://api.github.com/repos/o/r", 503, "unavailable", Message(), None
        )
        with (
            patch.object(api.opener, "open", side_effect=[error, io.BytesIO(b"[]")]),
            patch("astral_dev_toolchain._github.time.sleep"),
        ):
            self.assertEqual(api.request("GET", "/repos/o/r"), [])

    def test_long_retry_after_is_reported_without_sleep(self):
        api = GitHub(token="unused-test-token")
        headers = Message()
        headers["Retry-After"] = "900"
        error = HTTPError(
            "https://api.github.com/repos/o/r",
            429,
            "rate limited",
            headers,
            None,
        )
        with (
            patch.object(api.opener, "open", side_effect=error),
            patch("astral_dev_toolchain._github.time.sleep") as sleeping,
            self.assertRaises(GitHubError),
        ):
            api.request("GET", "/repos/o/r")
        sleeping.assert_not_called()

    def test_pagination_cap_fails_closed(self):
        api = GitHub(token="unused-test-token")
        with (
            patch.object(api, "request", return_value=[{}] * 100) as request,
            self.assertRaisesRegex(GitHubError, "scan incomplete"),
        ):
            list(api.paginate("/repos/o/r/tags"))
        self.assertEqual(request.call_count, 100)

    def test_redirects_do_not_leak_credentials(self):
        with self.assertRaisesRegex(GitHubError, "another origin"):
            _Redirects().redirect_request(
                Request("https://api.github.com/repos/o/r"),
                None,
                302,
                "Found",
                {},
                "https://example.org/leak",
            )


if __name__ == "__main__":
    unittest.main()
