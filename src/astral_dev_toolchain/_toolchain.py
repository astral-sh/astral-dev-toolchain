import logging
from dataclasses import dataclass

from astral_dev_toolchain._plan import Plan
from astral_dev_toolchain._tool import Tool

logger = logging.getLogger(__name__)


@dataclass(kw_only=True, frozen=True)
class Toolchain:
    tools: list[Tool]

    @classmethod
    def load(self, plan: Plan) -> Toolchain:
        tools = []

        for tool_dir in plan.toolchain_dir.iterdir():
            if not tool_dir.is_dir():
                logger.warning(f"non-directory in toolchain dir: {tool_dir}")
                continue

            config_path = tool_dir / "build.toml"

            tool = Tool.load(config_path)
            if not plan.selected_tools or tool.tool.name in plan.selected_tools:
                tools.append(tool)

        tools.sort(key=lambda tool: tool.tool.name)

        return Toolchain(tools=tools)
