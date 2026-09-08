from datetime import timedelta
from unittest.mock import patch

import pytest
from astral_dev_toolchain._git import Git
from astral_dev_toolchain._update import COOLDOWN, definitions, scan, tag_version
from astral_dev_toolchain_core import Tool
from helpers import NOW
from packaging.version import Version


def observations(path):
    update = Tool.load(path).update
    assert update is not None
    return update.observed


@pytest.mark.parametrize(
    "tag, prefix, expected",
    [
        ("1.9.0", "", "1.9.0"),
        ("v1.10.0", "", "1.10.0"),
        ("project/v2.0.0", "project/", "2.0.0"),
        ("other/v2.0.0", "project/", None),
        ("v2.0.0-rc.1", "", None),
        ("2.0.0+local", "", None),
        ("2.0", "", None),
        ("02.0.0", "", None),
    ],
)
def test_tag_versions(tag, prefix, expected):
    assert tag_version(tag, prefix) == (Version(expected) if expected else None)


@pytest.mark.parametrize("go", [False, True], ids=["rust", "go"])
def test_update_lifecycle(tool_factory, upstream, update, go):
    path = tool_factory(go=go)
    assert not update().errors
    assert set(observations(path)) == {"v1.10.0", "v2.0.0"}
    # The fixture's commits are from 2000; only our observation starts the clock.
    assert observations(path)["v2.0.0"].first_seen == NOW
    assert Tool.load(path).source.tag == "v1.9.0"

    before = path.read_bytes()
    assert not update(now=NOW + COOLDOWN - timedelta(microseconds=1)).errors
    assert path.read_bytes() == before
    assert not update(now=NOW + COOLDOWN, dry_run=True).errors
    assert path.read_bytes() == before

    assert not update(now=NOW + COOLDOWN).errors
    tool = Tool.load(path)
    assert (tool.source.tag, tool.source.commit) == ("v2.0.0", upstream.commits[2])
    assert tool.tool.version == ("2.0.0" if go else None)
    assert "# Keep this comment." in path.read_text()
    assert "rebuild" not in path.read_text()
    assert tool.tests[0].run == ("example", "--help")

    before = path.read_bytes()
    assert not update(now=NOW + COOLDOWN).errors
    assert path.read_bytes() == before


def test_highest_eligible_version(tool_file, upstream, update):
    upstream.delete_tag("v2.0.0")
    assert not update().errors
    upstream.tag("v2.0.0", upstream.commits[2])
    assert not update(now=NOW + COOLDOWN).errors
    assert Tool.load(tool_file).source.tag == "v1.10.0"
    assert observations(tool_file)["v2.0.0"].first_seen == NOW + COOLDOWN


def test_candidate_observation_lifecycle(observed_tool, upstream, update):
    upstream.tag("v2.0.0", upstream.commits[2], message="New annotation")
    assert not update(now=NOW + timedelta(days=1)).errors
    assert observations(observed_tool)["v2.0.0"].first_seen == NOW

    upstream.tag("v2.0.0", upstream.commits[3])
    upstream.delete_tag("v1.10.0")
    assert not update(now=NOW + COOLDOWN).errors
    observed = observations(observed_tool)
    assert set(observed) == {"v2.0.0"}
    assert (observed["v2.0.0"].commit, observed["v2.0.0"].first_seen) == (
        upstream.commits[3],
        NOW + COOLDOWN,
    )
    assert Tool.load(observed_tool).source.tag == "v1.9.0"

    upstream.tag("v1.10.0", upstream.commits[1])
    assert not update(now=NOW + COOLDOWN).errors
    assert observations(observed_tool)["v1.10.0"].first_seen == NOW + COOLDOWN
    upstream.delete_tag("v1.10.0")
    upstream.delete_tag("v2.0.0")
    assert not update(now=NOW + COOLDOWN).errors
    assert observations(observed_tool) == {}


@pytest.mark.parametrize("failure", ["moved pin", "deleted pin", "unavailable repo"])
def test_upstream_failure_preserves_observations_and_continues(
    observed_tool, upstream, tool_factory, repo_factory, update, failure
):
    tool_factory("other", repo=repo_factory("other.git"))
    assert not update().errors
    before = observed_tool.read_bytes()
    if failure == "moved pin":
        upstream.tag("v1.9.0", upstream.commits[3])
    elif failure == "deleted pin":
        upstream.delete_tag("v1.9.0")
    else:
        upstream.path.rename(upstream.path.with_suffix(".offline"))
    result = update(now=NOW + COOLDOWN)
    assert len(result.errors) == 1
    assert observed_tool.read_bytes() == before
    assert [d.name for d in result.discoveries if d.proposal] == ["other"]


@pytest.mark.parametrize("tag", ["v1.9.0", "v2.0.0"], ids=["pin", "candidate"])
@pytest.mark.parametrize("change", ["move", "delete"])
def test_tag_changes_before_writing(
    observed_tool, upstream, update, before_recheck, tag, change
):
    before = observed_tool.read_bytes()
    before_recheck(
        lambda: (
            upstream.tag(tag, upstream.commits[3])
            if change == "move"
            else upstream.delete_tag(tag)
        )
    )
    result = update(now=NOW + COOLDOWN)
    assert result.errors
    assert Tool.load(observed_tool).source.commit == upstream.commits[0]
    if tag == "v1.9.0":
        assert observed_tool.read_bytes() == before
    elif change == "delete":
        assert tag not in observations(observed_tool)
    else:
        assert observations(observed_tool)[tag].first_seen == NOW + COOLDOWN


def test_human_edits_are_preserved(observed_tool, update, before_recheck):
    edited = observed_tool.read_text() + "\n# Human edit\n"
    before_recheck(lambda: observed_tool.write_text(edited))
    result = update(now=NOW + COOLDOWN)
    assert result.errors
    assert observed_tool.read_text() == edited


def test_shared_repository_failure_is_atomic(observed_tool, tool_factory, update):
    other = tool_factory("other", go=True)
    other.write_text(
        other.read_text().replace('version = "1.9.0"', 'version = "9.0.0"')
    )
    assert not update().errors
    before = {path: path.read_bytes() for path in (observed_tool, other)}
    result = update(now=NOW + COOLDOWN)
    assert len(result.errors) == 1
    assert "explicit tool.version" in result.errors[0]
    assert not result.discoveries
    assert all(path.read_bytes() == content for path, content in before.items())


def test_tools_share_discovery_and_respect_prefixes(tool_factory, upstream, update):
    paths = []
    for name in ("first", "second"):
        prefix = f"{name}/"
        paths.append(tool_factory(name, prefix=prefix))
        upstream.tag(f"{prefix}v1.9.0", upstream.commits[0])
        upstream.tag(f"{prefix}v2.0.0", upstream.commits[2])
    git = Git()
    with patch.object(git, "tags", wraps=git.tags) as tags:
        assert not update(git=git).errors
        assert tags.call_count == 1
    assert not update(now=NOW + COOLDOWN).errors
    assert [Tool.load(path).source.tag for path in paths] == [
        "first/v2.0.0",
        "second/v2.0.0",
    ]


def test_new_repository_restarts_observations(
    observed_tool, tool_factory, repo_factory
):
    previous = Tool.load(observed_tool)
    tool_factory(repo=repo_factory("new.git"))
    result = scan(
        definitions(observed_tool.parent.parent),
        Git(),
        NOW + COOLDOWN,
        previous={"example": previous},
    )
    assert not result.errors
    (discovery,) = result.discoveries
    assert discovery.proposal is None
    assert discovery.observed["v2.0.0"].first_seen == NOW + COOLDOWN


def test_selection_and_disabled_tools(tool_file, tool_factory, update):
    manual = tool_factory("manual")
    manual.write_text(manual.read_text().replace("enabled = true", "enabled = false"))
    result = update(selected=["manual"])
    assert not result.errors
    assert not result.discoveries
    with pytest.raises(ValueError, match="unknown tools"):
        update(selected=["missing"])
    assert [
        d.path for d in definitions(tool_file.parent.parent, ["example", "example"])
    ] == [tool_file]
