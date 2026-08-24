"""Check out tool sources and dispatch to the appropriate build backend."""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from ._build_go import GoBuilder
from ._build_rust import RustBuilder
from ._plan import Plan
from ._tool import GoToolSpec, RustToolSpec, Tool


@dataclass
class Builder:
    plan: Plan
    tool: Tool

    def build(self) -> None:
        with TemporaryDirectory() as tempdir:
            # Build up our directory structure. Roughly:
            #
            # {tmpdir}
            # |
            # |- builder/
            # | |
            # | |- pyproject.toml (generated for Rust builds)
            # | |- README.md (generated for Rust builds)
            # | |- <tool>[.exe] (compiled binary for Go builds)
            # |
            # |- src/ (git clone contents)
            src_dir = Path(tempdir) / "src"
            build_dir = Path(tempdir) / "builder"
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
                self.tool.source.repo,
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
