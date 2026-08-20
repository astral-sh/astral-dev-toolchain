import logging
from dataclasses import dataclass

from ._plan import Plan
from ._tool import Tool

logger = logging.getLogger(__name__)


@dataclass(kw_only=True, frozen=True)
class Toolchain:
    tools: list[Tool]

    @classmethod
    def load(cls, plan: Plan) -> "Toolchain":
        tools = []

        for tool_dir in plan.toolchain_dir.iterdir():
            if not tool_dir.is_dir():
                logger.warning("non-directory in toolchain dir: %s", tool_dir)
                continue

            tool = Tool.load(tool_dir / "build.toml")
            if not plan.selected_tools or tool.tool.name in plan.selected_tools:
                tools.append(tool)

        tools.sort(key=lambda tool: tool.tool.name)
        return cls(tools=tools)
