#!/usr/bin/env bash
set -euo pipefail

examples=$(mktemp -d)
trap 'rm -rf "$examples"' EXIT

cargo new --bin --vcs none "$examples/hello-rust"
mkdir "$examples/hello-go"
echo 'module example.com/hello-go' > "$examples/hello-go/go.mod"
cat > "$examples/hello-go/main.go" <<'GO'
package main

import "fmt"

func main() {
	fmt.Println("Hello, world!")
}
GO

# The builder clones a pinned revision. Git bundles let it clone the examples
# from the read-only /toolchain mount without a remote repository.
for backend in rust go; do
  tool="hello-$backend"
  dir="$examples/$tool"
  echo "# $tool" > "$dir/README.md"
  git init -b hello "$dir"
  git -C "$dir" add .
  git -C "$dir" -c user.name=CI -c user.email=ci@example.com commit -m hello
  git -C "$dir" bundle create source.bundle hello

  cat > "$dir/build.toml" <<TOML
version = 1

[tool]
backend = "$backend"
name = "$tool"
version = "0.1.0"
repo = "/toolchain/$tool/source.bundle"

[[source]]
tag = "hello"
commit = "$(git -C "$dir" rev-parse HEAD)"
TOML
done

uv run --locked astral-dev-toolchain --toolchain-dir "$examples" build
