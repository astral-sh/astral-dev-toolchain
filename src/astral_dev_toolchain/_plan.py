"""
Operation planning.
"""

import argparse

from dataclasses import dataclass
from pathlib import Path
from shutil import which


def _which(bin: str) -> Path:
    path = which(bin)

    if not path:
        raise ValueError(f"missing {bin}")

    return Path(path)


@dataclass(kw_only=True)
class Plan:
    maturin_bin: Path
    output_dir: Path
    dry_run: bool

    @classmethod
    def load(cls, args: argparse.Namespace) -> Plan:
        if not args.output_dir.is_dir():
            raise ValueError(f"Not a directory: {args.output_dir}")

        return Plan(
            maturin_bin=_which("maturin"), output_dir=args.output_dir, dry_run=False
        )
