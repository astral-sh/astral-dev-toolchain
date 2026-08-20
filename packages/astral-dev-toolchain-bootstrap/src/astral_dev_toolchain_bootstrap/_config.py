import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


class _ConfigModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


class RustConfig(_ConfigModel):
    toolchain: StrictStr
    targets: tuple[StrictStr, ...]


class XwinConfig(_ConfigModel):
    architectures: tuple[StrictStr, ...]
    sdk_version: StrictStr = Field(alias="sdk-version")
    crt_version: StrictStr = Field(alias="crt-version")
    http_retries: StrictInt = Field(alias="http-retries", ge=0)
    include_debug_libs: StrictBool = Field(alias="include-debug-libs")
    include_debug_symbols: StrictBool = Field(alias="include-debug-symbols")


class ImageConfig(_ConfigModel):
    version: Literal[1]
    rust: RustConfig
    xwin: XwinConfig

    @classmethod
    def load(cls, path: Path) -> "ImageConfig":
        with path.open("rb") as stream:
            return cls.model_validate(tomllib.load(stream))
