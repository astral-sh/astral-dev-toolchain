from pathlib import Path
from unittest.mock import patch

from astral_dev_toolchain_core import GoToolSpec, Tool, ToolSource
from astral_dev_toolchain_zygote._build_go import GoBuilder
from astral_dev_toolchain_zygote._plan import Plan


def test_dry_run_does_not_build_wheels(tmp_path):
    src = tmp_path / "src"
    (src / "cmd" / "example").mkdir(parents=True)
    (src / "go.mod").write_text("module example.com/example\n\ngo 1.26\n")
    (src / "README.md").write_text("# Example\n", encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    tool = Tool[GoToolSpec](
        tool=GoToolSpec(
            backend="go",
            name="example",
            repo="https://example.com/upstream",
            version="v1.2.3",
            directory="cmd/example",
        ),
        sources=(ToolSource(tag="v1.2.3", commit="a" * 40),),
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
        targets=("aarch64-apple-darwin",),
    )
    go = GoBuilder.prepare(plan, tool, tool.python_package_name)
    with patch("astral_dev_toolchain_zygote._build_go.build_wheels") as build_wheels:
        go.build(src, build)
    build_wheels.assert_not_called()
