import logging
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from astral_dev_toolchain_core import Toolchain

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
            env["PATH"] = os.pathsep.join(
                part for part in (str(bin_dir), env.get("PATH")) if part
            )

            for tool in self.toolchain.tools:
                if not tool.tests:
                    logger.info("skipping %s: no tests configured", tool.tool.name)
                    continue

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
                    failures.append(
                        f"{tool.tool.name}: installation exited with "
                        f"{result.returncode}"
                    )
                    continue

                for test in tool.tests:
                    command = list(test.run)
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
                        failures.append(
                            f"{tool.tool.name} ({test.name}): executable not found: "
                            f"{test.run[0]}"
                        )
                        continue
                    except subprocess.TimeoutExpired:
                        failures.append(
                            f"{tool.tool.name} ({test.name}): timed out after "
                            f"{_TEST_TIMEOUT_SECONDS} seconds"
                        )
                        continue

                    if result.returncode != test.exitcode:
                        failures.append(
                            f"{tool.tool.name} ({test.name}): expected exit code "
                            f"{test.exitcode}, got {result.returncode}"
                        )
                    else:
                        passed += 1

        if failures:
            logger.error("%d smoke test failure(s):", len(failures))
            for failure in failures:
                logger.error("- %s", failure)
            return False

        logger.info("all %d smoke test(s) passed", passed)
        return True
