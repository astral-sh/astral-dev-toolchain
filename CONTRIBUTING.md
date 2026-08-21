# Contributing

## Linting and formatting

Setup:

```shell
# important: sync all packages to make sure Ruff and ty can see them
uv sync --locked --all-packages
```

Linting:

```shell
uv run --dev ruff check
uv run --dev ty check
```

Formatting and fixing:

```shell
uv run --dev ruff check --fix
uv run --dev ruff format
uv run --dev ty check --fix
```

## Developing the bootstrap image

In general, the bootstrap image does not need to be rebuilt
unless the core bootstrapping dependencies actually change.

```shell
docker build \
    --target bootstrap-image \
    --tag astral-dev-toolchain-bootstrap:dev \
    .

uv run astral-dev-toolchain \
    --bootstrap-image astral-dev-toolchain-bootstrap:dev \
    build --dry-run --select just
```

## Building tools

Install the host-side CLI:

```shell
uv sync --locked
```

Build all tools in the toolchain (note that this will be very slow):

```shell
uv run astral-dev-toolchain build
```

Build a specific tool:

```shell
uv run astral-dev-toolchain build --select cargo-hack
```

Build artifacts are written to `toolchain-dist/` by default.

See `--help` for additional options.

## Tool definitions

Each `build.toml` tells astral-dev-toolchain how to build a tool in the toolchain.

At the moment, Go and Rust builds are supported. Other backends may be added in
the future.

See the definitions under [toolchain](./toolchain/) for concrete examples.

### Rust backend

If you set `tool.backend = "rust"`, the following backend options become available:

- `tool.mainfest` overrides the repo-relative path to the `Cargo.toml` manifest
  that maturin uses. By default, this is just `Cargo.toml`, but workspace-style
  repos may need a custom path.
- `tool.no-default-features` passes `--no-default-features` to the build.
  By default this is `false` (i.e. default features are enabled).

### Go backend

If you set `tool.backend = "go"`, the following backend options become available:

- `version-var` controls the Go variable to populate with a version, if the build
  expects one. For example, `version-var = "main.version"` will results in
  Go emitting `-X main.version="..."` as a linker flag.
- `directory` overrides the repo-relative path to the source directory.
  By default this is `.`, but repos with a `cmd/...` or similar layout
  may need a custom path.

In addition, `version` is required when using the Go backend, since Go packages
do not convey their own version.
