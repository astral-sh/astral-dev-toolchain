"""Select tool builds from the GitHub Actions event and emit a JSON matrix."""

import json
import os
import subprocess
from pathlib import Path

from astral_dev_toolchain_core import Tool


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    event_name = os.environ["GITHUB_EVENT_NAME"]
    definitions = {
        path: Tool.load(path) for path in Path("toolchain").glob("*/build.toml")
    }
    by_name = {tool.tool.name: tool for tool in definitions.values()}

    override = os.environ.get("SELECT", "")
    if override:
        selected = [tool.strip() for tool in override.split(",")]
        unknown = sorted(set(selected) - by_name.keys())
        if unknown:
            raise SystemExit(f"Unknown tools: {', '.join(map(repr, unknown))}")
    elif event_name == "workflow_dispatch":
        tool = event["inputs"]["tool"]
        if tool not in by_name:
            raise SystemExit(f"Unknown tool: {tool!r}")
        selected = [tool]
    elif event_name == "push" and event["before"] == "0" * 40:
        # A newly created branch has no previous commit to compare against.
        selected = list(by_name)
    else:
        if event_name == "pull_request":
            base = event["pull_request"]["base"]["sha"]
            head = event["pull_request"]["head"]["sha"]
            revision_range = f"{base}...{head}"
        else:
            revision_range = f"{event['before']}..{event['after']}"

        changed = subprocess.check_output(
            [
                "git",
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                "--diff-filter=d",
                revision_range,
                "--",
                "toolchain/*/build.toml",
            ],
        )
        selected = [
            definitions[Path(path)].tool.name
            for path in changed.decode().split("\0")
            if Path(path) in definitions
        ]

    tools = sorted(set(selected))
    matrix = [{"tool": tool, "shard": by_name[tool].ci.shard} for tool in tools]
    print(f"Selected tools: {', '.join(tools) or '(none)'}")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        print(f"tools={json.dumps(tools)}", file=output)
        print(f"matrix={json.dumps(matrix)}", file=output)


if __name__ == "__main__":
    main()
