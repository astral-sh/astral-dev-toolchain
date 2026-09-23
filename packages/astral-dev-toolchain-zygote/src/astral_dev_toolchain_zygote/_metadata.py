"""Package metadata shared across build backends."""

from pathlib import Path

from astral_dev_toolchain_core import Tool

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


def prepare_readme(tool: Tool, readme: Path, build_dir: Path) -> Path:
    if readme.suffix.lower() != ".md":
        raise ValueError(f"unsupported README format for {tool.tool.name}: {readme}")

    generated = build_dir / "README.md"
    generated.write_text(
        readme_notice(tool) + "\n---\n\n" + readme.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return generated
