"""Data models for tool definitions."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr


def _to_kebab(s: str) -> str:
    return s.replace("_", "-")


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=_to_kebab, populate_by_name=True)


class Tool(_Model):
    """
    Represents a tool definition in a `build.toml` file.
    """

    version: Literal[1] = 1
    tool: ToolSpec
    source: ToolSource
    build: BuildSpec | None = None

    @classmethod
    def load(cls, path: Path) -> Tool:
        with path.open(mode="rb") as stream:
            raw = tomllib.load(stream)

        return cls.model_validate(raw)

    @property
    def python_package_name(self) -> str:
        return f"astral-dev-toolchain-{self.tool.name}"


class ToolSpec(_Model):
    """
    A tool "spec" contains information that affects wheel metadata for the tool
    being built.
    """

    name: StrictStr
    """
    The tool's package name.

    This does not affect binary names; it serves as the suffix for the package name.
    For example, a tool named `foo` will become `astral-dev-toolchain-foo`.
    """

    version: str | None = None
    """
    The tool's package version.

    By default, this is extracted from the tool's source tree using `cargo metadata`.
    However, it can be explicitly overridden as well.
    """


class ToolSource(_Model):
    """
    The tool's source origin. This is used to fetch the tool for building.
    """

    repo: StrictStr
    commit: StrictStr
    tag: StrictStr


class BuildSpec(_Model):
    """
    The tool's build specification. This is used to tweak the tool's build.
    """

    manifest: StrictStr
    """
    This is a relative path (within the source checkout) to the tool's `Cargo.toml`,
    if the source checkout uses a workspace-style layout or otherwise doesn't expose the
    right `Cargo.toml` at the repo root.
    """

    no_default_features: StrictBool = False
    """
    Whether to pass `--no-default-features` to the build.
    """
