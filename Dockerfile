# syntax=docker/dockerfile:1

ARG UV_VERSION=0.12.5
ARG UBUNTU_SNAPSHOT=20260127T200000Z
ARG MACOSX_SDK_SHA=041600eda65c6a668f66cb7d56b7d1da3e8bcc93
# Keep this direct-build default aligned with image/toolchain.toml. The host CLI
# passes BOOTSTRAP_IMAGE explicitly on every normal zygote build.
ARG BOOTSTRAP_IMAGE=ghcr.io/astral-sh/astral-dev-toolchain-bootstrap:0.1.0

FROM ubuntu:26.04@sha256:4095ef613201918336b5d7d00be15d8b09c72ddb77c80bca249c255887a64d87 AS ubuntu-build-deps

ARG TARGETARCH
ARG UBUNTU_SNAPSHOT

# ca-certificates are required before using the snapshot.
RUN --mount=type=cache,id=astral-ubuntu-apt-lists-${TARGETARCH}-v3,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=astral-ubuntu-apt-archives-${TARGETARCH}-v1,target=/var/cache/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && DEBIAN_FRONTEND=noninteractive apt install -y --update ca-certificates \
    && apt update --snapshot ${UBUNTU_SNAPSHOT} \
    && DEBIAN_FRONTEND=noninteractive apt install -y --snapshot ${UBUNTU_SNAPSHOT} \
        curl \
        git

FROM ubuntu-build-deps AS base

ENV HOME=/app
ENV CARGO_HOME=${HOME}/rust/cargo
ENV RUSTUP_HOME=${HOME}/rust/rustup
ENV UV_CACHE_DIR=${HOME}/.cache/uv
ENV UV_LINK_MODE=copy
ENV UV_NO_DEV=1
ENV UV_PYTHON_INSTALL_DIR=${HOME}/python/interpreter
ENV XWIN_CACHE_DIR=${HOME}/xwin-cache

WORKDIR ${HOME}

# Download the macOS cross-compilation SDK independently from Python and Rust.
FROM ubuntu-build-deps AS macos-sdk

ARG MACOSX_SDK_SHA

# TODO: Replace with https://github.com/joseluisq/macosx-sdks?
RUN --mount=type=tmpfs,target=/tmp/scratch \
    cd /tmp/scratch \
    && git clone --filter=blob:none --no-checkout https://github.com/phracker/MacOSX-SDKs \
    && cd MacOSX-SDKs \
    && git sparse-checkout set MacOSX11.3.sdk \
    && git checkout -q ${MACOSX_SDK_SHA} \
    && mkdir -p /app \
    && mv MacOSX11.3.sdk /app/macos-sdk

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# Install the pinned Python independently from either Python package.
FROM base AS python

ARG TARGETARCH

COPY --from=uv /uv /uvx /bin/
COPY .python-version ./
RUN --mount=type=cache,id=astral-uv-${TARGETARCH},target=/app/.cache/uv,sharing=locked \
    uv python install

# Install the small, stable bootstrap member before provisioning the expensive
# Rust and xwin state. Neither host source, zygote source, nor tool definitions
# are inputs to this stage.
FROM python AS bootstrap-deps

ARG TARGETARCH
WORKDIR ${HOME}/bootstrap-workspace

COPY .python-version pyproject.toml uv.lock ./
COPY packages/astral-dev-toolchain-bootstrap/pyproject.toml packages/astral-dev-toolchain-bootstrap/pyproject.toml
RUN --mount=type=cache,id=astral-uv-${TARGETARCH},target=/app/.cache/uv,sharing=locked \
    uv sync --frozen \
        --package astral-dev-toolchain-bootstrap \
        --no-install-workspace

COPY --link packages/astral-dev-toolchain-bootstrap packages/astral-dev-toolchain-bootstrap
COPY --link image/toolchain.toml ${HOME}/toolchain-image.toml
RUN --mount=type=cache,id=astral-uv-${TARGETARCH},target=/app/.cache/uv,sharing=locked \
    uv sync --frozen \
        --package astral-dev-toolchain-bootstrap \
        --no-editable

# Provision the toolchain only after the complete bootstrap environment has
# been validated and cached.
FROM bootstrap-deps AS provisioned

ARG TARGETARCH

# The cache mount seeds repeated bootstrap executions, while the copied state is
# committed to the image. The cache is architecture-specific because rustup's
# host toolchain is architecture-specific.
RUN --mount=type=cache,id=astral-bootstrap-${TARGETARCH}-v1,target=/var/cache/astral-bootstrap,sharing=locked \
    mkdir -p \
        /var/cache/astral-bootstrap/rust \
        /var/cache/astral-bootstrap/xwin-cache \
    && CARGO_HOME=/var/cache/astral-bootstrap/rust/cargo \
       RUSTUP_HOME=/var/cache/astral-bootstrap/rust/rustup \
       XWIN_CACHE_DIR=/var/cache/astral-bootstrap/xwin-cache \
       .venv/bin/astral-dev-toolchain-bootstrap --config ${HOME}/toolchain-image.toml \
    && mkdir -p ${HOME}/rust ${HOME}/xwin-cache \
    && cp -a /var/cache/astral-bootstrap/rust/. ${HOME}/rust/ \
    && cp -a /var/cache/astral-bootstrap/xwin-cache/. ${HOME}/xwin-cache/ \
    && chmod -R a+rwX ${HOME}/rust ${HOME}/xwin-cache

# This is the slow-moving image published by the bootstrap-v* workflow.
FROM provisioned AS bootstrap-image

ARG TARGETARCH
ARG UBUNTU_SNAPSHOT

# Install manually allowlisted build dependencies.
# llvm provides llvm-mt, which lld-link expects as mt.exe for manifest embedding.
# Clang provides clang-cl for Windows builds.
RUN --mount=type=cache,id=astral-ubuntu-runtime-apt-lists-${TARGETARCH}-v1,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=astral-ubuntu-apt-archives-${TARGETARCH}-v1,target=/var/cache/apt,sharing=locked \
    apt update --snapshot ${UBUNTU_SNAPSHOT} \
    && DEBIAN_FRONTEND=noninteractive apt install -y --snapshot ${UBUNTU_SNAPSHOT} \
        build-essential \
        llvm \
        clang \
        pkg-config \
    && ln -s /usr/bin/llvm-mt /usr/bin/mt.exe

COPY --link --chmod=a+rX --from=macos-sdk ${HOME}/macos-sdk ${HOME}/macos-sdk

# macOS cross-compilation SDK environment.
ENV SDKROOT=${HOME}/macos-sdk
ENV CFLAGS_x86_64_apple_darwin="-isystem ${SDKROOT}/usr/include -iframework ${SDKROOT}/System/Library/Frameworks"
ENV CFLAGS_aarch64_apple_darwin="-isystem ${SDKROOT}/usr/include -iframework ${SDKROOT}/System/Library/Frameworks"
ENV CARGO_TARGET_X86_64_APPLE_DARWIN_RUSTFLAGS="-C link-arg=-F${SDKROOT}/System/Library/Frameworks -C link-arg=-L${SDKROOT}/usr/lib"
ENV CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS="-C link-arg=-F${SDKROOT}/System/Library/Frameworks -C link-arg=-L${SDKROOT}/usr/lib"

ENV PATH="${CARGO_HOME}/bin:${PATH}"

RUN cargo --version \
    && rustc --version \
    && uv --version \
    && clang --version \
    && command -v llvm-mt

# The default target is the frequently changing local zygote. It starts from
# the separately published bootstrap image rather than from the stage above, so
# normal host CLI invocations never traverse the expensive bootstrap graph.
FROM ${BOOTSTRAP_IMAGE} AS zygote

ARG TARGETARCH

# Install zygote dependencies independently from zygote source. Source changes
# therefore invalidate only the final package installation and smoke test.
WORKDIR ${HOME}/zygote-workspace
COPY .python-version pyproject.toml uv.lock ./
COPY packages/astral-dev-toolchain-zygote/pyproject.toml packages/astral-dev-toolchain-zygote/pyproject.toml
RUN --mount=type=cache,id=astral-uv-${TARGETARCH},target=/app/.cache/uv,sharing=locked \
    uv sync --frozen \
        --package astral-dev-toolchain-zygote \
        --no-install-workspace

# Zygote source is the final application input.
COPY --link packages/astral-dev-toolchain-zygote packages/astral-dev-toolchain-zygote
RUN --mount=type=cache,id=astral-uv-${TARGETARCH},target=/app/.cache/uv,sharing=locked \
    uv sync --frozen \
        --package astral-dev-toolchain-zygote \
        --no-editable

ENV PATH="${HOME}/zygote-workspace/.venv/bin:${CARGO_HOME}/bin:${PATH}"

RUN maturin --version \
    && python-zig version \
    && astral-dev-toolchain-zygote --help

ENTRYPOINT ["astral-dev-toolchain-zygote"]
