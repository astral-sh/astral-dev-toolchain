"""Operation planning."""

import argparse
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from ._config import ImageConfig


def _which(bin: str) -> Path:
    path = which(bin)

    if not path:
        raise ValueError(f"missing {bin}")

    return Path(path)


@dataclass(kw_only=True)
class Plan:
    toolchain_dir: Path
    maturin_bin: Path
    output_dir: Path
    dry_run: bool
    selected_tools: set[str]
    targets: tuple[str, ...]

    @classmethod
    def load(cls, args: argparse.Namespace, config: ImageConfig) -> Plan:
        if not args.output_dir.is_dir():
            raise ValueError(f"not a directory: {args.output_dir}")

        return cls(
            toolchain_dir=args.toolchain_dir,
            maturin_bin=_which("maturin"),
            output_dir=args.output_dir,
            selected_tools=set(args.select),
            targets=config.targets,
            dry_run=args.dry_run,
        )
