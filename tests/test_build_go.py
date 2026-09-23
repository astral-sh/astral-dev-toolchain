import logging
import subprocess
import sys
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest
from astral_dev_toolchain_core import GoToolSpec, RustToolSpec, Tool, ToolSource
from astral_dev_toolchain_zygote._build_go import GoBuilder
from astral_dev_toolchain_zygote._build_rust import RustBuilder, _CargoPackage
from astral_dev_toolchain_zygote._metadata import readme_notice
from astral_dev_toolchain_zygote._plan import Plan

TARGETS = {
    "aarch64-apple-darwin": ("darwin", "arm64", "macosx_13_0_arm64"),
    "x86_64-apple-darwin": ("darwin", "amd64", "macosx_13_0_x86_64"),
    "aarch64-unknown-linux-gnu": ("linux", "arm64", "manylinux_2_17_aarch64"),
    "x86_64-unknown-linux-gnu": ("linux", "amd64", "manylinux_2_17_x86_64"),
    "aarch64-pc-windows-msvc": ("windows", "arm64", "win_arm64"),
    "x86_64-pc-windows-msvc": ("windows", "amd64", "win_amd64"),
}
README = "# Example\n\nUpstream documentation: café.\n"


def make_builder(tmp_path, **settings):
    src = tmp_path / "src"
    (src / "cmd" / "example").mkdir(parents=True)
    (src / "go.mod").write_text("module example.com/example\n\ngo 1.26\n")
    (src / "README.md").write_text(README, encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    tool = Tool[GoToolSpec](
        tool=GoToolSpec.model_validate(
            {
                "backend": "go",
                "name": "example",
                "version": "v1.2.3",
                "rebuild": 2,
                "directory": "cmd/example",
                "version_var": "main.version",
            }
            | settings
        ),
        source=ToolSource(
            repo="https://example.com/upstream", tag="v1.2.3", commit="a" * 40
        ),
    )
    plan = Plan(
        git_bin=Path("git"),
        cargo_bin=Path("cargo"),
        maturin_bin=Path("maturin"),
        go_bin=Path(sys.executable),
        toolchain_dir=tmp_path,
        output_dir=tmp_path / "dist",
        dry_run=False,
        selected_tools=set(),
        targets=tuple(TARGETS),
    )
    return GoBuilder.prepare(plan, tool, tool.python_package_name), src, build


@pytest.fixture
def go_calls(monkeypatch):
    # Exercise radlermass's real wheel writer without requiring Go in unit CI.
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert command[0] == sys.executable
        if command[1:] == ["env", "GOVERSION"]:
            return subprocess.CompletedProcess(command, 0, stdout="go1.27.1\n")
        assert command[1] == "build"
        env = kwargs["env"]
        binary = Path(command[command.index("-o") + 1])
        binary.write_bytes(f"{env['GOOS']}/{env['GOARCH']}".encode())
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_wheels_for_all_targets(tmp_path, go_calls):
    builder, src, build = make_builder(tmp_path)
    builder.build(src, build)

    stem = "astral_dev_toolchain_example-1.2.3.post2"
    assert len(list(builder.plan.output_dir.glob("*.whl"))) == len(TARGETS)
    for goos, goarch, tag in TARGETS.values():
        with zipfile.ZipFile(
            builder.plan.output_dir / f"{stem}-py3-none-{tag}.whl"
        ) as wheel:
            command = "example.exe" if goos == "windows" else "example"
            script = f"{stem}.data/scripts/{command}"
            assert wheel.read(script) == f"{goos}/{goarch}".encode()
            assert wheel.getinfo(script).external_attr >> 16 & 0o777 == 0o755
            assert not any(name.endswith(".py") for name in wheel.namelist())
            metadata = Parser().parsestr(
                wheel.read(f"{stem}.dist-info/METADATA").decode("utf-8")
            )
            assert metadata["Name"] == builder.package_name
            assert metadata["Version"] == "1.2.3.post2"
            assert metadata["Description-Content-Type"] == "text/markdown"
            assert metadata["Requires-Dist"] is None
            assert metadata["Requires-Python"] is None
            assert (
                metadata.get_payload()
                == readme_notice(builder.tool) + "\n---\n\n" + README
            )

    builds = [(command, args) for command, args in go_calls if command[1] == "build"]
    assert len(builds) == len(TARGETS)
    for command, args in builds:
        assert command[-1] == "./cmd/example"
        assert "-ldflags=-s -w -X main.version=v1.2.3" in command
        assert args["cwd"] == src.resolve()
        assert args["env"]["CGO_ENABLED"] == "0"
    assert (src / "README.md").read_text(encoding="utf-8") == README


@pytest.mark.parametrize("directory", [".", "cmd/example"])
def test_package_in_nested_module_and_custom_readme(tmp_path, go_calls, directory):
    builder, src, build = make_builder(
        tmp_path, directory=directory, readme="docs/README.MD", version_var=None
    )
    module = src / directory
    (module / "go.mod").write_text("module example.com/nested\n\ngo 1.26\n")
    (src / "docs").mkdir()
    (src / "docs" / "README.MD").write_text("# Custom README\n", encoding="utf-8")
    builder.plan.targets = ("x86_64-pc-windows-msvc",)
    builder.build(src, build)
    [(command, args)] = go_calls
    assert args["cwd"] == module.resolve()
    assert command[-1] == "./."
    assert "-ldflags=-s -w" in command
    [wheel_path] = builder.plan.output_dir.glob("*.whl")
    with zipfile.ZipFile(wheel_path) as wheel:
        [metadata] = [name for name in wheel.namelist() if name.endswith("/METADATA")]
        assert wheel.read(metadata).decode("utf-8").endswith("# Custom README\n")


def test_dry_run_prepares_readme_without_go(tmp_path, go_calls, caplog):
    builder, src, build = make_builder(tmp_path)
    builder.plan.dry_run = True
    with caplog.at_level(logging.INFO):
        builder.build(src, build)
    assert not go_calls
    assert not builder.plan.output_dir.exists()
    assert "astral-dev-toolchain-example 1.2.3.post2" in caplog.text
    assert (build / "README.md").read_text(encoding="utf-8").endswith(README)


@pytest.mark.parametrize("field", ["directory", "readme"])
@pytest.mark.parametrize("escape", ["parent", "absolute", "symlink"])
def test_paths_stay_in_checkout(tmp_path, go_calls, field, escape):
    outside = tmp_path / "outside"
    outside.mkdir()
    value = {"parent": "../outside", "absolute": str(outside), "symlink": "link"}[
        escape
    ]
    builder, src, build = make_builder(tmp_path, **{field: value})
    (src / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match=f"Go tool.{field} must be relative"):
        builder.build(src, build)
    assert not go_calls


@pytest.mark.parametrize(
    "settings, error",
    [
        ({"directory": "missing"}, "Go package directory does not exist"),
        ({"readme": "missing.md"}, "package README does not exist"),
        ({"readme": "go.mod"}, "unsupported README format"),
    ],
)
def test_missing_or_unsupported_source(tmp_path, go_calls, settings, error):
    builder, src, build = make_builder(tmp_path, **settings)
    with pytest.raises(ValueError, match=error):
        builder.build(src, build)
    assert not go_calls


def test_module_search_stops_at_checkout(tmp_path, go_calls):
    builder, src, build = make_builder(tmp_path)
    (src / "go.mod").rename(tmp_path / "go.mod")
    with pytest.raises(ValueError, match="no go.mod found"):
        builder.build(src, build)
    assert not go_calls


def test_unknown_target_fails_before_building(tmp_path, go_calls):
    builder, src, build = make_builder(tmp_path)
    builder.plan.targets += ("unsupported",)
    with pytest.raises(ValueError, match="unsupported Go build target"):
        builder.build(src, build)
    assert not go_calls
    assert not builder.plan.output_dir.exists()


def test_failed_build_preserves_existing_wheels(tmp_path, go_calls, monkeypatch):
    builder, src, build = make_builder(tmp_path)
    builder.plan.targets = ("x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc")
    builder.plan.output_dir.mkdir()
    existing = builder.plan.output_dir / (
        "astral_dev_toolchain_example-1.2.3.post2-py3-none-manylinux_2_17_x86_64.whl"
    )
    existing.write_bytes(b"previous build")
    run = subprocess.run

    def fail_windows(command, **kwargs):
        if kwargs.get("env", {}).get("GOOS") == "windows":
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="compilation failed"
            )
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_windows)
    with pytest.raises(RuntimeError, match="compilation failed"):
        builder.build(src, build)
    assert existing.read_bytes() == b"previous build"
    assert list(builder.plan.output_dir.iterdir()) == [existing]


def test_rust_and_go_use_same_readme(tmp_path, monkeypatch):
    go, src, build = make_builder(tmp_path)
    go.plan.dry_run = True
    go.build(src, build)
    go_readme = (build / "README.md").read_bytes()
    rust = RustBuilder.prepare(
        go.plan,
        Tool[RustToolSpec](
            tool=RustToolSpec(backend="rust", name="example"), source=go.tool.source
        ),
        go.package_name,
    )
    monkeypatch.setattr(
        RustBuilder,
        "_manifest_metadata",
        lambda *_: _CargoPackage(version="1.2.3", readme=src / "README.md"),
    )
    rust._prepare(src, build)
    assert (build / "README.md").read_bytes() == go_readme
    pyproject = tomllib.loads((build / "pyproject.toml").read_text())
    assert pyproject["project"]["readme"] == "README.md"
