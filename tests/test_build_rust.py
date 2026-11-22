from pathlib import Path

import pytest
from astral_dev_toolchain_core import RustToolSpec, Tool, ToolSource
from astral_dev_toolchain_zygote._build_rust import RustBuilder
from astral_dev_toolchain_zygote._plan import Plan


@pytest.mark.parametrize(
    ("defaults", "maturin", "override", "expected"),
    [
        (["cli"], "other", None, "cli"),
        (["cli", "lib"], "cli", None, "cli"),
        (["cli", "lib"], None, None, None),
        (["cli", "lib"], "other", None, None),
        (["cli"], "cli", "other/Cargo.toml", "other"),
    ],
)
def test_workspace_package_selection(tmp_path, defaults, maturin, override, expected):
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["cli", "lib", "other"]\n'
        f"default-members = {defaults!r}\n"
    )
    for name in ("cli", "lib", "other"):
        member = tmp_path / name
        (member / "src").mkdir(parents=True)
        (member / "src/main.rs").write_text("fn main() {}")
        (member / "README.md").write_text(name)
        (member / "Cargo.toml").write_text(
            f'[package]\nname = "{name}"\nversion = "1.0.0"\n'
        )
    if maturin:
        (tmp_path / "pyproject.toml").write_text(
            f'[tool.maturin]\nmanifest-path = "{maturin}/Cargo.toml"\n'
        )

    tool = Tool[RustToolSpec](
        tool=RustToolSpec(
            backend="rust",
            name="cli",
            repo="https://example.com/cli",
            manifest=override,
        ),
        sources=(ToolSource(tag="1.0.0", commit="a" * 40),),
    )
    plan = Plan(
        git_bin=Path("git"),
        cargo_bin=Path("cargo"),
        maturin_bin=Path("maturin"),
        go_bin=Path("go"),
        toolchain_dir=tmp_path,
        output_dir=tmp_path / "dist",
        dry_run=True,
        selected_tools=set(),
        targets=(),
    )
    builder = RustBuilder.prepare(plan, tool, tool.python_package_name)
    if expected is None:
        with pytest.raises(ValueError, match=r"Set tool\.manifest"):
            builder._manifest_metadata(tmp_path)
    else:
        package = builder._manifest_metadata(tmp_path)
        assert package.manifest == tmp_path / expected / "Cargo.toml"
        assert package.readme == tmp_path / expected / "README.md"
