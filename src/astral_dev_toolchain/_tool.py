"""
Data models for tools.

In this project's parlance, a "tool" is an individual member of our toolchain,
one that gets turned into a Python wheel. Each "tool" can actually contain
multiple individual binaries/entry points.
"""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, StrictStr, ConfigDict, StrictBool


def _to_kebab(s: str) -> str:
    return s.replace("_", "-")


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=_to_kebab, populate_by_name=True)


class Tool(_Model):
    version: Literal[1]
    tool: ToolSpec
    source: ToolSource
    build: BuildSpec | None = None

    @classmethod
    def load(cls, path: Path) -> Tool:
        with path.open(mode="rb") as io:
            raw = tomllib.load(io)

        return Tool.model_validate(raw)

    @property
    def python_package_name(self) -> str:
        # TODO: Probably need to validate and/or normalize the name here.
        return f"astral-dev-toolchain-{self.tool.name}"


class ToolSpec(_Model):
    name: StrictStr


class ToolSource(_Model):
    repo: StrictStr
    commit: StrictStr
    tag: StrictStr


class BuildSpec(_Model):
    manifest: StrictStr
    no_default_features: StrictBool = False
