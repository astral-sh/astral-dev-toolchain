from astral_dev_toolchain._constants import RUSTUP_TARGETS
import dataclasses
import importlib.metadata
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

_RUSTUP_TOOLCHAIN = "1.97.1"


_XWIN_ARCHES = ["x86_64", "aarch64"]

_XWIN_SDK_VERSION = "10.0.22621"
_XWIN_CRT_VERSION = "14.44.17.14"


def _find_bin(bin: str) -> Path:
    """
    Locate the `spellcheck` executable installed by this package.
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
    def discover(cls) -> Bootstrapper:
        cargo_xwin_path = _find_bin("cargo-xwin")
        rustup_path = _find_bin("rustup")

        return Bootstrapper(cargo_xwin_path=cargo_xwin_path, rustup_path=rustup_path)

    def rustup(self, args: Sequence[str | os.PathLike[str]]):
        subprocess.run([self.rustup_path, *args], check=True)

    def cargo_xwin(self, args: Sequence[str | os.PathLike[str]]):
        subprocess.run([self.cargo_xwin_path, *args], check=True)

    def bootstrap(self):
        """Bootstrap our Rust toolchain."""

        # Install the toolchain and make it the default.
        self.rustup(
            [
                "toolchain",
                "install",
                "--no-self-update",
                _RUSTUP_TOOLCHAIN,
            ]
        )
        self.rustup(["default", _RUSTUP_TOOLCHAIN])

        # Install our targets.
        self.rustup(["target", "add", *RUSTUP_TARGETS])

        # Download and cache the Windows SDK and CRT.
        self.cargo_xwin(
            [
                "cache",
                "xwin",
                f"--xwin-sdk-version={_XWIN_SDK_VERSION}",
                f"--xwin-crt-version={_XWIN_CRT_VERSION}",
                f"--xwin-arch={','.join(_XWIN_ARCHES)}",
                "--xwin-http-retries=10",
                "--xwin-include-debug-libs",
                "--xwin-include-debug-symbols",
            ],
        )
