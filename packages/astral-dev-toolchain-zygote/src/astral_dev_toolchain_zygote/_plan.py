"""Operation planning."""

import argparse
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from ._config import ImageConfig


class PlanError(ValueError): ...


def _which(bin: str) -> Path:
    path = which(bin)

    if not path:
        raise ValueError(f"missing {bin}")

    return Path(path)


@dataclass(kw_only=True)
class Plan:
    git_bin: Path
    cargo_bin: Path
    maturin_bin: Path
    go_bin: Path
    toolchain_dir: Path
    output_dir: Path
    dry_run: bool
    selected_tools: set[str]
    targets: tuple[str, ...]

    @classmethod
    def load(cls, args: argparse.Namespace, config: ImageConfig) -> Plan:
        if not args.output_dir.is_dir():
            raise PlanError(f"not a directory: {args.output_dir}")

        selected_targets = set(args.target)
        unknown_targets = selected_targets - set(config.targets)
        if unknown_targets:
            raise PlanError(
                f"unknown build targets: {', '.join(sorted(unknown_targets))}; "
                f"supported targets: {', '.join(config.targets)}"
            )
        targets = tuple(
            target
            for target in config.targets
            if not selected_targets or target in selected_targets
        )

        return cls(
            git_bin=_which("git"),
            cargo_bin=_which("cargo"),
            maturin_bin=_which("maturin"),
            go_bin=_which("go"),
            toolchain_dir=args.toolchain_dir,
            output_dir=args.output_dir,
            selected_tools=set(args.select),
            targets=targets,
            dry_run=args.dry_run,
        )
