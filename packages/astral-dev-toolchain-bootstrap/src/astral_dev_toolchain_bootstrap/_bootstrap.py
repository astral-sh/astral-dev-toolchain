import dataclasses
import importlib.metadata
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from ._config import ImageConfig


def _find_bin(package: str, bin: str) -> Path:
    """
    Locate a binary in package metadata.
    """
    files = importlib.metadata.files(package)

    if not files:
        raise ValueError(f"Couldn't locate {bin}: no files found for {package}")

    path = next((Path(file.locate()) for file in files if file.name == bin), None)

    if not path:
        raise ValueError(f"Missing {bin} in {package}")

    return path


@dataclasses.dataclass
class Bootstrapper:
    cargo_xwin_path: Path
    ninja_path: Path
    rustup_path: Path

    @classmethod
    def discover(cls) -> Bootstrapper:
        return cls(
            cargo_xwin_path=_find_bin("astral-dev-toolchain-cargo-xwin", "cargo-xwin"),
            ninja_path=_find_bin("ninja", "ninja"),
            rustup_path=_find_bin("rustup", "rustup"),
        )

    def rustup(self, args: Sequence[str | os.PathLike[str]]) -> None:
        subprocess.run([self.rustup_path, *args], check=True)

    def cargo_xwin(self, args: Sequence[str | os.PathLike[str]]) -> None:
        subprocess.run([self.cargo_xwin_path, *args], check=True)

    def bootstrap(self, config: ImageConfig) -> None:
        """Bootstrap the Rust toolchain and Windows SDK cache."""
        self.rustup(
            [
                "toolchain",
                "install",
                "--no-self-update",
                "--profile",
                "minimal",
                config.rust.toolchain,
            ]
        )
        self.rustup(["default", config.rust.toolchain])
        self.rustup(["target", "add", *config.rust.targets])

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
