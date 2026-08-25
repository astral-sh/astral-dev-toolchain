import argparse
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from astral_dev_toolchain_core import Toolchain

from ._test import Tester

_LOCAL_IMAGE = "astral-dev-toolchain-zygote:local"
_LOG_FORMAT = "%(levelname)s %(message)s"
_TEST_SELECT_ENV = "ASTRAL_DEV_TOOLCHAIN_TEST_SELECT"

logger = logging.getLogger(__name__)


def _build_zygote(args: argparse.Namespace, docker: str) -> str:
    if args.image:
        return args.image

    command = [docker, "build", "--tag", _LOCAL_IMAGE]
    if args.bootstrap_image:
        command.extend(["--build-arg", f"BOOTSTRAP_IMAGE={args.bootstrap_image}"])
        logger.info("building local zygote image from %s", args.bootstrap_image)
    else:
        logger.info("building local zygote image from the latest GHCR bootstrap")
    command.append(".")

    logger.debug("running: %s", shlex.join(command))
    try:
        result = subprocess.run(command, check=False)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    if result.returncode:
        raise SystemExit(result.returncode)

    return _LOCAL_IMAGE


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    images = parser.add_mutually_exclusive_group()
    images.add_argument(
        "--bootstrap-image",
        help="Bootstrap image for the local zygote build (defaults to GHCR latest)",
    )
    images.add_argument(
        "--image",
        help="Prebuilt zygote image to execute instead of building locally",
    )

    parser.add_argument(
        "--toolchain-dir",
        type=Path,
        default=Path("toolchain"),
        help="Load tool definitions from this directory",
    )

    subcommands = parser.add_subparsers(required=True)

    build = subcommands.add_parser("build")
    build.set_defaults(func=_do_build)
    build.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be run without building anything",
    )
    build.add_argument(
        "--select",
        default=[],
        nargs="+",
        metavar="NAME",
        help="List of toolchain tools to build, by tool.name",
    )
    build.add_argument(
        "--output-dir",
        type=Path,
        default=Path("toolchain-dist"),
        help="Host directory to write wheels into (default: ./toolchain-dist)",
    )

    test = subcommands.add_parser("test")
    test.set_defaults(func=_do_test)
    test.add_argument(
        "--select",
        default=[],
        nargs="+",
        metavar="NAME",
        help="List of toolchain tools to test, by tool.name",
    )
    test.add_argument(
        "--dist-dir",
        type=Path,
        default=Path("toolchain-dist"),
        help="Host directory containing wheels to test (default: ./toolchain-dist)",
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=_LOG_FORMAT,
        force=True,
    )

    toolchain_dir = args.toolchain_dir.resolve()
    if not toolchain_dir.is_dir():
        parser.error(f"toolchain directory does not exist: {toolchain_dir}")

    args.toolchain_dir = toolchain_dir
    args.func(parser, args)


def _do_build(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    docker = shutil.which("docker")
    if docker is None:
        parser.error("docker is not installed or not present on PATH")

    image = _build_zygote(args, docker)
    command = [
        docker,
        "run",
        "--rm",
        "--init",
        "--label",
        f"org.astral-sh.astral-dev-toolchain.invocation={uuid4().hex}",
    ]
    if sys.stdin.isatty():
        command.append("--interactive")
    if sys.stdout.isatty():
        command.append("--tty")

    command.extend(
        [
            "--mount",
            f"type=bind,source={args.toolchain_dir},target=/toolchain,readonly",
            "--mount",
            f"type=bind,source={output_dir},target=/dist",
            image,
        ]
    )
    if args.verbose:
        command.append("--verbose")
    command.append("build")
    if args.dry_run:
        command.append("--dry-run")
    if args.select:
        command.extend(["--select", *args.select])

    logger.info("running toolchain build in %s", image)
    logger.debug("running: %s", shlex.join(command))

    # Let Docker own signal forwarding and exit-status propagation directly.
    os.execv(docker, command)


def _do_test(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.bootstrap_image or args.image:
        parser.error("--bootstrap-image and --image can only be used with build")

    dist_dir = args.dist_dir.resolve()
    if not dist_dir.is_dir():
        parser.error(f"distribution directory does not exist: {dist_dir}")

    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv is not installed or not present on PATH")

    selected = args.select
    if not selected and (raw_selection := os.environ.get(_TEST_SELECT_ENV)):
        try:
            selected = json.loads(raw_selection)
        except json.JSONDecodeError as error:
            parser.error(f"invalid {_TEST_SELECT_ENV}: {error}")
        if not isinstance(selected, list) or not all(
            isinstance(tool, str) for tool in selected
        ):
            parser.error(f"{_TEST_SELECT_ENV} must be a JSON list of strings")

    toolchain = Toolchain.load(args.toolchain_dir, selected)
    logger.info("testing %d tool(s)", len(toolchain.tools))
    if not Tester(toolchain=toolchain, dist_dir=dist_dir, uv_bin=uv).run():
        raise SystemExit(1)
