import importlib.util
import json
import os
import runpy
import sys
import tempfile
import tomllib
import unittest
from contextlib import chdir, redirect_stdout
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from astral_dev_toolchain._update import COOLDOWN
from astral_dev_toolchain_core import Tool
from helpers import NOW, A, B, C, D, MemoryGitHub, definition

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "ci_update_tools", ROOT / "scripts/ci-update-tools.py"
)
assert spec is not None and spec.loader is not None
ci = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ci
spec.loader.exec_module(ci)


class PullRequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = definition(self.root)
        self.api = MemoryGitHub()
        self.main = self.api.initialize(self.root)
        self.branch = "codex/update/example"

    def run_updates(self, now=NOW, *, dry_run=False):
        return ci.run_updates(
            self.api, self.api.repo, self.root, self.main, now=now, dry_run=dry_run
        )

    def create(self):
        self.assertFalse(self.run_updates().errors)
        result = self.run_updates(NOW + COOLDOWN)
        self.assertFalse(result.errors)
        self.assertEqual(len(self.api.prs), 1)
        return self.api.prs[1]

    def pr_writes(self):
        return [call for call in self.api.writes if "/pulls" in call[1]]

    def branch_tool(self):
        text = self.api.files(self.api.refs[self.branch])[
            "toolchain/example/build.toml"
        ]
        return Tool.model_validate(tomllib.loads(text))

    def human_commit(self, *, author="human", verified=False):
        files = self.api.files(self.api.refs[self.branch])
        files["toolchain/example/build.toml"] += "\n# Human edit\n"
        head = self.api.commit(
            files,
            [self.api.refs[self.branch]],
            "toolchain: update example",
            author=author,
            verified=verified,
        )
        self.api.set_head(self.branch, head)
        return head

    def test_first_run_observes_on_tool_branch_without_a_pr_or_global_state(self):
        result = self.run_updates()
        self.assertFalse(result.errors)
        self.assertFalse(self.api.prs)
        self.assertEqual(set(self.api.refs), {"main", self.branch})
        observed = self.api.observations()["v2.0.0"]
        self.assertEqual((observed.commit, observed.first_seen), (C, NOW))
        self.assertEqual(self.branch_tool().source.commit, A)
        self.assertEqual(
            self.api.files(self.main)["toolchain/example/build.toml"],
            self.path.read_text(),
        )
        for tree in self.api.trees.values():
            self.assertNotIn("state.json", tree)

    def test_no_newer_tags_need_no_branch(self):
        self.api.upstreams["upstream/example"] = {"v1.9.0": A}
        self.assertFalse(self.run_updates().errors)
        self.assertEqual(set(self.api.refs), {"main"})
        self.assertFalse(self.api.writes)

    def test_draft_is_idempotent_and_only_changes_the_tool_definition(self):
        pr = self.create()
        self.assertTrue(pr["draft"])
        self.assertIn("v2.0.0", pr["body"])
        self.assertIn(C, pr["body"])
        self.assertIn(A, pr["body"])
        self.assertIn("ready for review", pr["body"])
        base, branch = self.api.files(self.main), self.api.files(pr["head"]["sha"])
        self.assertEqual(
            [path for path in branch if base[path] != branch[path]],
            ["toolchain/example/build.toml"],
        )
        self.api.calls.clear()
        self.assertFalse(self.run_updates(NOW + COOLDOWN + timedelta(days=1)).errors)
        self.assertFalse(self.api.writes)
        self.assertEqual(len(self.api.prs), 1)

    def test_refreshes_same_draft_after_new_candidate_cooldown(self):
        pr = self.create()
        self.api.upstreams["upstream/example"]["v3.0.0"] = D
        self.run_updates(NOW + COOLDOWN)
        self.assertEqual(self.branch_tool().source.commit, C)
        self.assertEqual(self.api.observations()["v3.0.0"].first_seen, NOW + COOLDOWN)
        self.assertEqual(pr["title"], "toolchain: update example to 2.0.0")
        old_head = pr["head"]["sha"]
        result = self.run_updates(NOW + COOLDOWN * 2)
        self.assertFalse(result.errors)
        self.assertEqual(len(self.api.prs), 1)
        self.assertEqual(pr["title"], "toolchain: update example to 3.0.0")
        self.assertTrue(self.api.ancestor(old_head, pr["head"]["sha"]))

    def test_refresh_incorporates_updated_build_settings_from_main(self):
        pr = self.create()
        files = self.api.files(self.main)
        files["toolchain/example/build.toml"] += "\n# New build workaround\n"
        self.path.write_text(files["toolchain/example/build.toml"])
        self.main = self.api.commit(files, [self.main])
        self.api.set_head("main", self.main)
        self.assertFalse(self.run_updates(NOW + COOLDOWN).errors)
        self.assertIn(
            "# New build workaround",
            self.api.files(pr["head"]["sha"])["toolchain/example/build.toml"],
        )

    def test_ready_pr_is_frozen_even_after_returning_to_draft(self):
        pr = self.create()
        head = pr["head"]["sha"]
        pr["draft"] = False
        self.api.events[1].append({"event": "ready_for_review"})
        self.api.calls.clear()
        result = self.run_updates(NOW + COOLDOWN)
        self.assertTrue(any("frozen" in message for message in result.messages))
        pr["draft"] = True
        self.run_updates(NOW + COOLDOWN * 2)
        self.assertEqual(pr["head"]["sha"], head)
        self.assertFalse(self.api.writes)

    def test_ready_and_draft_between_runs_is_detected_from_history(self):
        self.create()
        self.api.events[1] = [
            {"event": "ready_for_review"},
            {"event": "converted_to_draft"},
        ]
        self.api.calls.clear()
        result = self.run_updates(NOW + COOLDOWN)
        self.assertTrue(any("frozen" in message for message in result.messages))
        self.assertFalse(self.api.writes)

    def test_human_commit_freezes_draft(self):
        self.create()
        head = self.human_commit()
        self.api.calls.clear()
        result = self.run_updates(NOW + COOLDOWN)
        self.assertEqual(self.api.refs[self.branch], head)
        self.assertTrue(any("frozen" in message for message in result.messages))
        self.assertFalse(self.api.writes)

    def test_human_commit_during_cooldown_freezes_branch(self):
        self.run_updates()
        head = self.human_commit()
        self.api.calls.clear()
        result = self.run_updates(NOW + COOLDOWN)
        self.assertEqual(self.api.refs[self.branch], head)
        self.assertTrue(any("frozen" in message for message in result.messages))
        self.assertFalse(self.api.writes)
        self.assertFalse(self.api.prs)

    def test_unsigned_bot_identity_does_not_allow_overwriting_human_work(self):
        self.run_updates()
        self.human_commit(author="github-actions[bot]", verified=False)
        self.api.calls.clear()
        result = self.run_updates(NOW + COOLDOWN)
        self.assertTrue(any("frozen" in message for message in result.messages))
        self.assertFalse(self.api.writes)

    def test_closed_version_is_suppressed_but_newer_version_gets_a_draft(self):
        pr = self.create()
        pr["state"] = "closed"
        self.api.calls.clear()
        result = self.run_updates(NOW + COOLDOWN)
        self.assertTrue(any("suppressed" in message for message in result.messages))
        self.assertFalse(self.pr_writes())
        self.api.upstreams["upstream/example"]["v3.0.0"] = D
        self.run_updates(NOW + COOLDOWN)
        self.run_updates(NOW + COOLDOWN * 2)
        self.assertEqual(len(self.api.prs), 2)
        self.assertEqual(self.api.prs[2]["title"], "toolchain: update example to 3.0.0")

    def test_closed_pr_is_suppressed_even_if_its_description_was_edited(self):
        pr = self.create()
        pr["body"] = "Superseded; closing this update."
        pr["state"] = "closed"
        self.api.calls.clear()
        result = self.run_updates(NOW + COOLDOWN)
        self.assertFalse(result.errors)
        self.assertTrue(any("suppressed" in message for message in result.messages))
        self.assertFalse(self.pr_writes())

    def test_human_commits_after_closure_are_preserved(self):
        pr = self.create()
        pr["state"] = "closed"
        head = self.human_commit()
        self.api.upstreams["upstream/example"]["v3.0.0"] = D
        self.api.calls.clear()
        self.run_updates(NOW + COOLDOWN)
        self.run_updates(NOW + COOLDOWN * 2)
        self.assertEqual(self.api.refs[self.branch], head)
        self.assertFalse(self.api.writes)

    def test_resolved_human_edit_to_definition_allows_next_update(self):
        pr = self.create()
        self.human_commit()
        pr["state"] = "closed"
        self.api.upstreams["upstream/example"]["v3.0.0"] = D
        self.assertFalse(self.run_updates(NOW + COOLDOWN).errors)
        self.assertFalse(self.run_updates(NOW + COOLDOWN * 2).errors)
        self.assertEqual(len(self.api.prs), 2)

    def test_unrelated_human_changes_are_not_carried_into_a_new_pr(self):
        pr = self.create()
        files = self.api.files(self.api.refs[self.branch])
        files["README.md"] += "Unrelated human edit\n"
        head = self.api.commit(files, [self.api.refs[self.branch]], "human")
        self.api.set_head(self.branch, head)
        pr["state"] = "closed"
        self.api.calls.clear()
        result = self.run_updates(NOW + COOLDOWN)
        self.assertTrue(any("frozen" in message for message in result.messages))
        self.assertFalse(self.api.writes)

    def test_deleted_branch_can_be_recreated_without_reopening_rejected_pr(self):
        self.create()["state"] = "closed"
        self.run_updates(NOW + COOLDOWN)
        del self.api.refs[self.branch]
        self.api.upstreams["upstream/example"]["v3.0.0"] = D
        self.run_updates(NOW + COOLDOWN)
        self.assertEqual(len(self.api.prs), 1)
        self.assertFalse(self.run_updates(NOW + COOLDOWN * 2).errors)
        self.assertEqual(len(self.api.prs), 2)
        self.assertEqual(self.api.prs[2]["title"], "toolchain: update example to 3.0.0")

    def test_merge_preserves_observations_in_main_for_the_next_update(self):
        pr = self.create()
        self.api.upstreams["upstream/example"]["v3.0.0"] = D
        self.run_updates(NOW + COOLDOWN)
        pr["state"] = "closed"
        pr["merged_at"] = NOW.isoformat()
        self.main = pr["head"]["sha"]
        self.api.set_head("main", self.main)
        self.path.write_text(self.api.files(self.main)["toolchain/example/build.toml"])
        del self.api.refs[self.branch]
        result = self.run_updates(NOW + COOLDOWN * 2)
        self.assertFalse(result.errors)
        self.assertEqual(len(self.api.prs), 2)
        self.assertIn("from `2.0.0` to `3.0.0`", self.api.prs[2]["body"])

    def test_dry_run_only_makes_reads(self):
        result = self.run_updates(dry_run=True)
        self.assertTrue(
            any("would record observations" in message for message in result.messages)
        )
        self.assertFalse(self.api.writes)
        self.run_updates()
        head = self.api.refs[self.branch]
        self.api.calls.clear()
        result = self.run_updates(NOW + COOLDOWN, dry_run=True)
        self.assertTrue(any("would propose" in message for message in result.messages))
        self.assertFalse(self.api.writes)
        self.assertEqual(self.api.refs[self.branch], head)

    def test_failed_manifest_commit_prevents_pr_writes_and_can_be_retried(self):
        self.run_updates()
        head = self.api.refs[self.branch]
        self.api.failures.append(("POST", "/graphql", "before"))
        result = self.run_updates(NOW + COOLDOWN)
        self.assertTrue(result.errors)
        self.assertFalse(self.api.prs)
        self.assertEqual(self.api.refs[self.branch], head)
        self.assertFalse(self.run_updates(NOW + COOLDOWN).errors)
        self.assertEqual(len(self.api.prs), 1)

    def test_uncertain_manifest_commit_recovers_from_branch_contents(self):
        self.run_updates()
        self.api.failures.append(("POST", "/graphql", "after"))
        self.assertTrue(self.run_updates(NOW + COOLDOWN).errors)
        self.assertFalse(self.api.prs)
        self.assertEqual(self.branch_tool().source.commit, C)
        head = self.api.refs[self.branch]
        self.assertFalse(self.run_updates(NOW + COOLDOWN).errors)
        self.assertEqual(len(self.api.prs), 1)
        self.assertEqual(self.api.refs[self.branch], head)

    def test_uncertain_branch_creation_is_recovered_without_journaling(self):
        self.api.failures.append(("POST", f"/repos/{self.api.repo}/git/refs", "after"))
        self.assertTrue(self.run_updates().errors)
        self.assertEqual(self.api.refs[self.branch], self.main)
        self.assertFalse(self.run_updates().errors)
        self.assertEqual(self.api.observations()["v2.0.0"].first_seen, NOW)

    def test_interrupted_branch_initialization_survives_main_advancing(self):
        self.api.failures.append(("POST", f"/repos/{self.api.repo}/git/refs", "after"))
        self.run_updates()
        self.main = self.api.commit(
            self.api.files(self.main), [self.main], "advance main"
        )
        self.api.set_head("main", self.main)
        self.assertFalse(self.run_updates().errors)
        self.assertTrue(self.api.observations())

    def test_uncertain_pr_creation_is_recovered_without_duplicates(self):
        self.run_updates()
        self.api.failures.append(("POST", f"/repos/{self.api.repo}/pulls", "after"))
        self.assertTrue(self.run_updates(NOW + COOLDOWN).errors)
        self.assertEqual(len(self.api.prs), 1)
        self.api.calls.clear()
        self.assertFalse(self.run_updates(NOW + COOLDOWN).errors)
        self.assertFalse(self.api.writes)
        self.assertEqual(len(self.api.prs), 1)

    def test_racing_human_commit_rejects_atomic_update(self):
        self.create()
        self.api.upstreams["upstream/example"]["v3.0.0"] = D
        self.run_updates(NOW + COOLDOWN)
        request = self.api.request
        human = []

        def race(method, endpoint, *, data=None):
            if method == "POST" and endpoint == "/graphql":
                human.append(self.human_commit())
            return request(method, endpoint, data=data)

        with patch.object(self.api, "request", side_effect=race):
            self.assertTrue(self.run_updates(NOW + COOLDOWN * 2).errors)
        self.assertEqual(self.api.refs[self.branch], human[0])
        self.api.calls.clear()
        self.run_updates(NOW + COOLDOWN * 2)
        self.assertFalse(self.api.writes)

    def test_ready_during_discovery_is_rechecked_before_writing(self):
        pr = self.create()
        self.api.calls.clear()
        request = self.api.request

        def become_ready(method, endpoint, *, data=None):
            if method == "GET" and endpoint == f"/repos/{self.api.repo}/pulls/1":
                pr["draft"] = False
            return request(method, endpoint, data=data)

        with patch.object(self.api, "request", side_effect=become_ready):
            result = self.run_updates(NOW + COOLDOWN)
        self.assertFalse(self.api.writes)
        self.assertTrue(
            any("waiting for reconciliation" in message for message in result.messages)
        )

    def test_candidate_change_prevents_pr_and_restarts_observation(self):
        self.run_updates()
        with patch.object(self.api, "resolve_tag", side_effect=[A, A, D]):
            result = self.run_updates(NOW + COOLDOWN)
        self.assertTrue(result.errors)
        self.assertFalse(self.api.prs)
        self.assertEqual(self.branch_tool().source.commit, A)
        observed = self.api.observations()["v2.0.0"]
        self.assertEqual((observed.commit, observed.first_seen), (D, NOW + COOLDOWN))

    def test_no_eligible_candidate_does_not_downgrade_an_open_draft(self):
        self.create()
        self.api.upstreams["upstream/example"] = {"v1.9.0": A, "v3.0.0": D}
        result = self.run_updates(NOW + COOLDOWN)
        self.assertFalse(result.errors)
        self.assertEqual(self.branch_tool().source.commit, C)
        self.assertEqual(set(self.api.observations()), {"v3.0.0"})
        self.assertEqual(self.api.prs[1]["title"], "toolchain: update example to 2.0.0")

    def test_partial_failure_does_not_block_other_tools(self):
        definition(self.root, "other", "upstream/other")
        self.api.upstreams["upstream/other"] = {"v1.9.0": A, "v1.10.0": B}
        self.main = self.api.initialize(self.root)
        self.run_updates()
        self.api.failures.append(
            ("GET", "/repos/upstream/example/tags?per_page=100&page=1", "before")
        )
        result = self.run_updates(NOW + COOLDOWN)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(self.api.prs), 1)
        self.assertEqual(self.api.prs[1]["title"], "toolchain: update other to 1.10.0")

    def test_stale_checkout_is_rejected(self):
        self.api.refs["main"] = self.api.commit(
            self.api.files(self.main), [self.main], "advance main"
        )
        with self.assertRaisesRegex(ValueError, "default branch head"):
            self.run_updates()
        self.assertFalse(self.api.writes)


class SelectionTests(unittest.TestCase):
    def test_ready_for_review_selects_the_updated_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            definition(root)
            definition(root, "unchanged")
            (root / "image").mkdir()
            (root / "image/toolchain.toml").write_text(
                '[rust.targets]\nx86_64-unknown-linux-gnu = "ubuntu-24.04"\n'
            )
            event = root / "event.json"
            event.write_text(
                json.dumps(
                    {
                        "action": "ready_for_review",
                        "pull_request": {"base": {"sha": A}, "head": {"sha": B}},
                    }
                )
            )
            output = root / "output"
            env = {
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_OUTPUT": str(output),
                "SELECT": "",
            }
            with (
                chdir(root),
                patch.dict(os.environ, env),
                patch(
                    "subprocess.check_output",
                    return_value=b"toolchain/example/build.toml\0",
                ) as diff,
                redirect_stdout(StringIO()),
            ):
                runpy.run_path(
                    str(ROOT / "scripts/ci-select-tools.py"), run_name="__main__"
                )
            diff.assert_called_once()
            self.assertIn(f"{A}...{B}", diff.call_args.args[0])
            values = dict(
                line.split("=", 1) for line in output.read_text().splitlines()
            )
            self.assertEqual(json.loads(values["tools"]), ["example"])
            self.assertEqual(json.loads(values["matrix"])[0]["tool"], "example")


if __name__ == "__main__":
    unittest.main()
