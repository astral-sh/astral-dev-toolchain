import pytest
from astral_dev_toolchain_zygote import _build


def test_build_root_reuses_the_path_but_not_the_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(_build, "gettempdir", lambda: str(tmp_path))

    with _build._build_root() as first:
        (first / "src").mkdir()
        with pytest.raises(FileExistsError), _build._build_root():
            pass
        assert (first / "src").is_dir()

    with _build._build_root() as second:
        assert second == first
        assert not (second / "src").exists()
