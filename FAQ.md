# FAQ

## Why does this exist?

astral-dev-toolchain exists to give [Astral] more control over its development tooling,
_especially_ non-Python tooling written in compiled languages like Rust.

[Astral]: https://github.com/astral-sh

The basic shape of the problem is:

- We want to depend on tools like [cargo-shear];
- But we _don't_ want to waste CI/CD time building them;
- And we _don't_ want to install them on an ad-hoc basis through
  `curl | bash` and similar;
- And we _don't_ want to trust (most) third parties to rebuild for us.

[cargo-shear]: https://crates.io/crates/cargo-shear

Consequently, we need a way to reliably install our own binary builds.
And fortunately for us, uv is very good at installing binaries,
_as long_ as they are shaped like Python wheels.

## How does it work?

astral-dev-toolchain wraps [maturin] for Rust tools and [radlermass] for
CGO-disabled Go tools to package their binary builds as Python wheels.

[maturin]: https://www.maturin.rs/
[radlermass]: https://pypi.org/project/radlermass/

Under the hood there are three main parts:

- A "bootstrap" image (`astral-dev-toolchain-bootstrap`), which contains
  the Rust and Go compilers, cross-compilation targets, and SDKs.
- A "zygote" image (`astral-dev-toolchain-zygote`), which includes maturin,
  radlermass, and the Zig toolchain (which maturin uses for cross builds).
- A "host" tool (`astral-dev-toolchain`), which is responsible for executing
  the "zygote" image and sending it toolchain definitions (as `build.toml`).

## Who is this for?

Two audiences:

- Members of Astral's development team;
- Open source contributors working on Astral's tools.

If you aren't in one of these audiences, this toolchain is **probably not for you**.
We make no representations about its stability, suitability, etc. outside of our
specific use case.
