# hyperlane-svm-deployer
# Multi-stage build from upstream Hyperlane @ 16c056a0 (fork tag sealevel-gorbagana-v1.0.0)
# Produces: hyperlane-sealevel-client binary, .so program artifacts, solana-verify
#
# Build context: ~/cerc/hyperlane-monorepo (cloned by laconic-so setup-repositories)
# Invoked via: docker build -f <this-file> ~/cerc/hyperlane-monorepo
# Note: deploy script is ConfigMap-mounted at runtime, not baked into the image.

# ============================================================
# Stage 1: Builder — compile sealevel-client and .so programs
# ============================================================
FROM ubuntu:22.04 AS builder

ARG SOLANA_CLI_VERSION=3.0.14
ARG DEBIAN_FRONTEND=noninteractive

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    git \
    libssl-dev \
    libudev-dev \
    pkg-config \
    protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

# Install Rust toolchain (version pinned in rust/sealevel/rust-toolchain)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain none
ENV PATH="/root/.cargo/bin:${PATH}"

# Set a default Rust toolchain so cargo is available for global installs
RUN rustup default stable

# Install Solana CLI
RUN sh -c "$(curl -sSfL https://release.anza.xyz/v${SOLANA_CLI_VERSION}/install)" && \
    mv /root/.local/share/solana/install/active_release/bin/* /usr/local/bin/

# Install solana-verify for post-deploy hash verification
RUN cargo install solana-verify --locked

# Copy required directories from build context (cloned by laconic-so)
WORKDIR /build/hyperlane-monorepo
COPY rust/ ./rust/
# Install the Rust toolchain specified by rust/sealevel/rust-toolchain
RUN cd rust/sealevel && rustup show

# Run cargo-audit on sealevel dependencies
RUN cargo install cargo-audit --locked && \
    cd rust && cargo audit || true

# Build sealevel-client (the deployment CLI)
RUN cd rust/sealevel \
    && cargo build --release --locked --bin hyperlane-sealevel-client

# Build all .so program artifacts via the monorepo's build script
RUN cd rust/sealevel/programs && ./build-programs.sh

# Pre-install spl-token-cli (hyperlane fork) — the warp-route deploy
# command calls install_spl_token_cli() which expects rustup at runtime.
# Building it here avoids needing rustup/cargo in the runtime image.
RUN rustup toolchain install 1.76.0 && \
    cargo +1.76.0 install spl-token-cli \
      --git https://github.com/hyperlane-xyz/solana-program-library \
      --branch dan/create-token-for-mint \
      --rev e101cca \
      --locked

# ============================================================
# Stage 2: Runtime — minimal image with binaries only
# ============================================================
FROM ubuntu:22.04 AS runtime

ARG SOLANA_CLI_VERSION=3.0.14
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gettext-base \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Install Solana CLI in runtime (needed for solana-keygen, solana commands)
RUN sh -c "$(curl -sSfL https://release.anza.xyz/v${SOLANA_CLI_VERSION}/install)" && \
    mv /root/.local/share/solana/install/active_release/bin/* /usr/local/bin/ && \
    rm -rf /root/.local/share/solana

# Install kubectl for ConfigMap/Secret creation
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl && mv kubectl /usr/local/bin/

# Copy sealevel-client binary
COPY --from=builder /build/hyperlane-monorepo/rust/sealevel/target/release/hyperlane-sealevel-client /usr/local/bin/

# Copy solana-verify
COPY --from=builder /root/.cargo/bin/solana-verify /usr/local/bin/

# Copy pre-built spl-token binary (hyperlane fork, built in builder stage).
# The warp-route deploy command calls install_spl_token_cli() which
# unconditionally runs rustup + cargo install. We provide shims that
# exit 0 so the function succeeds without needing the full Rust toolchain.
# spl-token binary must be at $HOME/.cargo/bin/spl-token — the sealevel
# client hardcodes this path (warp_route.rs:296).
RUN mkdir -p /root/.cargo/bin
COPY --from=builder /root/.cargo/bin/spl-token /root/.cargo/bin/spl-token
# install_spl_token_cli() unconditionally runs rustup + cargo install before
# every warp deploy. Shims that exit 0 let it pass without the full toolchain.
RUN printf '#!/bin/sh\nexit 0\n' > /usr/local/bin/rustup && chmod +x /usr/local/bin/rustup && \
    printf '#!/bin/sh\nexit 0\n' > /usr/local/bin/cargo && chmod +x /usr/local/bin/cargo

# Copy .so program artifacts
COPY --from=builder /build/hyperlane-monorepo/rust/sealevel/target/deploy/*.so /opt/hyperlane/programs/

WORKDIR /opt/hyperlane

CMD ["echo", "No script mounted. Override command to run a deploy script."]
