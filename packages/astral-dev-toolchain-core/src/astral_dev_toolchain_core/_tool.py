"""Data models for tool definitions."""

import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
)

type _NonEmptyString = Annotated[StrictStr, StringConstraints(min_length=1)]


def _to_kebab(s: str) -> str:
    return s.replace("_", "-")


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_kebab, populate_by_name=True, extra="forbid"
    )


class ToolTest(_Model):
    """A smoke test for an installed tool wheel."""

    name: _NonEmptyString
    """A human-readable name for the test."""

    run: Annotated[tuple[_NonEmptyString, ...], Field(min_length=1)]
    """The executable and arguments to run, without shell interpretation."""

    exitcode: StrictInt = 0
    """The exact expected process exit code."""


class ToolCI(_Model):
    """CI settings for a tool."""

    shard: StrictBool = False
    """Build each target in a separate CI job."""

    exclude_targets: tuple[_NonEmptyString, ...] = Field(default_factory=tuple)
    """Targets to omit when the upstream tool cannot be built for them."""


class ToolObservation(_Model):
    """The first observation of an upstream candidate tag and commit."""

    commit: Annotated[
        StrictStr, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    ]
    first_seen: AwareDatetime


class ToolUpdate(_Model):
    """Opt-in discovery of newer upstream tags."""

    enabled: StrictBool
    """Discover newer tags through Git."""

    tag_prefix: StrictStr = ""
    """Literal prefix to remove before parsing a tag's version."""

    observed: dict[_NonEmptyString, ToolObservation] = Field(default_factory=dict)
    """Candidate tags and their first-seen timestamps, maintained by the updater."""


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
    """The source origin used to fetch a tool for building."""

    repo: StrictStr
    commit: StrictStr
    tag: StrictStr


class RustToolSpec(_ToolSpec):
    """Metadata and build settings for a Rust tool."""

    backend: Literal["rust"]

    version: StrictStr | None = None
    """The package version, defaulting to the version from `cargo metadata`."""

    manifest: StrictStr = "Cargo.toml"
    """The source-relative path to the tool's Cargo manifest."""

    no_default_features: StrictBool = False
    """Whether to pass `--no-default-features` to the Rust build."""

    features: tuple[_NonEmptyString, ...] = Field(default_factory=tuple)
    """Cargo features to enable for the Rust build."""

    cargo_config: StrictBool = True
    """
    Whether to preserve Cargo configuration from the pinned source checkout.

    This should only be disabled when that configuration conflicts with
    cross-compilation and the required settings are supplied explicitly.
    """

    env: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    """Environment overrides for this tool's Cargo and Maturin subprocesses."""


class GoToolSpec(_ToolSpec):
    """Metadata and build settings for a CGO-disabled Go tool."""

    backend: Literal["go"]

    version: Annotated[
        StrictStr, StringConstraints(strip_whitespace=True, min_length=1)
    ]
    """The explicit package version; Go modules do not declare their own version."""

    directory: StrictStr = "."
    """The Go executable's package directory, relative to the source checkout."""

    readme: StrictStr = "README.md"
    """The Markdown README, relative to the source checkout."""

    version_var: StrictStr | None = None
    """An optional Go string variable to set to tool.version using linker flags."""


type ToolSpec = Annotated[RustToolSpec | GoToolSpec, Field(discriminator="backend")]


class Tool[T: ToolSpec](_Model):
    """A tool definition in a `build.toml` file."""

    version: Literal[1] = 1
    tool: T
    source: ToolSource
    ci: ToolCI = Field(default_factory=ToolCI)
    update: ToolUpdate | None = None
    tests: tuple[ToolTest, ...] = Field(default_factory=tuple, alias="test")

    @classmethod
    def load(cls, path: Path) -> Tool:
        with path.open(mode="rb") as stream:
            raw = tomllib.load(stream)

        return cls.model_validate(raw)

    @property
    def python_package_name(self) -> str:
        return f"astral-dev-toolchain-{self.tool.name}"
