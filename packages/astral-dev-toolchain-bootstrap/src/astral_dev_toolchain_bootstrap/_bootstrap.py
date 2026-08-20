import dataclasses
import importlib.metadata
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from ._config import ImageConfig


def _find_bin(bin: str) -> Path:
    """
    Locate a binary in package metadata.
    """
    files = importlib.metadata.files(bin)

    if not files:
        raise ValueError(f"Couldn't locate {bin}: no files found in package metadata")

    path = next((Path(file.locate()) for file in files if file.name == bin), None)

    if not path:
        raise ValueError(f"Missing {bin}")

    return path


@dataclasses.dataclass
class Bootstrapper:
    cargo_xwin_path: Path
    rustup_path: Path

    @classmethod
    def discover(cls) -> "Bootstrapper":
        return cls(
            cargo_xwin_path=_find_bin("cargo-xwin"),
            rustup_path=_find_bin("rustup"),
        )

    def rustup(self, args: Sequence[str | os.PathLike[str]]) -> None:
        subprocess.run([self.rustup_path, *args], check=True)

    def cargo_xwin(self, args: Sequence[str | os.PathLike[str]]) -> None:
        subprocess.run([self.cargo_xwin_path, *args], check=True)

    def install_rustup_proxies(self) -> None:
        """Materialize rustup and its tool proxies in CARGO_HOME."""
        cargo_bin = Path(os.environ["CARGO_HOME"]) / "bin"
        cargo_bin.mkdir(parents=True, exist_ok=True)

        rustup = cargo_bin / "rustup"
        shutil.copy2(self.rustup_path, rustup)

        for proxy in (
            "cargo",
            "cargo-clippy",
            "cargo-fmt",
            "clippy-driver",
            "rustc",
            "rustdoc",
            "rustfmt",
        ):
            path = cargo_bin / proxy
            path.unlink(missing_ok=True)
            path.symlink_to("rustup")

    def bootstrap(self, config: ImageConfig) -> None:
        """Bootstrap the Rust toolchain and Windows SDK cache."""
        self.rustup(
            [
                "toolchain",
                "install",
                "--no-self-update",
                config.rust.toolchain,
            ]
        )
        self.rustup(["default", config.rust.toolchain])
        self.rustup(["target", "add", *config.rust.targets])
        self.install_rustup_proxies()

        xwin_args = [
            "cache",
            "xwin",
            f"--xwin-sdk-version={config.xwin.sdk_version}",
            f"--xwin-crt-version={config.xwin.crt_version}",
            f"--xwin-arch={','.join(config.xwin.architectures)}",
            f"--xwin-http-retries={config.xwin.http_retries}",
        ]
        if config.xwin.include_debug_libs:
            xwin_args.append("--xwin-include-debug-libs")
        if config.xwin.include_debug_symbols:
            xwin_args.append("--xwin-include-debug-symbols")

        self.cargo_xwin(xwin_args)
