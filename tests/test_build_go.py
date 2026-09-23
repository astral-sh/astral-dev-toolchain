from pathlib import Path
from unittest.mock import patch

import pytest
from astral_dev_toolchain_core import GoToolSpec, Tool, ToolSource
from astral_dev_toolchain_zygote._build_go import GoBuilder
from astral_dev_toolchain_zygote._metadata import readme_notice
from astral_dev_toolchain_zygote._plan import Plan

README = "# Example\n\nUpstream documentation: café.\n"


@pytest.fixture
def builder(tmp_path):
    src = tmp_path / "src"
    (src / "cmd" / "example").mkdir(parents=True)
    (src / "go.mod").write_text("module example.com/example\n\ngo 1.26\n")
    (src / "README.md").write_text(README, encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    tool = Tool[GoToolSpec](
        tool=GoToolSpec(
            backend="go",
            name="example",
            version="v1.2.3",
            rebuild=2,
            directory="cmd/example",
            version_var="main.version",
        ),
        source=ToolSource(
            repo="https://example.com/upstream", tag="v1.2.3", commit="a" * 40
        ),
    )
    plan = Plan(
        git_bin=Path("git"),
        cargo_bin=Path("cargo"),
        maturin_bin=Path("maturin"),
        go_bin=Path("go"),
        toolchain_dir=tmp_path,
        output_dir=tmp_path / "dist",
        dry_run=False,
        selected_tools=set(),
        targets=("aarch64-apple-darwin", "x86_64-pc-windows-msvc"),
    )
    return GoBuilder.prepare(plan, tool, tool.python_package_name), src, build


def test_build_passes_package_metadata_and_selected_targets(builder):
    go, src, build = builder
    with patch(
        "astral_dev_toolchain_zygote._build_go.build_wheels", return_value=[]
    ) as build_wheels:
        go.build(src, build)

    build_wheels.assert_called_once_with(
        src.resolve(),
        package_path="cmd/example",
        name="astral-dev-toolchain-example",
        version="1.2.3.post2",
        output_dir=go.plan.output_dir,
        entry_point="example",
        platforms=["darwin-arm64", "windows-amd64"],
        go_binary="go",
        ldflags="-X main.version=v1.2.3",
        readme=(build / "README.md").resolve(),
    )
    assert (build / "README.md").read_text(encoding="utf-8") == (
        readme_notice(go.tool) + "\n---\n\n" + README
    )
    assert (src / "README.md").read_text(encoding="utf-8") == README


def test_custom_readme(builder):
    go, src, build = builder
    go.tool.tool.readme = "docs/README.md"
    (src / "docs").mkdir()
    (src / go.tool.tool.readme).write_text("# Custom README\n", encoding="utf-8")
    with patch(
        "astral_dev_toolchain_zygote._build_go.build_wheels", return_value=[]
    ) as build_wheels:
        go.build(src, build)
    readme = build_wheels.call_args.kwargs["readme"]
    assert readme.read_text(encoding="utf-8") == (
        readme_notice(go.tool) + "\n---\n\n# Custom README\n"
    )


def test_dry_run_does_not_build_wheels(builder):
    go, src, build = builder
    go.plan.dry_run = True
    with patch("astral_dev_toolchain_zygote._build_go.build_wheels") as build_wheels:
        go.build(src, build)
    build_wheels.assert_not_called()
    assert (build / "README.md").is_file()
