"""Cross-compile CGO-disabled Go tools and package them with radlermass."""

import logging
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from astral_dev_toolchain_core import GoToolSpec, Tool
from packaging.version import Version
from radlermass import build_wheels

from ._metadata import prepare_readme
from ._plan import Plan

logger = logging.getLogger(__name__)

# The shared configuration uses Rust triples; radlermass selects the wheel tags.
_TARGETS = {
    "aarch64-apple-darwin": "darwin-arm64",
    "x86_64-apple-darwin": "darwin-amd64",
    "aarch64-unknown-linux-gnu": "linux-arm64",
    "x86_64-unknown-linux-gnu": "linux-amd64",
    "aarch64-pc-windows-msvc": "windows-arm64",
    "x86_64-pc-windows-msvc": "windows-amd64",
}


@dataclass
class GoBuilder:
    plan: Plan
    tool: Tool[GoToolSpec]
    package_name: str

    @classmethod
    def prepare(
        cls, plan: Plan, tool: Tool[GoToolSpec], package_name: str
    ) -> GoBuilder:
        return cls(plan=plan, tool=tool, package_name=package_name)

    @cached_property
    def version(self) -> Version:
        """
        Creates a proper PEP 440 version, including any postrelease number.
        """
        base = Version(self.tool.tool.version)
        if self.tool.tool.rebuild:
            base = base.__replace__(post=self.tool.tool.rebuild)
        return base

    def build(self, src_dir: Path, build_dir: Path) -> None:
        for target in self.plan.targets:
            if target not in _TARGETS:
                raise ValueError(f"unsupported Go build target: {target}")
        platforms = [_TARGETS[target] for target in self.plan.targets]

        src_dir = src_dir.resolve()
        go_dir = self._source_path(src_dir, self.tool.tool.directory, "directory")
        if not go_dir.is_dir():
            raise ValueError(f"Go package directory does not exist: {go_dir}")
        module = self._module_directory(src_dir, go_dir)
        package_path = go_dir.relative_to(module).as_posix()

        readme = self._source_path(src_dir, self.tool.tool.readme, "readme")
        if not readme.is_file():
            raise ValueError(f"package README does not exist: {readme}")
        readme = prepare_readme(self.tool, readme, build_dir).resolve()

        if self.plan.dry_run:
            logger.info(
                "[dry-run] would build %s %s with radlermass using '%s' from '%s' "
                "(package=%s, platforms=%s, ldflags=%r, readme=%s) in '%s'",
                self.package_name,
                self.version,
                self.plan.go_bin,
                module,
                package_path,
                platforms,
                self._ldflags,
                readme,
                self.plan.output_dir,
            )
            return

        logger.info("building %s for %s", self.tool.tool.name, platforms)
        wheels = build_wheels(
            module,
            package_path=package_path,
            name=self.package_name,
            version=str(self.version),
            output_dir=self.plan.output_dir,
            entry_point=self.tool.tool.name,
            platforms=platforms,
            go_binary=str(self.plan.go_bin),
            ldflags=self._ldflags,
            readme=readme,
        )
        for wheel in wheels:
            logger.info("built %s", wheel)

    @staticmethod
    def _source_path(src_dir: Path, value: str, field: str) -> Path:
        path = (src_dir / value).resolve()
        if Path(value).is_absolute() or not path.is_relative_to(src_dir):
            raise ValueError(f"Go tool.{field} must be relative to the source checkout")
        return path

    @staticmethod
    def _module_directory(src_dir: Path, go_dir: Path) -> Path:
        # Preserve support for commands in nested modules without searching
        # outside the pinned checkout.
        for directory in (go_dir, *go_dir.parents):
            if (directory / "go.mod").is_file():
                return directory
            if directory == src_dir:
                break
        raise ValueError(f"no go.mod found for Go package: {go_dir}")

    @property
    def _ldflags(self) -> str | None:
        if self.tool.tool.version_var:
            # Pass the upstream version verbatim; set_version_var would use the
            # normalized Python version, including any postrelease modifier.
            return f"-X {self.tool.tool.version_var}={self.tool.tool.version}"
        return None
