# astral-dev-toolchain

astral-dev-toolchain provides Astral's developer toolchain
as Python wheels.

See [FAQ.md](./FAQ.md) for an explanation of why, how, and for whom
this is for.

## Usage

As an end user, you should be installing astral-dev-toolchain packages.

TODO.

## Development

Install the host-side CLI:

```shell
uv sync --locked
```

Build everything:

```shell
uv run astral-dev-toolchain build
```

Build artifacts are written to `toolchain-dist/` by default.

Build a specific tool:

```shell
uv run astral-dev-toolchain build --select just
```

See `--help` for additional options.

### Developing the bootstrap image

```shell
docker build \
    --target bootstrap-image \
    --tag astral-dev-toolchain-bootstrap:dev \
    .

uv run astral-dev-toolchain \
    --bootstrap-image astral-dev-toolchain-bootstrap:dev \
    build --dry-run --select just
```
