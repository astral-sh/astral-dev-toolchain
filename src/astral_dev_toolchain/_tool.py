"""
Data models for tools.

In this project's parlance, a "tool" is an individual member of our toolchain,
one that gets turned into a Python wheel. Each "tool" can actually contain
multiple individual binaries/entry points.
"""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, StrictStr


class Tool(BaseModel):
    version: Literal[1]
    tool: ToolSpec
    source: ToolSource

    @classmethod
    def load(cls, path: Path) -> Tool:
        with path.open(mode="rb") as io:
            raw = tomllib.load(io)

        return Tool.model_validate(raw)


class ToolSpec(BaseModel):
    name: StrictStr


class ToolSource(BaseModel):
    repo: StrictStr
    commit: StrictStr
    tag: StrictStr
