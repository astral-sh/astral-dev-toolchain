import pytest
from astral_dev_toolchain._git import Git


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_tag_listing_and_rechecks(repo_factory, object_format):
    repo = repo_factory(object_format=object_format)
    first, _, latest, _ = repo.commits
    repo.tag("inner", latest, message="Inner annotation")
    repo.tag("project/v2.0.0", "inner", message="Outer annotation")
    git = Git()
    assert git.tags(repo.url, "project/") == {"project/v2.0.0": latest}
    assert git.resolve_tags(
        str(repo.path), ("v1.9.0", "project/v2.0.0", "missing")
    ) == {
        "v1.9.0": first,
        "project/v2.0.0": latest,
    }
