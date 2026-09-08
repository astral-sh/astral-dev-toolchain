import importlib.util
import json
import os
import runpy
import sys
import tomllib
from contextlib import chdir, redirect_stdout
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
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


class PullRequests:
    def __init__(self, root: Path):
        self.root = root
        self.path = definition(self.root)
        self.api = MemoryGitHub()
        self.main = self.api.initialize(self.root)
        self.branch = "codex/update/example"

    def run_updates(self, now=NOW, *, dry_run=False):
        return ci.run_updates(
            self.api, self.api.repo, self.root, self.main, now=now, dry_run=dry_run
        )

    def create(self):
        assert not self.run_updates().errors
        result = self.run_updates(NOW + COOLDOWN)
        assert not result.errors
        assert len(self.api.prs) == 1
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


@pytest.fixture
def prs(tmp_path: Path):
    return PullRequests(tmp_path)


def test_first_run_observes_on_tool_branch_without_a_pr_or_global_state(
    prs: PullRequests,
):
    result = prs.run_updates()
    assert not result.errors
    assert not prs.api.prs
    assert set(prs.api.refs) == {"main", prs.branch}
    observed = prs.api.observations()["v2.0.0"]
    assert (observed.commit, observed.first_seen) == (C, NOW)
    assert prs.branch_tool().source.commit == A
    assert (
        prs.api.files(prs.main)["toolchain/example/build.toml"] == prs.path.read_text()
    )
    for tree in prs.api.trees.values():
        assert "state.json" not in tree


def test_no_newer_tags_need_no_branch(prs: PullRequests):
    prs.api.upstreams["upstream/example"] = {"v1.9.0": A}
    assert not prs.run_updates().errors
    assert set(prs.api.refs) == {"main"}
    assert not prs.api.writes


def test_draft_is_idempotent_and_only_changes_the_tool_definition(prs: PullRequests):
    pr = prs.create()
    assert pr["draft"]
    assert "v2.0.0" in pr["body"]
    assert C in pr["body"]
    assert A in pr["body"]
    assert "ready for review" in pr["body"]
    base, branch = (prs.api.files(prs.main), prs.api.files(pr["head"]["sha"]))
    assert [path for path in branch if base[path] != branch[path]] == [
        "toolchain/example/build.toml"
    ]
    prs.api.calls.clear()
    assert not prs.run_updates(NOW + COOLDOWN + timedelta(days=1)).errors
    assert not prs.api.writes
    assert len(prs.api.prs) == 1


def test_refreshes_same_draft_after_new_candidate_cooldown(prs: PullRequests):
    pr = prs.create()
    prs.api.upstreams["upstream/example"]["v3.0.0"] = D
    prs.run_updates(NOW + COOLDOWN)
    assert prs.branch_tool().source.commit == C
    assert prs.api.observations()["v3.0.0"].first_seen == NOW + COOLDOWN
    assert pr["title"] == "toolchain: update example to 2.0.0"
    old_head = pr["head"]["sha"]
    result = prs.run_updates(NOW + COOLDOWN * 2)
    assert not result.errors
    assert len(prs.api.prs) == 1
    assert pr["title"] == "toolchain: update example to 3.0.0"
    assert prs.api.ancestor(old_head, pr["head"]["sha"])


def test_refresh_incorporates_updated_build_settings_from_main(prs: PullRequests):
    pr = prs.create()
    files = prs.api.files(prs.main)
    files["toolchain/example/build.toml"] += "\n# New build workaround\n"
    prs.path.write_text(files["toolchain/example/build.toml"])
    prs.main = prs.api.commit(files, [prs.main])
    prs.api.set_head("main", prs.main)
    assert not prs.run_updates(NOW + COOLDOWN).errors
    assert (
        "# New build workaround"
        in prs.api.files(pr["head"]["sha"])["toolchain/example/build.toml"]
    )


def test_ready_pr_is_frozen_even_after_returning_to_draft(prs: PullRequests):
    pr = prs.create()
    head = pr["head"]["sha"]
    pr["draft"] = False
    prs.api.events[1].append({"event": "ready_for_review"})
    prs.api.calls.clear()
    result = prs.run_updates(NOW + COOLDOWN)
    assert any("frozen" in message for message in result.messages)
    pr["draft"] = True
    prs.run_updates(NOW + COOLDOWN * 2)
    assert pr["head"]["sha"] == head
    assert not prs.api.writes


def test_ready_and_draft_between_runs_is_detected_from_history(prs: PullRequests):
    prs.create()
    prs.api.events[1] = [{"event": "ready_for_review"}, {"event": "converted_to_draft"}]
    prs.api.calls.clear()
    result = prs.run_updates(NOW + COOLDOWN)
    assert any("frozen" in message for message in result.messages)
    assert not prs.api.writes


def test_human_commit_freezes_draft(prs: PullRequests):
    prs.create()
    head = prs.human_commit()
    prs.api.calls.clear()
    result = prs.run_updates(NOW + COOLDOWN)
    assert prs.api.refs[prs.branch] == head
    assert any("frozen" in message for message in result.messages)
    assert not prs.api.writes


def test_human_commit_during_cooldown_freezes_branch(prs: PullRequests):
    prs.run_updates()
    head = prs.human_commit()
    prs.api.calls.clear()
    result = prs.run_updates(NOW + COOLDOWN)
    assert prs.api.refs[prs.branch] == head
    assert any("frozen" in message for message in result.messages)
    assert not prs.api.writes
    assert not prs.api.prs


def test_unsigned_bot_identity_does_not_allow_overwriting_human_work(prs: PullRequests):
    prs.run_updates()
    prs.human_commit(author="github-actions[bot]", verified=False)
    prs.api.calls.clear()
    result = prs.run_updates(NOW + COOLDOWN)
    assert any("frozen" in message for message in result.messages)
    assert not prs.api.writes


def test_closed_version_is_suppressed_but_newer_version_gets_a_draft(prs: PullRequests):
    pr = prs.create()
    pr["state"] = "closed"
    prs.api.calls.clear()
    result = prs.run_updates(NOW + COOLDOWN)
    assert any("suppressed" in message for message in result.messages)
    assert not prs.pr_writes()
    prs.api.upstreams["upstream/example"]["v3.0.0"] = D
    prs.run_updates(NOW + COOLDOWN)
    prs.run_updates(NOW + COOLDOWN * 2)
    assert len(prs.api.prs) == 2
    assert prs.api.prs[2]["title"] == "toolchain: update example to 3.0.0"


def test_closed_pr_is_suppressed_even_if_its_description_was_edited(prs: PullRequests):
    pr = prs.create()
    pr["body"] = "Superseded; closing this update."
    pr["state"] = "closed"
    prs.api.calls.clear()
    result = prs.run_updates(NOW + COOLDOWN)
    assert not result.errors
    assert any("suppressed" in message for message in result.messages)
    assert not prs.pr_writes()


def test_human_commits_after_closure_are_preserved(prs: PullRequests):
    pr = prs.create()
    pr["state"] = "closed"
    head = prs.human_commit()
    prs.api.upstreams["upstream/example"]["v3.0.0"] = D
    prs.api.calls.clear()
    prs.run_updates(NOW + COOLDOWN)
    prs.run_updates(NOW + COOLDOWN * 2)
    assert prs.api.refs[prs.branch] == head
    assert not prs.api.writes


def test_resolved_human_edit_to_definition_allows_next_update(prs: PullRequests):
    pr = prs.create()
    prs.human_commit()
    pr["state"] = "closed"
    prs.api.upstreams["upstream/example"]["v3.0.0"] = D
    assert not prs.run_updates(NOW + COOLDOWN).errors
    assert not prs.run_updates(NOW + COOLDOWN * 2).errors
    assert len(prs.api.prs) == 2


def test_unrelated_human_changes_are_not_carried_into_a_new_pr(prs: PullRequests):
    pr = prs.create()
    files = prs.api.files(prs.api.refs[prs.branch])
    files["README.md"] += "Unrelated human edit\n"
    head = prs.api.commit(files, [prs.api.refs[prs.branch]], "human")
    prs.api.set_head(prs.branch, head)
    pr["state"] = "closed"
    prs.api.calls.clear()
    result = prs.run_updates(NOW + COOLDOWN)
    assert any("frozen" in message for message in result.messages)
    assert not prs.api.writes


def test_deleted_branch_can_be_recreated_without_reopening_rejected_pr(
    prs: PullRequests,
):
    prs.create()["state"] = "closed"
    prs.run_updates(NOW + COOLDOWN)
    del prs.api.refs[prs.branch]
    prs.api.upstreams["upstream/example"]["v3.0.0"] = D
    prs.run_updates(NOW + COOLDOWN)
    assert len(prs.api.prs) == 1
    assert not prs.run_updates(NOW + COOLDOWN * 2).errors
    assert len(prs.api.prs) == 2
    assert prs.api.prs[2]["title"] == "toolchain: update example to 3.0.0"


def test_merge_preserves_observations_in_main_for_the_next_update(prs: PullRequests):
    pr = prs.create()
    prs.api.upstreams["upstream/example"]["v3.0.0"] = D
    prs.run_updates(NOW + COOLDOWN)
    pr["state"] = "closed"
    pr["merged_at"] = NOW.isoformat()
    prs.main = pr["head"]["sha"]
    prs.api.set_head("main", prs.main)
    prs.path.write_text(prs.api.files(prs.main)["toolchain/example/build.toml"])
    del prs.api.refs[prs.branch]
    result = prs.run_updates(NOW + COOLDOWN * 2)
    assert not result.errors
    assert len(prs.api.prs) == 2
    assert "from `2.0.0` to `3.0.0`" in prs.api.prs[2]["body"]


def test_dry_run_only_makes_reads(prs: PullRequests):
    result = prs.run_updates(dry_run=True)
    assert any("would record observations" in message for message in result.messages)
    assert not prs.api.writes
    prs.run_updates()
    head = prs.api.refs[prs.branch]
    prs.api.calls.clear()
    result = prs.run_updates(NOW + COOLDOWN, dry_run=True)
    assert any("would propose" in message for message in result.messages)
    assert not prs.api.writes
    assert prs.api.refs[prs.branch] == head


def test_failed_manifest_commit_prevents_pr_writes_and_can_be_retried(
    prs: PullRequests,
):
    prs.run_updates()
    head = prs.api.refs[prs.branch]
    prs.api.failures.append(("POST", "/graphql", "before"))
    result = prs.run_updates(NOW + COOLDOWN)
    assert result.errors
    assert not prs.api.prs
    assert prs.api.refs[prs.branch] == head
    assert not prs.run_updates(NOW + COOLDOWN).errors
    assert len(prs.api.prs) == 1


def test_uncertain_manifest_commit_recovers_from_branch_contents(prs: PullRequests):
    prs.run_updates()
    prs.api.failures.append(("POST", "/graphql", "after"))
    assert prs.run_updates(NOW + COOLDOWN).errors
    assert not prs.api.prs
    assert prs.branch_tool().source.commit == C
    head = prs.api.refs[prs.branch]
    assert not prs.run_updates(NOW + COOLDOWN).errors
    assert len(prs.api.prs) == 1
    assert prs.api.refs[prs.branch] == head


def test_uncertain_branch_creation_is_recovered_without_journaling(prs: PullRequests):
    prs.api.failures.append(("POST", f"/repos/{prs.api.repo}/git/refs", "after"))
    assert prs.run_updates().errors
    assert prs.api.refs[prs.branch] == prs.main
    assert not prs.run_updates().errors
    assert prs.api.observations()["v2.0.0"].first_seen == NOW


def test_interrupted_branch_initialization_survives_main_advancing(prs: PullRequests):
    prs.api.failures.append(("POST", f"/repos/{prs.api.repo}/git/refs", "after"))
    prs.run_updates()
    prs.main = prs.api.commit(prs.api.files(prs.main), [prs.main], "advance main")
    prs.api.set_head("main", prs.main)
    assert not prs.run_updates().errors
    assert prs.api.observations()


def test_uncertain_pr_creation_is_recovered_without_duplicates(prs: PullRequests):
    prs.run_updates()
    prs.api.failures.append(("POST", f"/repos/{prs.api.repo}/pulls", "after"))
    assert prs.run_updates(NOW + COOLDOWN).errors
    assert len(prs.api.prs) == 1
    prs.api.calls.clear()
    assert not prs.run_updates(NOW + COOLDOWN).errors
    assert not prs.api.writes
    assert len(prs.api.prs) == 1


def test_racing_human_commit_rejects_atomic_update(prs: PullRequests):
    prs.create()
    prs.api.upstreams["upstream/example"]["v3.0.0"] = D
    prs.run_updates(NOW + COOLDOWN)
    request = prs.api.request
    human = []

    def race(method, endpoint, *, data=None):
        if method == "POST" and endpoint == "/graphql":
            human.append(prs.human_commit())
        return request(method, endpoint, data=data)

    with patch.object(prs.api, "request", side_effect=race):
        assert prs.run_updates(NOW + COOLDOWN * 2).errors
    assert prs.api.refs[prs.branch] == human[0]
    prs.api.calls.clear()
    prs.run_updates(NOW + COOLDOWN * 2)
    assert not prs.api.writes


def test_ready_during_discovery_is_rechecked_before_writing(prs: PullRequests):
    pr = prs.create()
    prs.api.calls.clear()
    request = prs.api.request

    def become_ready(method, endpoint, *, data=None):
        if method == "GET" and endpoint == f"/repos/{prs.api.repo}/pulls/1":
            pr["draft"] = False
        return request(method, endpoint, data=data)

    with patch.object(prs.api, "request", side_effect=become_ready):
        result = prs.run_updates(NOW + COOLDOWN)
    assert not prs.api.writes
    assert any("waiting for reconciliation" in message for message in result.messages)


def test_candidate_change_prevents_pr_and_restarts_observation(prs: PullRequests):
    prs.run_updates()
    with patch.object(prs.api, "resolve_tag", side_effect=[A, A, D]):
        result = prs.run_updates(NOW + COOLDOWN)
    assert result.errors
    assert not prs.api.prs
    assert prs.branch_tool().source.commit == A
    observed = prs.api.observations()["v2.0.0"]
    assert (observed.commit, observed.first_seen) == (D, NOW + COOLDOWN)


def test_no_eligible_candidate_does_not_downgrade_an_open_draft(prs: PullRequests):
    prs.create()
    prs.api.upstreams["upstream/example"] = {"v1.9.0": A, "v3.0.0": D}
    result = prs.run_updates(NOW + COOLDOWN)
    assert not result.errors
    assert prs.branch_tool().source.commit == C
    assert set(prs.api.observations()) == {"v3.0.0"}
    assert prs.api.prs[1]["title"] == "toolchain: update example to 2.0.0"


def test_partial_failure_does_not_block_other_tools(prs: PullRequests):
    definition(prs.root, "other", "upstream/other")
    prs.api.upstreams["upstream/other"] = {"v1.9.0": A, "v1.10.0": B}
    prs.main = prs.api.initialize(prs.root)
    prs.run_updates()
    prs.api.failures.append(
        ("GET", "/repos/upstream/example/tags?per_page=100&page=1", "before")
    )
    result = prs.run_updates(NOW + COOLDOWN)
    assert len(result.errors) == 1
    assert len(prs.api.prs) == 1
    assert prs.api.prs[1]["title"] == "toolchain: update other to 1.10.0"


def test_stale_checkout_is_rejected(prs: PullRequests):
    prs.api.refs["main"] = prs.api.commit(
        prs.api.files(prs.main), [prs.main], "advance main"
    )
    with pytest.raises(ValueError, match="default branch head"):
        prs.run_updates()
    assert not prs.api.writes


def test_ready_for_review_selects_the_updated_tool(tmp_path: Path):
    root = tmp_path
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
            "subprocess.check_output", return_value=b"toolchain/example/build.toml\x00"
        ) as diff,
        redirect_stdout(StringIO()),
    ):
        runpy.run_path(str(ROOT / "scripts/ci-select-tools.py"), run_name="__main__")
    diff.assert_called_once()
    assert f"{A}...{B}" in diff.call_args.args[0]
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(values["tools"]) == ["example"]
    assert json.loads(values["matrix"])[0]["tool"] == "example"
