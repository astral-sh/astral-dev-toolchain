"""Build Rust tools with maturin."""

import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import tomli_w
from packaging.version import Version

from ._plan import Plan
from ._tool import RustToolSpec, Tool

logger = logging.getLogger(__name__)


@dataclass
class RustBuilder:
    plan: Plan
    tool: Tool[RustToolSpec]
    package_name: str

    @classmethod
    def prepare(
        cls, plan: Plan, tool: Tool[RustToolSpec], package_name: str
    ) -> RustBuilder:
        return cls(plan=plan, tool=tool, package_name=package_name)

    def build(self, src_dir: Path, build_dir: Path) -> None:
        self._prepare(src_dir, build_dir)

        command = [self.plan.maturin_bin, "build", "--release", "--zig"]
        if self.tool.tool.no_default_features:
            command.append("--no-default-features")
        command.extend(["--out", self.plan.output_dir])

        if self.plan.dry_run and self.tool.tool.env:
            logger.info("[dry-run] environment overrides: %r", self.tool.tool.env)

        for target in self.plan.targets:
            target_command = [*command, "--target", target]
            if self.plan.dry_run:
                logger.info(
                    "[dry-run] would have run: '%s' in '%s'",
                    shlex.join([str(arg) for arg in target_command]),
                    build_dir,
                )
            else:
                subprocess.run(
                    target_command,
                    check=True,
                    cwd=build_dir,
                    env=os.environ | self.tool.tool.env,
                )

    def _prepare(self, src_dir: Path, build_dir: Path) -> None:
        manifest = src_dir / self.tool.tool.manifest

        raw_version = self.tool.tool.version or self._manifest_version(
            src_dir, manifest
        )
        version = Version(raw_version)
        if self.tool.tool.rebuild:
            version = version.__replace__(post=self.tool.tool.rebuild)

        pyproject = {
            "build-system": {
                "requires": ["maturin>=1,<2"],
                "build-backend": "maturin",
            },
            "project": {
                "name": self.package_name,
                "version": str(version),
                "maintainers": [
                    {"name": "Astral", "email": "hey@astral.sh"},
                ],
                "dynamic": [
                    "description",
                    "keywords",
                    "license",
                    "readme",
                    "urls",
                ],
            },
            "tool": {"maturin": {"bindings": "bin", "manifest-path": str(manifest)}},
        }
        (build_dir / "pyproject.toml").write_text(tomli_w.dumps(pyproject))

    def _manifest_version(self, src_dir: Path, manifest: Path) -> str:
        result = subprocess.run(
            [
                self.plan.cargo_bin,
                "metadata",
                "--format-version",
                "1",
                "--no-deps",
                "--manifest-path",
                manifest,
            ],
            check=True,
            cwd=src_dir,
            env=os.environ | self.tool.tool.env,
            stdout=subprocess.PIPE,
            text=True,
        )
        metadata = json.loads(result.stdout)
        manifest = manifest.resolve()
        for package in metadata["packages"]:
            if Path(package["manifest_path"]).resolve() == manifest:
                return package["version"]
        raise ValueError(f"no package found for manifest: {manifest}")
