import argparse
import logging
import sys
from pathlib import Path
from typing import NoReturn

from astral_dev_toolchain._bootstrap import Bootstrapper
from astral_dev_toolchain._build import Builder
from astral_dev_toolchain._plan import Plan
from astral_dev_toolchain._toolchain import Toolchain

_HERE = Path(__file__)
_ROOT = _HERE.parent.parent.parent
_TOOLCHAIN_DIR = _ROOT / "toolchain"
_LOG_FORMAT = "%(levelname)s %(message)s"

logger = logging.getLogger(__name__)


def _configure_logging(*, verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=_LOG_FORMAT,
        force=True,
    )


def run() -> NoReturn:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
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
    build.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be run without building anything",
    )
    build.add_argument(
        "--select",
        type=str,
        default=[],
        nargs="+",
        help="List of toolchain tools to build, by `tool.name`",
    )
    build.add_argument(
        "--output-dir",
        type=Path,
        default="/dist",
        help="The output directory to write wheels into. Note that this is a container-side path; you probably don't need to modify it.",
    )

    args = parser.parse_args()

    _configure_logging(verbose=args.verbose)
    logger.debug("debug logging enabled")

    if not _TOOLCHAIN_DIR.is_dir():
        logger.warning("default toolchain dir %s not present", _TOOLCHAIN_DIR)

    args.func(parser, args)

    sys.exit(0)


def _do_bootstrap(parser: argparse.ArgumentParser, args: argparse.Namespace):
    """
    Runs `astral-dev-toolchain bootstrap`.
    """

    bootstrapper = Bootstrapper.discover()
    bootstrapper.bootstrap()


def _do_build(parser: argparse.ArgumentParser, args: argparse.Namespace):
    """
    Runs `astral-dev-toolchain build`.
    """

    if not args.toolchain_dir.is_dir():
        parser.error(f"toolchain directory does not exist: {args.toolchain_dir}")

    plan = Plan.load(args)
    toolchain = Toolchain.load(plan)

    # TODO: --select a subset of tools here.
    for tool in toolchain.tools:
        builder = Builder(plan=plan, tool=tool)
        builder.build()
