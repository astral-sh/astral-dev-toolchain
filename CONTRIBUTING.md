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

Build only a subset of targets with one or more `--target` flags:

```shell
uv run astral-dev-toolchain build --select cargo-nextest \
    --target aarch64-apple-darwin --target x86_64-apple-darwin
```

See `--help` for additional options.

## Testing built tools

By default, smoke tests run `--help` on each executable in `tool.binaries` and
expect exit code zero. `tool.binaries` defaults to a list containing the tool name.
Define `[[test]]` entries to replace the generated tests. The runner executes
`run` as an argument array without a shell. Set `exitcode` when a test
intentionally returns a different code:

```toml
[[test]]
name = "expected failure"
run = ["some-tool", "check"]
exitcode = 2
```

Set `test = []` at the top level to skip smoke tests for a tool. To install wheels
exclusively from `toolchain-dist/` and run tests for the current platform:

```shell
uv run astral-dev-toolchain test
```

Use `--select` to test the same subset passed to `build`, or `--dist-dir` to
read wheels from another directory:

```shell
uv run astral-dev-toolchain test --select cargo-hack just
```

## Tool definitions

Each `build.toml` tells astral-dev-toolchain how to build a tool in the toolchain.

At the moment, Go and Rust builds are supported. Other backends may be added in
the future.

Set `tool.repo` to the upstream Git repository. The first `[[source]]` entry
pins the tag and commit used for builds. Builds verify that the tag resolves to
that exact commit after cloning the repository.

CI builds each target in a separate job by default. Set `ci.shard = false` to
build all targets for a tool in one job.

See the definitions under [toolchain](./toolchain/) for concrete examples.

### Updating tools

Automatic updates are enabled by default. Set `update.enabled = false` to
disable automatic updates for a tool. If its upstream tags have a custom prefix,
configure it in `build.toml`:

```toml
[update]
tag-prefix = "cargo-audit/" # Optional; an initial v is stripped automatically.
```

The updater records candidates in additional `[[source]]` entries with
`first-seen` timestamps. When it promotes a candidate to the first entry, it
keeps that timestamp. A manual pin may omit `first-seen`.

Updates are normally performed automatically in CI, but can also be run locally:

```shell
uv run astral-dev-toolchain update --select cargo-audit --dry-run
uv run astral-dev-toolchain update
```

### Rust backend

If you set `tool.backend = "rust"`, the following backend options become available:

- `tool.manifest` overrides the repo-relative path to the `Cargo.toml` manifest
  that maturin uses. By default, this is just `Cargo.toml`, but workspace-style
  repos may need a custom path.
- `tool.no-default-features` passes `--no-default-features` to the build.
  By default this is `false` (i.e. default features are enabled).
- `tool.env` is a table of string environment overrides for the tool's Cargo
  metadata and Maturin subprocesses. These are merged into the inherited
  environment without affecting other tools or the parent process. Names and
  values are passed literally, without shell expansion.

For example, cargo-edit needs a compiler driver override for Windows ARM64:

```toml
[tool.env]
"CFLAGS_aarch64-pc-windows-msvc" = "--driver-mode=cl"
```

The target-qualified variable limits the compiler flag to Windows ARM64. Its
hyphenated spelling is intentional: cargo-xwin sets the underscored variant,
and the `cc` crate combines both, preserving the existing SDK include flags.
This keeps cargo-xwin's default backend and the SDK/CRT already in the image.

### Go backend

If you set `tool.backend = "go"`, the following backend options become available:

- `version-var` controls the Go variable to populate with a version, if the build
  expects one. For example, `version-var = "main.version"` will results in
  Go emitting `-X main.version="..."` as a linker flag.
- `directory` overrides the repo-relative path to the source directory.
  By default this is `.`, but repos with a `cmd/...` or similar layout
  may need a custom path.
- `readme` overrides the repo-relative path to the Markdown README.
  By default this is `README.md`. The build prepends the same provenance
  notice used for Rust tools.

In addition, `version` is required when using the Go backend, since Go packages
do not convey their own version.
