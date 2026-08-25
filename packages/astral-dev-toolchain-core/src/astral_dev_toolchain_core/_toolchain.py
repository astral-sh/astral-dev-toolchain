import logging
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from ._tool import Tool

logger = logging.getLogger(__name__)


@dataclass(kw_only=True, frozen=True)
class Toolchain:
    tools: list[Tool]

    @classmethod
    def load(
        cls, toolchain_dir: Path, selected_tools: Collection[str] = ()
    ) -> Toolchain:
        tools = []
        selected = set(selected_tools)

        for tool_dir in toolchain_dir.iterdir():
            if not tool_dir.is_dir():
                logger.warning("non-directory in toolchain dir: %s", tool_dir)
                continue

            tool = Tool.load(tool_dir / "build.toml")
            if not selected or tool.tool.name in selected:
                tools.append(tool)

        tools.sort(key=lambda tool: tool.tool.name)
        return cls(tools=tools)
