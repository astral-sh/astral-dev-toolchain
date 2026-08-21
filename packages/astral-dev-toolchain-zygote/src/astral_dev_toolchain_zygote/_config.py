import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImageConfig:
    targets: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> ImageConfig:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)

        if raw.get("version") != 1:
            raise ValueError(
                f"unsupported image configuration version: {raw.get('version')!r}"
            )

        rust = raw.get("rust")
        if not isinstance(rust, dict):
            raise TypeError("missing or invalid [rust] table")

        return cls(targets=tuple(str(target) for target in rust["targets"]))
