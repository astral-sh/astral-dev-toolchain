"""Cross-compile CGO-disabled Go tools and package them as wheels."""

import logging
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from go_to_wheel import build_wheel, compile_go_binary
from packaging.version import Version

from ._metadata import readme_notice
from ._plan import Plan
from ._tool import GoToolSpec, Tool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Target:
    goos: str
    goarch: str
    wheel_tag: str


# The shared configuration uses Rust triples. Go 1.26 requires macOS 12.
_TARGETS = {
    "aarch64-apple-darwin": _Target("darwin", "arm64", "macosx_12_0_arm64"),
    "x86_64-apple-darwin": _Target("darwin", "amd64", "macosx_12_0_x86_64"),
    "aarch64-unknown-linux-gnu": _Target("linux", "arm64", "manylinux_2_17_aarch64"),
    "x86_64-unknown-linux-gnu": _Target("linux", "amd64", "manylinux_2_17_x86_64"),
    "aarch64-pc-windows-msvc": _Target("windows", "arm64", "win_arm64"),
    "x86_64-pc-windows-msvc": _Target("windows", "amd64", "win_amd64"),
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

        go_dir = self._package_directory(src_dir)
        for target in self.plan.targets:
            platform = _TARGETS[target]
            suffix = ".exe" if platform.goos == "windows" else ""
            binary = build_dir / f"{self.tool.tool.name}{suffix}"
            if self.plan.dry_run:
                self._log_dry_run(go_dir, binary, platform)
            else:
                logger.info("building %s for %s", self.tool.tool.name, target)
                self._build_target(go_dir, binary, platform)

    def _package_directory(self, src_dir: Path) -> Path:
        src_dir = src_dir.resolve()
        go_dir = (src_dir / self.tool.tool.directory).resolve()
        if Path(self.tool.tool.directory).is_absolute() or not go_dir.is_relative_to(
            src_dir
        ):
            raise ValueError(
                "Go tool.directory must be relative to the source checkout"
            )
        if not go_dir.is_dir():
            raise ValueError(f"Go package directory does not exist: {go_dir}")
        return go_dir

    @property
    def _ldflags(self) -> str | None:
        if self.tool.tool.version_var:
            # NOTE: Intentionally uses the verbatim configured version,
            # since this appears in the Go binary and not the Python metadata.
            return f"-X {self.tool.tool.version_var}={self.tool.tool.version}"
        return None

    def _build_target(self, go_dir: Path, binary: Path, platform: _Target) -> None:
        compile_go_binary(
            go_dir=str(go_dir),
            output_path=str(binary),
            goos=platform.goos,
            goarch=platform.goarch,
            go_binary=str(self.plan.go_bin),
            ldflags=self._ldflags,
        )
        wheel = build_wheel(
            binary_path=str(binary),
            output_dir=str(self.plan.output_dir),
            name=self.package_name,
            # NOTE: Uses the PEP 440 version, including any postrelease modifier.
            version=str(self.version),
            platform_tag=platform.wheel_tag,
            entry_point=self.tool.tool.name,
            is_windows=platform.goos == "windows",
            readme_content=readme_notice(self.tool),
        )
        logger.info("built %s", wheel)

    def _log_dry_run(self, go_dir: Path, binary: Path, platform: _Target) -> None:
        ldflags = "-s -w"
        if extra_flags := self._ldflags:
            ldflags += f" {extra_flags}"
        logger.info(
            "[dry-run] would compile '%s' with '%s' for %s/%s "
            "(CGO_ENABLED=0, ldflags=%r) to '%s'",
            go_dir,
            self.plan.go_bin,
            platform.goos,
            platform.goarch,
            ldflags,
            binary,
        )
        logger.info(
            "[dry-run] would package '%s' as %s %s for %s in '%s'",
            binary,
            self.package_name,
            self.tool.tool.version,
            platform.wheel_tag,
            self.plan.output_dir,
        )
