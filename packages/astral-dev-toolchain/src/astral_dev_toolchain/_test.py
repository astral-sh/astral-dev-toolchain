import logging
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from astral_dev_toolchain_core import Tool, Toolchain, ToolTest

_TEST_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class Tester:
    toolchain: Toolchain
    dist_dir: Path
    uv_bin: str

    def run(self) -> bool:
        failures: list[str] = []
        passed = 0

        with TemporaryDirectory(prefix="astral-dev-toolchain-test-") as tempdir:
            temp = Path(tempdir)
            tool_dir = temp / "tools"
            bin_dir = temp / "bin"
            tool_dir.mkdir()
            bin_dir.mkdir()

            env = os.environ.copy()
            env["UV_TOOL_DIR"] = str(tool_dir)
            env["UV_TOOL_BIN_DIR"] = str(bin_dir)
            path = env.get("PATH")
            if path:
                env["PATH"] = os.pathsep.join((str(bin_dir), path))
            else:
                env["PATH"] = str(bin_dir)

            for tool in self.toolchain.tools:
                tool_passed, tool_failures = self._run_tool(tool, env)
                passed += tool_passed
                failures.extend(tool_failures)

        if failures:
            logger.error("%d smoke test failure(s):", len(failures))
            for failure in failures:
                logger.error("- %s", failure)
            return False

        logger.info("all %d smoke test(s) passed", passed)
        return True

    def _run_tool(self, tool: Tool, env: dict[str, str]) -> tuple[int, list[str]]:
        if not tool.tests:
            logger.info("skipping %s: no tests configured", tool.tool.name)
            return 0, []

        install = [
            self.uv_bin,
            "tool",
            "install",
            "--python",
            sys.executable,
            "--offline",
            "--no-index",
            "--find-links",
            str(self.dist_dir),
            "--no-cache",
            "--no-config",
            tool.python_package_name,
        ]
        logger.info("installing %s", tool.python_package_name)
        logger.debug("running: %s", shlex.join(install))
        result = subprocess.run(install, check=False, env=env)
        if result.returncode:
            return 0, [
                f"{tool.tool.name}: installation exited with {result.returncode}"
            ]

        passed = 0
        failures = []
        for test in tool.tests:
            if failure := self._run_test(tool, test, env):
                failures.append(failure)
            else:
                passed += 1

        return passed, failures

    @staticmethod
    def _run_test(tool: Tool, test: ToolTest, env: dict[str, str]) -> str | None:
        command = list(test.run)
        # Keep build.toml platform-neutral while resolving .exe names on Windows
        # through PATHEXT.
        executable = shutil.which(command[0], path=env["PATH"])
        if executable is None:
            return (
                f"{tool.tool.name} ({test.name}): executable not found: {test.run[0]}"
            )
        command[0] = executable
        logger.info(
            "testing %s (%s): %s",
            tool.tool.name,
            test.name,
            shlex.join(command),
        )
        try:
            result = subprocess.run(
                command,
                check=False,
                env=env,
                timeout=_TEST_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            return (
                f"{tool.tool.name} ({test.name}): executable not found: {test.run[0]}"
            )
        except subprocess.TimeoutExpired:
            return (
                f"{tool.tool.name} ({test.name}): timed out after "
                f"{_TEST_TIMEOUT_SECONDS} seconds"
            )

        if result.returncode != test.exitcode:
            return (
                f"{tool.tool.name} ({test.name}): expected exit code "
                f"{test.exitcode}, got {result.returncode}"
            )
        return None
