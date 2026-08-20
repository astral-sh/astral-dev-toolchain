# astral-dev-toolchain

astral-dev-toolchain provides Astral's developer toolchain
as Python wheels.

See [FAQ.md](./FAQ.md) for an explanation of why, how, and for whom
this is for.

## Usage

As an end user, you should be installing astral-dev-toolchain packages.

TODO.

## Development

Bootstrap the toolchain environment:

```shell
docker build -t astral-dev-toolchain .
```

Build everything (very slow):

```shell
docker run --rm -it -v "$(pwd)/dist:/dist" astral-dev-toolchain build
```

Build a specific tool:

```
docker run --rm -it -v "$(pwd)/dist:/dist" astral-dev-toolchain build --select just
```
