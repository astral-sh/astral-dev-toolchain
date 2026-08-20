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
    version: Literal[1]
    tool: "ToolSpec"
    source: "ToolSource"
    build: "BuildSpec | None" = None

    @classmethod
    def load(cls, path: Path) -> "Tool":
        with path.open(mode="rb") as stream:
            raw = tomllib.load(stream)

        return cls.model_validate(raw)

    @property
    def python_package_name(self) -> str:
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
