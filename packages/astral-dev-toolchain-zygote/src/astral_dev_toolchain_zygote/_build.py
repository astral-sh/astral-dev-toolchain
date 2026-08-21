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
                case GoToolSpec() as spec:
                    GoBuilder(self.plan, spec, package_name).build(src_dir, build_dir)
                case RustToolSpec() as spec:
                    RustBuilder(self.plan, spec, package_name).build(src_dir, build_dir)

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
        # TODO: Confirm the checkout matches `self.tool.source.commit`.
