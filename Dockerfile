ARG UV_VERSION=0.12.5
ARG UBUNTU_SNAPSHOT=20260127T200000Z
ARG MACOSX_SDK_SHA=041600eda65c6a668f66cb7d56b7d1da3e8bcc93

FROM ubuntu:26.04@sha256:4095ef613201918336b5d7d00be15d8b09c72ddb77c80bca249c255887a64d87 AS ubuntu-build-deps

ARG UBUNTU_SNAPSHOT

# ca-certificates are required before using the snapshot.
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

# Download the macOS cross-compilation SDK.
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

# Install astral-dev-toolchain and use it to bootstrap Rust and xwin.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM base AS bootstrap

COPY --from=uv /uv /uvx /bin

ENV UV_NO_DEV=1
ENV UV_CACHE_DIR=${HOME}/.cache/uv
ENV UV_LINK_MODE=copy
WORKDIR ${HOME}/astral-dev-toolchain

# Install third-party Python dependencies separately so source changes only
# reinstall the project itself before rerunning the toolchain bootstrap.
COPY .python-version pyproject.toml uv.lock ./
RUN --mount=type=cache,id=astral-uv,target=/app/.cache/uv \
    uv sync --locked --no-install-project

# Install the complete project, then bootstrap through its public CLI.
COPY README.md ./
COPY src src
COPY toolchain toolchain
RUN --mount=type=cache,id=astral-uv,target=/app/.cache/uv \
    uv sync --locked
RUN uv run astral-dev-toolchain bootstrap

FROM base

# Install manually allowlisted build dependencies
# llvm is for llvm-mt (symlinked to mt.exe for lld-link manifest embedding)
# TODO: Get rid of `ln -s /usr/bin/llvm-mt /usr/bin/mt.exe`
# Clang is for clang-cl (Windows)
RUN --mount=type=cache,id=astral-ubuntu-apt-lists-v2,target=/var/lib/apt/lists,sharing=locked \
    apt install -y --update --snapshot ${UBUNTU_SNAPSHOT} \
        build-essential \
        llvm \
        clang \
        pkg-config \
    && ln -s /usr/bin/llvm-mt /usr/bin/mt.exe

COPY --chmod=a+rX --from=macos-sdk ${HOME}/macos-sdk ${HOME}/macos-sdk

COPY --chmod=a+rwX --from=bootstrap ${HOME}/rust ${HOME}/rust
COPY --chmod=a+rX --from=uv /uv /uvx ${HOME}/.local/bin/
COPY --chmod=a+rX --from=bootstrap ${HOME}/python ${HOME}/python
COPY --chmod=a+rX --from=bootstrap ${HOME}/astral-dev-toolchain ${HOME}/astral-dev-toolchain
COPY --chmod=a+rwX --from=bootstrap ${HOME}/xwin-cache ${HOME}/xwin-cache

# macOS cross-compilation SDK env vars
ENV SDKROOT=${HOME}/macos-sdk
ENV CFLAGS_x86_64_apple_darwin="-isystem ${SDKROOT}/usr/include -iframework ${SDKROOT}/System/Library/Frameworks"
ENV CFLAGS_aarch64_apple_darwin="-isystem ${SDKROOT}/usr/include -iframework ${SDKROOT}/System/Library/Frameworks"
ENV CARGO_TARGET_X86_64_APPLE_DARWIN_RUSTFLAGS="-C link-arg=-F${SDKROOT}/System/Library/Frameworks -C link-arg=-L${SDKROOT}/usr/lib"
ENV CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS="-C link-arg=-F${SDKROOT}/System/Library/Frameworks -C link-arg=-L${SDKROOT}/usr/lib"

ENV PATH="${HOME}/astral-dev-toolchain/.venv/bin:${HOME}/.local/bin:${CARGO_HOME}/bin:${PATH}"

# Smoke tests
RUN chmod a+w ${HOME}/xwin-cache \
    && cargo --version \
    && uv --version \
    && maturin --version \
    && python-zig version
