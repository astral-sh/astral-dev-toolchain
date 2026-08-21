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