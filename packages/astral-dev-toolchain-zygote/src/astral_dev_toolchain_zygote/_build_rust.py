"""Build Rust tools with maturin."""

import json
import logging
import os
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from astral_dev_toolchain_core import RustToolSpec, Tool
from packaging.version import Version

from ._metadata import prepare_readme
from ._plan import Plan

logger = logging.getLogger(__name__)


def _resolve_checkout_file(src_dir: Path, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(src_dir) or not resolved.is_file():
        raise ValueError(f"expected a regular {path.name} inside {src_dir}")
    return resolved


def _remove_cargo_config(src_dir: Path, directory: Path) -> None:
    # Cargo has no option to disable configuration discovered in a source
    # tree, and higher-priority configuration cannot clear array values.
    # The checkout is temporary, so remove both supported filenames.
    cargo_dir = (directory / ".cargo").resolve()
    if not cargo_dir.is_relative_to(src_dir):
        raise ValueError(f"Cargo config directory is outside {src_dir}: {cargo_dir}")
    for name in ("config.toml", "config"):
        config = cargo_dir / name
        if config.is_symlink() or config.is_file():
            logger.info("ignoring upstream Cargo configuration: %s", config)
            config.unlink()


def _upstream_maturin_manifest(src_dir: Path) -> Path | None:
    try:
        pyproject = _resolve_checkout_file(src_dir, src_dir / "pyproject.toml")
    except FileNotFoundError:
        return None
    with pyproject.open("rb") as stream:
        upstream = tomllib.load(stream)

    tool = upstream.get("tool")
    if isinstance(tool, dict):
        maturin = tool.get("maturin")
    else:
        maturin = None

    if isinstance(maturin, dict):
        manifest = maturin.get("manifest-path")
    else:
        manifest = None

    if manifest is None:
        return None
    if not isinstance(manifest, str) or not manifest:
        raise ValueError(
            f"{pyproject}: tool.maturin.manifest-path must be a non-empty string"
        )
    return (src_dir / manifest).resolve()


@dataclass(frozen=True)
class _CargoPackage:
    manifest: Path
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
        src_dir = src_dir.resolve()
        if not self.tool.tool.cargo_config:
            _remove_cargo_config(src_dir, src_dir)

        package = self._manifest_metadata(src_dir)
        if self.tool.tool.manifest is None and not self.tool.tool.cargo_config:
            # cargo-xwin looks up Rust flags starting at the member's directory.
            for directory in package.manifest.parents:
                if directory == src_dir:
                    break
                _remove_cargo_config(src_dir, directory)

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
            "tool": {
                "maturin": {"bindings": "bin", "manifest-path": str(package.manifest)}
            },
        }
        (build_dir / "pyproject.toml").write_text(
            tomlkit.dumps(pyproject), encoding="utf-8"
        )

    def _cargo_metadata(self, src_dir: Path, manifest: Path) -> dict:
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
        return json.loads(result.stdout)

    def _manifest_metadata(self, src_dir: Path) -> _CargoPackage:
        override = self.tool.tool.manifest
        if override is None:
            manifest = src_dir / "Cargo.toml"
            # Cargo resolves members relative to the original manifest path.
            _resolve_checkout_file(src_dir, manifest)
        else:
            manifest = src_dir / override

        metadata = self._cargo_metadata(src_dir, manifest)
        packages = metadata["packages"]
        if override is None:
            default_members = set(metadata["workspace_default_members"])
            packages = [pkg for pkg in packages if pkg["id"] in default_members]

        if override is None and len(packages) == 1:
            package = packages[0]
        else:
            if override is None:
                selected_manifest = _upstream_maturin_manifest(src_dir)
            else:
                selected_manifest = manifest.resolve()

            for package in packages:
                if Path(package["manifest_path"]).resolve() == selected_manifest:
                    break
            else:
                package = None
        if package is None:
            raise ValueError(
                f"could not select a Cargo package for {manifest}. "
                "Set tool.manifest to the package's Cargo.toml."
            )

        member_manifest = Path(package["manifest_path"])
        if override is None:
            manifest = _resolve_checkout_file(src_dir, member_manifest)
            if manifest.parent != member_manifest.parent.resolve():
                raise ValueError(
                    f"inferred manifest changes package directory: {member_manifest}"
                )
            # Maturin invokes Cargo on the member, which can inherit dependencies
            # from a nested workspace instead of the one that selected it.
            if manifest != src_dir / "Cargo.toml":
                member_metadata = self._cargo_metadata(src_dir, manifest)
                if member_metadata["workspace_root"] != metadata["workspace_root"]:
                    raise ValueError(
                        "inferred manifest selects a different Cargo workspace: "
                        f"{manifest}"
                    )
        else:
            manifest = member_manifest.resolve()

        readme = package.get("readme")
        if readme is None:
            raise ValueError(f"package does not declare a README: {manifest}")
        readme_path = (manifest.parent / readme).resolve()
        if not readme_path.is_relative_to(src_dir):
            raise ValueError(
                f"package README is outside the source checkout: {readme_path}"
            )
        if not readme_path.is_file():
            raise ValueError(f"package README does not exist: {readme_path}")
        return _CargoPackage(
            manifest=manifest, version=package["version"], readme=readme_path
        )
