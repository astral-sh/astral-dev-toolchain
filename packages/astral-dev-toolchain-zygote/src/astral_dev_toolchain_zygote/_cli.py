import argparse
import logging
from pathlib import Path

from astral_dev_toolchain_core import Toolchain

from ._build import Builder
from ._config import ImageConfig
from ._plan import Plan

_LOG_FORMAT = "%(levelname)s %(message)s"

logger = logging.getLogger(__name__)


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/app/toolchain-image.toml"),
        help="Image toolchain configuration",
    )
    parser.add_argument(
        "--toolchain-dir",
        type=Path,
        default=Path("/toolchain"),
        help="Load tool definitions from this directory",
    )

    subcommands = parser.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser("build")
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
        help="List of toolchain tools to build, by tool.name",
    )
    build.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/dist"),
        help="Container directory to write wheels into",
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=_LOG_FORMAT,
        force=True,
    )
    logger.debug("debug logging enabled")
    args.func(parser, args)


def _do_build(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.toolchain_dir.is_dir():
        parser.error(f"toolchain directory does not exist: {args.toolchain_dir}")

    config = ImageConfig.load(args.config)
    plan = Plan.load(args, config)
    toolchain = Toolchain.load(plan.toolchain_dir, plan.selected_tools)
    logger.info("building %d tool(s)", len(toolchain.tools))

    for tool in toolchain.tools:
        Builder(plan=plan, tool=tool).build()
