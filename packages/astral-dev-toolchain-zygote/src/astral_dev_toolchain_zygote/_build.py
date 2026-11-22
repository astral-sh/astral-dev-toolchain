"""Check out tool sources and dispatch to the appropriate build backend."""

import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import gettempdir

from astral_dev_toolchain_core import GoToolSpec, RustToolSpec, Tool

from ._build_go import GoBuilder
from ._build_rust import RustBuilder
from ._plan import Plan


@contextmanager
def _build_root() -> Iterator[Path]:
    # Each container builds one tool at a time. A fixed checkout path keeps
    # Rust symbols, Zig runtime debug info and CycloneDX references reproducible.
    # TODO(https://github.com/astral-sh/astral-dev-toolchain/issues/142):
    # Once we upgrade to Rust 1.101, try Cargo's trim-paths and remove the fixed
    # path if Zig and CycloneDX also produce path-independent output.
    root = Path(gettempdir()) / "astral-dev-toolchain-build"
    root.mkdir(mode=0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root)


@dataclass
class Builder:
    plan: Plan
    tool: Tool

    def build(self) -> None:
        with _build_root() as root:
            # Build up our directory structure. Roughly:
            #
            # {root}
            # |
            # |- builder/
            # | |
            # | |- pyproject.toml (generated for Rust builds)
            # | |- README.md (generated for Rust and Go builds)
            # |
            # |- src/ (git clone contents)
            src_dir = root / "src"
            build_dir = root / "builder"
            src_dir.mkdir()
            build_dir.mkdir()
            self._checkout(src_dir)

            package_name = self.tool.python_package_name
            match self.tool.tool:
                case GoToolSpec():
                    go_builder = GoBuilder.prepare(self.plan, self.tool, package_name)
                    go_builder.build(src_dir, build_dir)
                case RustToolSpec():
                    rust_builder = RustBuilder.prepare(
                        self.plan, self.tool, package_name
                    )
                    rust_builder.build(src_dir, build_dir)

    def _checkout(self, src_dir: Path) -> None:
        subprocess.run(
            [
                self.plan.git_bin,
                "clone",
                "--branch",
                self.tool.source.tag,
                "--depth",
                "1",
                self.tool.tool.repo,
                ".",
            ],
            check=True,
            cwd=src_dir,
        )
        result = subprocess.run(
            [self.plan.git_bin, "rev-parse", "HEAD"],
            check=True,
            cwd=src_dir,
            stdout=subprocess.PIPE,
            text=True,
        )
        actual_commit = result.stdout.strip()
        expected_commit = self.tool.source.commit
        if actual_commit != expected_commit:
            raise ValueError(
                f"source commit mismatch for {self.tool.tool.name}: "
                f"expected {expected_commit}, got {actual_commit}"
            )
