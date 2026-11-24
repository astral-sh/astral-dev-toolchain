"""Build dependency-free Rust and Go examples with the toolchain."""

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import tomlkit

_GO_SOURCE = """\
package main

import "fmt"

func main() {
\tfmt.Println("Hello, world!")
}
"""


def git(directory: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=directory, text=True).strip()


def prepare_tool(directory: Path, backend: str) -> None:
    # The builder clones a pinned revision. A Git bundle lets it clone from
    # the read-only /toolchain mount without a remote repository.
    (directory / "README.md").write_text(f"# {directory.name}\n")
    git(directory, "init", "-b", "hello")
    git(directory, "add", ".")
    git(
        directory,
        "-c",
        "user.name=CI",
        "-c",
        "user.email=ci@example.com",
        "commit",
        "-m",
        "hello",
    )
    git(directory, "bundle", "create", "source.bundle", "hello")

    definition = {
        "version": 1,
        "tool": {
            "backend": backend,
            "name": directory.name,
            "version": "0.1.0",
            "repo": f"/toolchain/{directory.name}/source.bundle",
        },
        "source": [{"tag": "hello", "commit": git(directory, "rev-parse", "HEAD")}],
    }
    (directory / "build.toml").write_text(tomlkit.dumps(definition))


def main() -> None:
    with TemporaryDirectory() as examples:
        rust = Path(examples) / "hello-rust"
        subprocess.run(["cargo", "new", "--bin", "--vcs", "none", rust], check=True)
        prepare_tool(rust, "rust")

        go = Path(examples) / "hello-go"
        go.mkdir()
        (go / "go.mod").write_text("module example.com/hello-go\n")
        (go / "main.go").write_text(_GO_SOURCE)
        prepare_tool(go, "go")

        subprocess.run(
            ["astral-dev-toolchain", "--toolchain-dir", examples, "build"], check=True
        )


if __name__ == "__main__":
    main()
