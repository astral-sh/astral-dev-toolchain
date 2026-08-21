import argparse
import logging
from pathlib import Path

from ._bootstrap import Bootstrapper
from ._config import ImageConfig

_LOG_FORMAT = "%(levelname)s %(message)s"


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/app/toolchain-image.toml"),
        help="Image toolchain configuration",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=_LOG_FORMAT,
        force=True,
    )

    config = ImageConfig.load(args.config)
    Bootstrapper.discover().bootstrap(config)
