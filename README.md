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

Build a specific tool:

```shell
uv run astral-dev-toolchain build --select just
```

The host CLI builds a small local zygote image on top of the published bootstrap
image, mounts `./toolchain` read-only into the container, and writes wheels to
`./dist`. Repeated invocations reuse Docker's cache, so an unchanged zygote build
is effectively a cache check.

Override the mounted host paths with `--toolchain-dir` and `build --output-dir`,
respectively. Pass `-v` before `build` to show the Docker commands and enable
debug logging inside the zygote. A prebuilt zygote image can bypass the local
build with `--image`.

### Developing the zygote

Edit the zygote package and rerun the normal command. Only its final application
layer is rebuilt:

```shell
uv run astral-dev-toolchain build --dry-run --select just
```

### Developing the bootstrap image

The bootstrap image contains the system packages, Python, Rust targets, xwin
cache, and macOS SDK. Build it explicitly and point the host CLI at it:

```shell
docker build \
    --target bootstrap-image \
    --tag astral-dev-toolchain-bootstrap:dev \
    .
uv run astral-dev-toolchain \
    --bootstrap-image astral-dev-toolchain-bootstrap:dev \
    build --dry-run --select just
```

Bootstrap images are published from `bootstrap-vX.Y.Z` tags. The tag must match
`bootstrap-image.version` in `image/toolchain.toml`; no rolling `latest` tag is
published.

The repository root is a virtual uv workspace harness. The host CLI,
build-time bootstrapper, and container-side zygote are peers under `packages/`.
Tool definitions are runtime inputs and are intentionally excluded from both
Docker image targets.
