"""
Operation planning.
"""

from dataclasses import dataclass
from pathlib import Path

from astral_dev_toolchain._toolchain import Toolchain


@dataclass(kw_only=True)
class Plan:
    maturin_bin: Path
    rustup_bin: Path
    toolchain: Toolchain
    dry_run: bool
