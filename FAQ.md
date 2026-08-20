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

astral-dev-toolchain essentially wraps [maturin], which in turn wraps
several other things, to munge freestanding Rust binary builds into
wheels.

[maturin]: https://www.maturin.rs/

The host-side `astral-dev-toolchain` command builds and executes a thin local
Docker image containing `astral-dev-toolchain-zygote`, which turns the small
`build.toml` metadata format into maturin builds.

That local image derives from a separately versioned, published bootstrap image.
The bootstrap contains the expensive and slow-moving cross-compilation state:
system dependencies, Rust targets, the Windows SDK cache, and the macOS SDK.
Zygote source changes therefore rebuild only a small application layer, while
tool definitions are mounted at runtime and require no image build at all.

## Who is this for?

Two audiences:

- Members of Astral's development team;
- Open source contributors working on Astral's tools.

If you aren't in one of these audiences, this toolchain is **probably not for you**.
We make no representations about its stability, suitability, etc. outside of our
specific use case.
