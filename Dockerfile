# Global state.
ARG UV_VERSION=0.12.5
ARG UV_DOCKER_HASH=sha256:143b40f4ab56a780f43377604702107b5a35f83a4453daf1e4be691358718a6a

ARG UBUNTU_SNAPSHOT=20260127T200000Z

ARG RUST_VERSION=1.93.0

ARG CARGO_XWIN_VERSION=0.21.4
ARG XWIN_SDK_VERSION=10.0.22621
ARG XWIN_CRT_VERSION=14.44.17.14

ARG MACOSX_SDK_SHA=041600eda65c6a668f66cb7d56b7d1da3e8bcc93

FROM ubuntu:26.04@sha256:4095ef613201918336b5d7d00be15d8b09c72ddb77c80bca249c255887a64d87 AS ubuntu-build-deps

ARG RUST_VERSION
ARG UBUNTU_SNAPSHOT

# Install dependencies for the installation for dependencies
# ca-certificates are required for using the snapshot
RUN --mount=type=cache,id=astral-ubuntu-apt-lists-v2,target=/var/lib/apt/lists,sharing=locked \
    apt install -y --update ca-certificates && \
    apt install -y --update --snapshot ${UBUNTU_SNAPSHOT} \
        curl \
        git

FROM ubuntu-build-deps AS base

ENV HOME=/app
ENV CARGO_HOME=${HOME}/rust/cargo
ENV RUSTUP_HOME=${HOME}/rust/rustup
ENV UV_PYTHON_INSTALL_DIR=${HOME}/python/interpreter
ENV XWIN_CACHE_DIR=${HOME}/xwin-cache

WORKDIR ${HOME}

# Download a macOS SDK
FROM ubuntu-build-deps AS macos-sdk

ARG MACOSX_SDK_SHA

# MacOS SDK
# TODO: Replace with https://github.com/joseluisq/macosx-sdks?
RUN --mount=type=tmpfs,target=/tmp/scratch \
    cd /tmp/scratch \
    && git clone https://github.com/phracker/MacOSX-SDKs \
    && cd MacOSX-SDKs \
    && git checkout -q ${MACOSX_SDK_SHA} \
    && mkdir -p /app \
    && mv MacOSX11.3.sdk /app/macos-sdk \
    && cd .. \
    && rm -rf MacOSX-SDKs


# Bootstrap astral-dev-toolchain, and use it to bootstrap
# `rustup` and `cargo xwin`.
# Work around Dockerfile syntax limitations
# TODO: re-add UV_DOCKER_HASH here
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM base AS bootstrap

COPY --from=uv /uv /uvx /bin

COPY . ${HOME}/astral-dev-toolchain
ENV UV_NO_DEV=1
WORKDIR ${HOME}/astral-dev-toolchain
RUN uv sync --locked
RUN uv run astral-dev-toolchain bootstrap

FROM base

# Install manually allowlisted build dependencies
# llvm is for llvm-mt (symlinked to mt.exe for lld-link manifest embedding)
# TODO: Get rid of `ln -s /usr/bin/llvm-mt /usr/bin/mt.exe`
# Clang is for clang-cl (Windows)
RUN --mount=type=cache,id=astral-ubuntu-apt-lists-v2,target=/var/lib/apt/lists,sharing=locked \
    apt install -y --update --snapshot ${UBUNTU_SNAPSHOT} \
        build-essential \
        ca-certificates \
        curl \
        git \
        llvm \
        clang \
        pkg-config \
    && ln -s /usr/bin/llvm-mt /usr/bin/mt.exe

COPY --chmod=a+rX --from=macos-sdk ${HOME}/macos-sdk ${HOME}/macos-sdk

COPY --chmod=a+rwX --from=bootstrap ${HOME}/rust ${HOME}/rust
COPY --chmod=a+rX --from=uv /uv /uvx ${HOME}/.local/bin/
COPY --chmod=a+rX --from=bootstrap ${HOME}/astral-dev-toolchain ${HOME}/astral-dev-toolchain
COPY --chmod=a+rwX --from=bootstrap ${HOME}/xwin-cache ${HOME}/xwin-cache
# COPY --chmod only affects contents, not the destination dir itself
RUN chmod a+w ${HOME}/xwin-cache

# macOS cross-compilation SDK env vars
ENV SDKROOT=${HOME}/macos-sdk
ENV CFLAGS_x86_64_apple_darwin="-isystem ${SDKROOT}/usr/include -iframework ${SDKROOT}/System/Library/Frameworks"
ENV CFLAGS_aarch64_apple_darwin="-isystem ${SDKROOT}/usr/include -iframework ${SDKROOT}/System/Library/Frameworks"
ENV CARGO_TARGET_X86_64_APPLE_DARWIN_RUSTFLAGS="-C link-arg=-F${SDKROOT}/System/Library/Frameworks -C link-arg=-L${SDKROOT}/usr/lib"
ENV CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS="-C link-arg=-F${SDKROOT}/System/Library/Frameworks -C link-arg=-L${SDKROOT}/usr/lib"

ENV PATH="${HOME}/astral-dev-toolchain/.venv/bin:${HOME}/.local/bin:${CARGO_HOME}/bin:${PATH}"

# Smoke tests
RUN cargo --version \
    uv --version \
    maturin --version \
    python-zig version
