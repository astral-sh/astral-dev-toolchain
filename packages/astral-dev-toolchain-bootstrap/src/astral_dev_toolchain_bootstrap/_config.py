import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"missing or invalid [{key}] table")
    return value


@dataclass(frozen=True)
class RustConfig:
    toolchain: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class XwinConfig:
    architectures: tuple[str, ...]
    sdk_version: str
    crt_version: str
    http_retries: int
    include_debug_libs: bool
    include_debug_symbols: bool


@dataclass(frozen=True)
class ImageConfig:
    rust: RustConfig
    xwin: XwinConfig

    @classmethod
    def load(cls, path: Path) -> "ImageConfig":
        with path.open("rb") as stream:
            raw = tomllib.load(stream)

        if raw.get("version") != 1:
            raise ValueError(f"unsupported image configuration version: {raw.get('version')!r}")

        rust = _table(raw, "rust")
        xwin = _table(raw, "xwin")
        return cls(
            rust=RustConfig(
                toolchain=str(rust["toolchain"]),
                targets=tuple(str(target) for target in rust["targets"]),
            ),
            xwin=XwinConfig(
                architectures=tuple(str(arch) for arch in xwin["architectures"]),
                sdk_version=str(xwin["sdk-version"]),
                crt_version=str(xwin["crt-version"]),
                http_retries=int(xwin["http-retries"]),
                include_debug_libs=bool(xwin["include-debug-libs"]),
                include_debug_symbols=bool(xwin["include-debug-symbols"]),
            ),
        )
