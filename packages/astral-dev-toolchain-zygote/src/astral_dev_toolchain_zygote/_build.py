import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import tomli_w

from ._plan import Plan
from ._tool import Tool

_PYPROJECT_SKELETON: dict = {
    "build-system": {"requires": ["maturin>=1,<2"], "build-backend": "maturin"},
    "tool": {"maturin": {"bindings": "bin"}},
}

logger = logging.getLogger(__name__)


@dataclass
class Builder:
    plan: Plan
    tool: Tool

    def _prepare(self) -> Path:
        with TemporaryDirectory(delete=False) as tempdir:
            tempdir = Path(tempdir)
            builder_dir = tempdir / "builder"
            builder_dir.mkdir()
            src_dir = tempdir / "src"
            src_dir.mkdir()

            subprocess.run(
                [
                    "git",
                    "clone",
                    "--branch",
                    self.tool.source.tag,
                    "--depth",
                    "1",
                    self.tool.source.repo,
                    ".",
                ],
                check=True,
                cwd=src_dir,
            )
            # TODO: Confirm the checkout matches `self.tool.source.commit`.

            pyproject = dict(**_PYPROJECT_SKELETON)
            pyproject["project"] = {"name": self.tool.python_package_name}

            if build := self.tool.build:
                manifest_path = src_dir / build.manifest
            else:
                manifest_path = src_dir / "Cargo.toml"

            pyproject["tool"]["maturin"]["manifest-path"] = str(manifest_path)
            (builder_dir / "pyproject.toml").write_text(tomli_w.dumps(pyproject))

        return tempdir

    def build(self) -> None:
        prepared_dir = self._prepare()

        for target in self.plan.targets:
            command = [self.plan.maturin_bin, "build"]

            if self.tool.build and self.tool.build.no_default_features:
                command.append("--no-default-features")

            command.extend(
                [
                    "--release",
                    "--target",
                    target,
                    "--zig",
                    "--out",
                    self.plan.output_dir,
                ]
            )

            if self.plan.dry_run:
                logger.info(
                    "[dry-run] would have run: '%s' in '%s'",
                    shlex.join([str(arg) for arg in command]),
                    prepared_dir / "builder",
                )
            else:
                subprocess.run(command, check=True, cwd=prepared_dir / "builder")
