"""Package metadata shared across build backends."""

from ._tool import Tool

_README_NOTICE = """\
> **Build provenance:** This package is built and distributed by [Astral](https://astral.sh) as part of [`astral-dev-toolchain`](https://github.com/astral-sh/astral-dev-toolchain).
> It packages [`{tool_name}`]({repo}) from tag `{tag}` at commit `{commit}`.
"""


def readme_notice(tool: Tool) -> str:
    return _README_NOTICE.format(
        tool_name=tool.tool.name,
        repo=tool.source.repo,
        tag=tool.source.tag,
        commit=tool.source.commit,
    )
