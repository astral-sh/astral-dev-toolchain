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

GitHub-hosted tools can opt into automatic updates:

```toml
[update]
github = true
tag-prefix = "cargo-audit/" # Optional literal prefix; defaults to empty.
```

The updater strips the prefix and one optional leading `v`, then considers only
stable `major.minor.patch` tags. It selects the highest newer version, including
major upgrades. Prereleases and unrelated tags are ignored. Omit `[update]` or
set `github = false` to keep a tool manual. Codeberg is not supported yet.

Each candidate tag and resolved commit must have been observed at least seven
days earlier. Observations live in the tool's own `build.toml`:

```toml
[update.observed."cargo-audit/v0.23.0"]
commit = "0123456789abcdef0123456789abcdef01234567"
first-seen = 2026-09-08T06:17:00Z
```

The updater maintains these entries automatically. A changed commit restarts
its clock; a tag missing from a complete scan loses its observation and must
wait again if it returns. Incomplete scans preserve the file. Commit dates and
release dates do not shorten the cooldown. Polling cannot detect a change that
happens and reverts between scans.

The host CLI needs Git, but does not need Docker, Rust, or Go:

```shell
# Record observations and apply any updates already eligible.
uv run astral-dev-toolchain update

# Preview a selected tool without changing its build.toml.
uv run astral-dev-toolchain update --select cargo-audit --dry-run
```

Normal runs save observations directly in the selected tools' definitions;
keep those changes for subsequent runs. Dry runs do not start persisted
cooldowns. `GH_TOKEN` (or `GITHUB_TOKEN`) can authenticate API reads; without
either, GitHub's unauthenticated rate limit applies. The CLI only changes local
files and never creates branches or PRs.

Discovery uses one `git ls-remote --tags` invocation per repository, using the
selected tools' common tag prefix. Git returns peeled IDs for annotated tags
without separate API requests. Pinned tags and eligible candidates are still
rechecked through the GitHub API. Git commands time out after 30 seconds and
never prompt for credentials; `GH_TOKEN` or `GITHUB_TOKEN` also authenticates Git
reads. Dry runs still contact upstream. Progress is logged per repository; use
`astral-dev-toolchain -v update --dry-run` to see Git and API request timings.

An eligible update changes the tag and pinned commit together, preserves TOML
comments, and removes `tool.rebuild`. An explicit `tool.version` is updated only
when it matches the old tag's version; a custom override needs manual handling.
The selected version's observation is retained, alongside any newer candidates.
Missing or moved currently pinned tags also need manual review. The build's
existing tag/commit verification catches tags that move after a proposal.

#### Scheduled updates and review

The **Toolchain update** workflow runs daily at 06:17 UTC. Its manual dispatch
defaults to a read-only preview. It checks out the default branch and uses the
built-in token; the repository must allow GitHub Actions to create pull requests.
There are no additional credentials to configure.

When newer tags appear, CI creates `codex/update/<tool>` and records observations
in that branch's `build.toml`. The selected source pin stays unchanged during
the cooldown. No PR is opened until a newer version qualifies. There is no
separate state file or global state branch, and discovery never writes to `main`.

Each qualifying update opens one draft PR per tool, with version and commit
changes, timestamps, and an upstream comparison link. The bot refreshes untouched
drafts as newer versions become eligible. Observations are included in the PR
and travel with the definition when it is merged.

Mark a PR **ready for review** to start normal CI, including the affected tool's
builds and smoke tests. Both CI and zizmor listen for `ready_for_review` alongside
their usual PR events. The bot checks GitHub's PR history and the branch's signed
bot commits before writing. It stops after a PR has been marked ready, including
if it is later converted back to draft, or when a human edits the branch.

Once a PR is resolved, the tool can receive another proposal. Closing without
merging suppresses that version and older versions. A newer eligible version
can open another draft. Human commits made after closure are preserved; branches
containing changes outside that tool's definition need manual cleanup before
reuse. The workflow never merges or publishes packages.

The workflow serializes runs and saves each definition with an atomic GitHub
commit that checks the expected branch head. A concurrent human commit prevents
the update. If a response is lost, the next run reads the branch and existing
PRs to recover. A failed write prevents that tool's PR from being opened;
independent tools can still proceed. API calls and retries are bounded, and
errors appear in the Actions summary and fail the job.

The first normal run records observations for newer tags; expect drafts at
least seven days later. Mark the first draft ready and verify CI starts and
subsequent updater runs leave it alone. The workflow only operates from a
checkout matching the default branch, so deployment begins after merging it.
Deleting a pending branch loses observations that have not reached `main` and
restarts those cooldowns. Malformed observations require fixing the affected
`build.toml`; failed scans do not silently reset them.

#### Updater tests

The offline suite uses simulated time and in-memory GitHub responses:

```shell
uv run --dev pytest
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

In addition, `version` is required when using the Go backend, since Go packages
do not convey their own version.
