"""Target selection coverage without Docker, network access, or compilation."""

import argparse
import io
import sys
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from astral_dev_toolchain_core import GoToolSpec, RustToolSpec, Tool, ToolSource
from astral_dev_toolchain_zygote import _cli as container_cli
from astral_dev_toolchain_zygote._build_go import GoBuilder
from astral_dev_toolchain_zygote._build_rust import RustBuilder
from astral_dev_toolchain_zygote._config import ImageConfig
from astral_dev_toolchain_zygote._plan import Plan, PlanError

from astral_dev_toolchain import _cli as host_cli

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "image/toolchain.toml"
CONFIG = ImageConfig.load(CONFIG_PATH)


def load_plan(targets: list[str]) -> Plan:
    args = argparse.Namespace(
        toolchain_dir=ROOT / "toolchain",
        output_dir=ROOT,
        select=["cargo-nextest"],
        target=targets,
        dry_run=False,
    )
    with patch("astral_dev_toolchain_zygote._plan._which", side_effect=Path):
        return Plan.load(args, CONFIG)


class TargetSelectionTests(unittest.TestCase):
    def test_selection(self) -> None:
        first, second, *_ = CONFIG.targets
        for selected, expected in (
            ([], CONFIG.targets),
            ([second], (second,)),
            ([second, first], (first, second)),
            ([second, first, second], (first, second)),
        ):
            with self.subTest(selected=selected):
                self.assertEqual(load_plan(selected).targets, expected)

    def test_unknown_target(self) -> None:
        with self.assertRaises(PlanError) as raised:
            load_plan([CONFIG.targets[0], "unsupported-triple"])
        self.assertIn("unsupported-triple", str(raised.exception))
        for target in CONFIG.targets:
            self.assertIn(target, str(raised.exception))


class CLITests(unittest.TestCase):
    def test_host_forwards_targets_and_dry_run(self) -> None:
        for targets in ([], [CONFIG.targets[1], CONFIG.targets[0], CONFIG.targets[1]]):
            for dry_run in (False, True):
                with (
                    self.subTest(targets=targets, dry_run=dry_run),
                    TemporaryDirectory() as output,
                ):
                    build_args = [
                        "build",
                        *(["--dry-run"] if dry_run else []),
                        "--select",
                        "cargo-nextest",
                    ]
                    for target in targets:
                        build_args.extend(["--target", target])
                    argv = [
                        "astral-dev-toolchain",
                        "--toolchain-dir",
                        str(ROOT / "toolchain"),
                        *build_args,
                        "--output-dir",
                        output,
                    ]
                    with (
                        patch.object(sys, "argv", argv),
                        patch.object(
                            host_cli, "_build_zygote", return_value="test-image"
                        ),
                        patch.object(
                            host_cli.shutil, "which", return_value="/bin/docker"
                        ),
                        patch.object(host_cli.os, "execv") as execute,
                    ):
                        host_cli.run()
                    docker, command = execute.call_args.args
                    self.assertEqual(docker, "/bin/docker")
                    self.assertEqual(
                        command[command.index("test-image") + 1 :], build_args
                    )

    def test_container_passes_filtered_plan_to_builder(self) -> None:
        argv = [
            "astral-dev-toolchain-zygote",
            "--config",
            str(CONFIG_PATH),
            "--toolchain-dir",
            str(ROOT / "toolchain"),
            "build",
            "--output-dir",
            str(ROOT),
            "--select",
            "cargo-nextest",
            "--dry-run",
            "--target",
            CONFIG.targets[1],
            "--target",
            CONFIG.targets[0],
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("astral_dev_toolchain_zygote._plan._which", side_effect=Path),
            patch.object(container_cli, "Builder") as builder,
        ):
            container_cli.run()
        builder.assert_called_once()
        plan = builder.call_args.kwargs["plan"]
        self.assertEqual(plan.targets, CONFIG.targets[:2])
        self.assertTrue(plan.dry_run)
        builder.return_value.build.assert_called_once_with()

    def test_invalid_target_is_cli_error_before_loading_tools(self) -> None:
        argv = [
            "astral-dev-toolchain-zygote",
            "--config",
            str(CONFIG_PATH),
            "--toolchain-dir",
            str(ROOT / "toolchain"),
            "build",
            "--output-dir",
            str(ROOT),
            "--target",
            "unsupported-triple",
        ]
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.object(container_cli.Toolchain, "load") as load_tools,
            patch.object(container_cli, "Builder") as builder,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            container_cli.run()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unknown build targets: unsupported-triple", stderr.getvalue())
        for target in CONFIG.targets:
            self.assertIn(target, stderr.getvalue())
        load_tools.assert_not_called()
        builder.assert_not_called()


class BackendTests(unittest.TestCase):
    def test_rust_builds_only_selected_targets(self) -> None:
        selected = list(CONFIG.targets[::2])
        tool = Tool(
            tool=RustToolSpec(backend="rust", name="example"),
            source=ToolSource(repo="unused", tag="unused", commit="unused"),
        )
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                plan = replace(load_plan(selected), dry_run=dry_run)
                builder = RustBuilder.prepare(plan, tool, tool.python_package_name)
                with (
                    patch.object(RustBuilder, "_prepare"),
                    patch(
                        "astral_dev_toolchain_zygote._build_rust.subprocess.run"
                    ) as run,
                    patch("astral_dev_toolchain_zygote._build_rust.logger.info") as log,
                ):
                    builder.build(ROOT, ROOT)
                if dry_run:
                    run.assert_not_called()
                    commands = [call.args[1] for call in log.call_args_list]
                    self.assertEqual(len(commands), len(selected))
                    for command, target in zip(commands, selected, strict=True):
                        self.assertIn(f"--target {target}", command)
                else:
                    commands = [call.args[0] for call in run.call_args_list]
                    self.assertEqual(
                        [
                            command[command.index("--target") + 1]
                            for command in commands
                        ],
                        selected,
                    )

    def test_go_builds_only_selected_targets(self) -> None:
        selected = ["x86_64-unknown-linux-gnu", "aarch64-pc-windows-msvc"]
        tool = Tool(
            tool=GoToolSpec(backend="go", name="example", version="1.0.0"),
            source=ToolSource(repo="unused", tag="unused", commit="unused"),
        )
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                plan = replace(load_plan(selected), dry_run=dry_run)
                builder = GoBuilder.prepare(plan, tool, tool.python_package_name)
                with (
                    patch(
                        "astral_dev_toolchain_zygote._build_go.compile_go_binary"
                    ) as compile,
                    patch("astral_dev_toolchain_zygote._build_go.build_wheel") as wheel,
                    patch.object(GoBuilder, "_log_dry_run") as log,
                ):
                    builder.build(ROOT, ROOT)
                if dry_run:
                    compile.assert_not_called()
                    wheel.assert_not_called()
                    platforms = [call.args[2] for call in log.call_args_list]
                    self.assertEqual(
                        [(platform.goos, platform.goarch) for platform in platforms],
                        [("linux", "amd64"), ("windows", "arm64")],
                    )
                else:
                    self.assertEqual(
                        [
                            (call.kwargs["goos"], call.kwargs["goarch"])
                            for call in compile.call_args_list
                        ],
                        [("linux", "amd64"), ("windows", "arm64")],
                    )
                    self.assertEqual(
                        [call.kwargs["platform_tag"] for call in wheel.call_args_list],
                        ["manylinux_2_17_x86_64", "win_arm64"],
                    )


if __name__ == "__main__":
    unittest.main()
