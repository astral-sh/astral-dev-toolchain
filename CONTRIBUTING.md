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

Each tool definition can declare smoke tests that run against the wheel for the
current platform:

```toml
[[test]]
name = "help"
run = ["some-tool", "--help"]
```

`run` is an argument array and is executed directly, without a shell. The
expected exit code defaults to zero; set `exitcode` when a test intentionally
returns another code:

```toml
[[test]]
name = "expected failure"
run = ["some-tool", "check"]
exitcode = 2
```

Tests are optional. Tools without tests are reported as skipped and are not
installed. To install wheels exclusively from `toolchain-dist/` and run all
configured tests for the current platform:

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

The configured `source.tag` must resolve to the exact commit in `source.commit`.
Builds validate this after cloning the source.

See the definitions under [toolchain](./toolchain/) for concrete examples.

### Updating tools

Opt a tool hosted in any Git repository into automatic updates in its `build.toml`:

```toml
[update]
enabled = true
tag-prefix = "cargo-audit/" # Optional; an initial v is stripped automatically.
```

The updater selects newer stable `major.minor.patch` tags, including major
upgrades, after observing the same tag and commit for seven days. Observations
are stored in `build.toml`; moved or deleted candidate tags restart their cooldown.
Commit and tag dates do not affect the cooldown.
Omit `[update]` or set `enabled = false` to update a tool manually.

Local updates use `git ls-remote --tags --sort=v:refname`, with Git's configured
credentials and transports. No local clone or hosting-service API is required:

```shell
uv run astral-dev-toolchain update --select cargo-audit --dry-run
uv run astral-dev-toolchain update
```

Dry runs contact upstream but do not save observations or change pins. Normal
runs preserve TOML comments and reset `tool.rebuild` when updating a pin. Use
`-v` before `update` for request timings. Run updater tests with `uv run --dev pytest`.

The daily **Toolchain update** workflow uses `gh` to keep observations on
`update/<tool>` branches and open draft PRs after the cooldown. Enable GitHub Actions PR
creation in repository settings. Untouched drafts refresh automatically;
marking a PR **ready for review** starts CI and stops bot edits. Human branch
edits also stop refreshes. Closing an unmerged PR suppresses that version.

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

In addition, `version` is required when using the Go backend, since Go packages
do not convey their own version.
