import logging
from dataclasses import dataclass
from pathlib import Path

from astral_dev_toolchain._tool import Tool

logger = logging.getLogger(__name__)


@dataclass(kw_only=True, frozen=True)
class Toolchain:
    tools: list[Tool]

    @classmethod
    def load(self, toolchain_dir: Path) -> Toolchain:
        tools = []

        for tool_dir in toolchain_dir.iterdir():
            if not tool_dir.is_dir():
                logger.warning(f"non-directory in toolchain dir: {tool_dir}")
                continue

            config_path = tool_dir / "build.toml"
            tools.append(Tool.load(config_path))

        tools.sort(key=lambda tool: tool.tool.name)

        return Toolchain(tools=tools)
