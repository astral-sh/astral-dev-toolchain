import argparse
import sys
from pathlib import Path
from typing import NoReturn
from warnings import warn

from astral_dev_toolchain._bootstrap import Bootstrapper
from astral_dev_toolchain._toolchain import Toolchain

_HERE = Path(__file__)
_ROOT = _HERE.parent.parent.parent
_TOOLCHAIN_DIR = _ROOT / "toolchain"

if not _TOOLCHAIN_DIR.is_dir():
    warn(f"default toolchain dir {_TOOLCHAIN_DIR} not present")


def run() -> NoReturn:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--toolchain-dir",
        type=Path,
        default=_TOOLCHAIN_DIR,
        help="Load tool definitions from this directory",
    )

    subcmds = parser.add_subparsers(dest="command", required=True)

    # `astral-dev-toolchain bootstrap`
    bootstrap = subcmds.add_parser("bootstrap")
    bootstrap.set_defaults(func=_do_bootstrap)

    # `astral-dev-toolchain build`
    build = subcmds.add_parser("build")
    build.set_defaults(func=_do_build)

    args = parser.parse_args()

    args.func(parser, args)

    sys.exit(0)


def _do_bootstrap(parser: argparse.ArgumentParser, args: argparse.Namespace):
    """
    Runs `astral-dev-toolchain bootstrap`.
    """

    bootstrapper = Bootstrapper.discover()
    bootstrapper.bootstrap()
    print(bootstrapper)


def _do_build(parser: argparse.ArgumentParser, args: argparse.Namespace):
    """
    Runs `astral-dev-toolchain build`.
    """

    if not args.toolchain_dir.is_dir():
        parser.error(f"toolchain directory does not exist: {args.toolchain_dir}")

    toolchain = Toolchain.load(args.toolchain_dir)
    print(toolchain)
