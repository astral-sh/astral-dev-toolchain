"""Data models for tool definitions."""

import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
)


def _to_kebab(s: str) -> str:
    return s.replace("_", "-")


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_kebab, populate_by_name=True, extra="forbid"
    )


class _ToolSpec(_Model):
    """Metadata shared by Rust and Go tools."""

    name: StrictStr
    """
    The tool's package name.

    This does not affect binary names; it serves as the suffix for the package name.
    For example, a tool named `foo` will become `astral-dev-toolchain-foo`.
    """

    rebuild: StrictInt | None = None
    """
    The tool's rebuild number.

    This becomes a "postrelease" number on the Python side, e.g. `1` becomes
    `{version}.post1`. Most tools will not need rebuilds.
    """


class ToolSource(_Model):
    """
    The tool's source origin. This is used to fetch the tool for building.
    """

    repo: StrictStr
    commit: StrictStr
    tag: StrictStr


class RustToolSpec(_ToolSpec):
    """Metadata and build settings for a Rust tool."""

    backend: Literal["rust"]

    version: StrictStr | None = None
    """The package version, defaulting to the version from `cargo metadata`."""

    manifest: StrictStr = "Cargo.toml"
    """
    This is a relative path (within the source checkout) to the tool's `Cargo.toml`,
    if the source checkout uses a workspace-style layout or otherwise doesn't expose the
    right `Cargo.toml` at the repo root.
    """

    no_default_features: StrictBool = False
    """
    Whether to pass `--no-default-features` to the Rust build.
    """


class GoToolSpec(_ToolSpec):
    """Metadata and build settings for a CGO-disabled Go tool."""

    backend: Literal["go"]

    version: Annotated[
        StrictStr, StringConstraints(strip_whitespace=True, min_length=1)
    ]
    """The explicit package version; Go modules do not declare their own version."""

    directory: StrictStr = "."
    """The Go executable's package directory, relative to the source checkout."""

    version_var: StrictStr | None = None
    """An optional Go string variable to set to tool.version using linker flags."""


type ToolSpec = Annotated[RustToolSpec | GoToolSpec, Field(discriminator="backend")]


class Tool[T: ToolSpec](_Model):
    """A tool definition in a `build.toml` file."""

    version: Literal[1] = 1
    tool: T
    source: ToolSource

    @classmethod
    def load(cls, path: Path) -> Tool:
        with path.open(mode="rb") as stream:
            raw = tomllib.load(stream)

        return cls.model_validate(raw)

    @property
    def python_package_name(self) -> str:
        return f"astral-dev-toolchain-{self.tool.name}"
