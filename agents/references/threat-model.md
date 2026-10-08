# astral-dev-toolchain threat model

## Scope

This repository builds selected Rust and Go developer tools as Python wheels.
It provisions a bootstrap image, builds tools in a zygote container, tests the
wheels on native runners, discovers upstream updates, and publishes to PyPI and
GHCR. Apply this model to changes in `packages/`, `toolchain/`, `image/`,
`Dockerfile`, `scripts/`, `.github/workflows/`, and their dependency and test
configuration.

## Trust and attacker control

Maintainers, their explicit dispatch inputs, reviewed and merged definitions,
protected refs, configured runners and environments, and intentionally selected
build tools are trusted. A contributor's PR files, metadata, and artifacts
remain untrusted before review. Third-party upstream tag names, tag targets,
source files, dependency metadata, and build outputs can change independently
of this repository.

Compiling an intentionally selected upstream revision, running its build scripts,
or executing a declared smoke test is expected behavior. A finding needs a
concrete boundary crossing: an independent attacker must gain access, integrity
impact, or privilege they did not already have. Do not report mere use of
mutable inputs, privileged permissions, or third-party dependencies without a
path from attacker control to harm.

## Build and artifact integrity

- Tool source tags must resolve to the exact commit in `build.toml` before any
  backend handles that source. Updates to a pin are reviewed proposals, not
  authorization for upstream to replace it later.
- The host mounts definitions read-only at `/toolchain` and an explicit output
  directory at `/dist`. Upstream code must not gain host mounts, the Docker
  socket, host credentials, or write access outside the intended outputs.
- Compilers and build backends execute source code by design. Review changes
  that make upstream paths or metadata read unrelated host files or inject
  commands outside the build context. Check path traversal and symlinks where
  README or source files are copied into wheels.
- Wheel metadata, package names, versions, provenance and targets must
  correspond to the selected reviewed definition. An attacker-controlled
  artifact must not replace a different tool's wheel or be promoted as a
  trusted build from another revision.
- Native smoke tests execute the configured commands after installing local
  wheels. They must not obtain publishing credentials, repository write access,
  or pull unintended packages from a network index instead of testing the
  selected build.

## Updates, CI, and publishing

- The scheduled updater processes remote tags while holding repository write
  and PR write permissions. Treat remote refs as data, not shell commands,
  paths, or workflow instructions. Preserve the seven-day observation period,
  reset it if a tag moves, recheck before proposing, and stop if the current
  pin no longer matches upstream. Observation or update branches must not
  overwrite human edits or expand writes beyond the intended tool definition.
- Ordinary PR builds and lint run proposed repository code. Forks and
  unreviewed artifacts must not acquire release tokens or influence a
  privileged job's checkout, caches, commands or published artifacts.
- PyPI publishing is authorized by a merged PR labeled `publish-after-merge`,
  or by an explicit non-dry-run dispatch. GHCR bootstrap publishing follows a
  `bootstrap-v*` tag. Evaluate trigger changes against those authorization
  boundaries. A build or smoke job must not run with publishing credentials.
- The security review runs only for PRs whose head is in this repository.
  Its setup executes before the sandbox; widening the trigger can expose
  credentials or a privileged runner to untrusted setup code. Keep its threat
  models and helpers from the workflow revision and shared workflow pin, and
  publish findings only for the reviewed PR head.

## Severity

Prioritize paths to unauthorized PyPI/GHCR releases, repository write access,
credential exposure, or attacker-chosen wheels installed by downstream users.
Bounded errors in trusted builds and intended local maintainer actions alone are
not security findings. Report regressions introduced by the PR, with the input,
crossing, and impact demonstrated.
