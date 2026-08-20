import argparse
import logging
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

_LOCAL_IMAGE = "astral-dev-toolchain-zygote:local"
_ZYGOTE_DOCKERFILE = Path("Dockerfile")
_ZYGOTE_PACKAGE = Path("packages/astral-dev-toolchain-zygote")
_CONTAINER_CONFIG = "/app/toolchain-image.toml"
_CONTAINER_TOOLCHAIN_DIR = "/toolchain"
_CONTAINER_OUTPUT_DIR = "/dist"
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
        "--bootstrap-image",
        help="Bootstrap image for the local zygote build (defaults to GHCR latest)",
    )
    parser.add_argument(
        "--image",
        help="Prebuilt zygote image to execute instead of building locally",
    )
    parser.add_argument(
        "--toolchain-dir",
        type=Path,
        default=Path("toolchain"),
        help="Load tool definitions from this directory",
    )

    subcmds = parser.add_subparsers(dest="command", required=True)

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
        default=Path("dist"),
        help="Host directory to write wheels into",
    )

    args = parser.parse_args()

    _configure_logging(verbose=args.verbose)
    logger.debug("debug logging enabled")

    args.func(parser, args)
    sys.exit(0)


def _build_zygote(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    docker: str,
    project_dir: Path,
) -> str:
    if args.image:
        if args.bootstrap_image:
            parser.error("--bootstrap-image cannot be combined with --image")
        return args.image

    for required in (_ZYGOTE_DOCKERFILE, _ZYGOTE_PACKAGE):
        if not (project_dir / required).exists():
            parser.error(f"repository input does not exist: {project_dir / required}")

    command = [
        docker,
        "build",
        "--file",
        str(project_dir / _ZYGOTE_DOCKERFILE),
        "--tag",
        _LOCAL_IMAGE,
    ]
    if args.bootstrap_image:
        command.extend(["--build-arg", f"BOOTSTRAP_IMAGE={args.bootstrap_image}"])
        logger.info("building local zygote image from %s", args.bootstrap_image)
    else:
        logger.info("building local zygote image from the latest GHCR bootstrap")
    command.append(str(project_dir))
    logger.debug("running: %s", shlex.join(command))

    try:
        completed = subprocess.run(command, check=False)
    except KeyboardInterrupt:
        sys.exit(130)
    if completed.returncode != 0:
        sys.exit(completed.returncode)

    return _LOCAL_IMAGE


def _do_build(parser: argparse.ArgumentParser, args: argparse.Namespace) -> NoReturn:
    project_dir = Path.cwd()
    toolchain_dir = args.toolchain_dir.resolve()
    if not toolchain_dir.is_dir():
        parser.error(f"toolchain directory does not exist: {toolchain_dir}")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    docker = shutil.which("docker")
    if docker is None:
        parser.error("docker is not installed or not present on PATH")

    image = _build_zygote(parser, args, docker, project_dir)
    invocation = uuid4().hex
    container_name = f"astral-dev-toolchain-{invocation}"
    command = [
        docker,
        "run",
        "--rm",
        "--pull=missing",
        "--name",
        container_name,
        "--label",
        f"org.astral-sh.astral-dev-toolchain.invocation={invocation}",
    ]

    if sys.stdin.isatty():
        command.append("--interactive")
    if sys.stdout.isatty():
        command.append("--tty")

    command.extend(
        [
            "--mount",
            f"type=bind,source={toolchain_dir},target={_CONTAINER_TOOLCHAIN_DIR},readonly",
            "--mount",
            f"type=bind,source={output_dir},target={_CONTAINER_OUTPUT_DIR}",
            image,
        ]
    )

    if args.verbose:
        command.append("--verbose")

    command.extend(
        [
            "--config",
            _CONTAINER_CONFIG,
            "--toolchain-dir",
            _CONTAINER_TOOLCHAIN_DIR,
            "build",
            "--output-dir",
            _CONTAINER_OUTPUT_DIR,
        ]
    )

    if args.dry_run:
        command.append("--dry-run")
    if args.select:
        command.extend(["--select", *args.select])

    logger.info("running toolchain build in %s", image)
    logger.debug("running: %s", shlex.join(command))

    process = subprocess.Popen(command)
    previous_handlers = {}
    received_signal = None

    def forward_signal(signum, _frame):
        nonlocal received_signal

        if process.poll() is not None:
            return
        received_signal = signum

        forwarded = subprocess.run(
            [
                docker,
                "stop",
                f"--signal={signal.Signals(signum).name}",
                "--timeout=1",
                container_name,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if forwarded.returncode != 0:
            try:
                process.send_signal(signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, forward_signal)

    try:
        returncode = process.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    if received_signal is not None:
        returncode = 128 + received_signal
    elif returncode < 0:
        returncode = 128 - returncode
    sys.exit(returncode)
