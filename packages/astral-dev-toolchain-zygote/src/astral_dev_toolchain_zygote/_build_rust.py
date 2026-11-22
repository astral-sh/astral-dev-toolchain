"""Build Rust tools with maturin."""

import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from astral_dev_toolchain_core import RustToolSpec, Tool
from packaging.version import Version

from ._metadata import prepare_readme
from ._plan import Plan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CargoPackage:
    version: str
    readme: Path


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
        if self.tool.tool.features:
            command.extend(["--features", ",".join(self.tool.tool.features)])
        command.extend(["--out", self.plan.output_dir])

        # RUSTFLAGS would override the tool's and the image's target-specific flags.
        # Use Cargo's trim-paths instead of a wrapper once we upgrade to Rust 1.101
        # (https://github.com/astral-sh/astral-dev-toolchain/issues/142).
        env = os.environ | self.tool.tool.env
        wrapper = build_dir / "rustc-wrapper"
        remap = shlex.quote(f"--remap-path-prefix={src_dir.resolve()}=/src")
        inner = env.get("RUSTC_WRAPPER")
        compiler = f'{shlex.quote(inner)} "$@"' if inner else '"$@"'
        wrapper.write_text(f"#!/bin/sh\nexec {compiler} {remap}\n", encoding="utf-8")
        wrapper.chmod(0o700)
        env["RUSTC_WRAPPER"] = str(wrapper)

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
                target_env = env.copy()
                if target.endswith("-linux-gnu"):
                    # Zig builds glibc and libunwind with its own Clang invocations.
                    # Linker flags don't reach them. '+' appends a flag, '#' silences
                    # Clang's override log.
                    overrides = env.get("CCC_OVERRIDE_OPTIONS", "#")
                    target_env["CCC_OVERRIDE_OPTIONS"] = (
                        f"{overrides} +-fdebug-compilation-dir=."
                    )
                subprocess.run(
                    target_command,
                    check=True,
                    cwd=build_dir,
                    env=target_env,
                )

    def _prepare(self, src_dir: Path, build_dir: Path) -> None:
        if not self.tool.tool.cargo_config:
            # Cargo has no option to disable configuration discovered in a source
            # tree, and higher-priority configuration cannot clear array values.
            # The checkout is temporary, so remove both supported filenames.
            cargo_dir = src_dir / ".cargo"
            for name in ("config.toml", "config"):
                config = cargo_dir / name
                if config.is_file() or config.is_symlink():
                    logger.info("ignoring upstream Cargo configuration: %s", config)
                    config.unlink()

        manifest = src_dir / self.tool.tool.manifest
        package = self._manifest_metadata(src_dir, manifest)

        raw_version = self.tool.tool.version or package.version
        version = Version(raw_version)
        if self.tool.tool.rebuild:
            version = version.__replace__(post=self.tool.tool.rebuild)

        readme = prepare_readme(self.tool, package.readme, build_dir)
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
                "readme": readme.name,
                "dynamic": [
                    "description",
                    "keywords",
                    "license",
                    "urls",
                ],
            },
            "tool": {"maturin": {"bindings": "bin", "manifest-path": str(manifest)}},
        }
        (build_dir / "pyproject.toml").write_text(
            tomlkit.dumps(pyproject), encoding="utf-8"
        )

    def _manifest_metadata(self, src_dir: Path, manifest: Path) -> _CargoPackage:
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
                readme = package.get("readme")
                if not isinstance(readme, str):
                    raise ValueError(f"package does not declare a README: {manifest}")
                readme_path = Path(readme)
                if not readme_path.is_absolute():
                    readme_path = manifest.parent / readme_path
                readme_path = readme_path.resolve()
                if not readme_path.is_relative_to(src_dir.resolve()):
                    raise ValueError(
                        f"package README is outside the source checkout: {readme_path}"
                    )
                if not readme_path.is_file():
                    raise ValueError(f"package README does not exist: {readme_path}")
                return _CargoPackage(version=package["version"], readme=readme_path)
        raise ValueError(f"no package found for manifest: {manifest}")
