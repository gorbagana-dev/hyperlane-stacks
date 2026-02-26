# hyperlane-svm-deployer
# Multi-stage build from hyperlane-monorepo @ agents-v2.0.0
# Produces: hyperlane-sealevel-client binary, .so program artifacts, solana-verify
#
# Build context: ~/cerc/hyperlane-monorepo (cloned by laconic-so setup-repositories)
# Invoked via: docker build -f <this-file> ~/cerc/hyperlane-monorepo
# Note: build.sh copies entrypoint.sh into the build context before building.

# ============================================================
# Stage 1: Builder — compile sealevel-client and .so programs
# ============================================================
FROM ubuntu:22.04 AS builder

ARG SOLANA_CLI_VERSION=1.18.18
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

# Install Rust toolchain (version from monorepo rust-toolchain.toml)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain none
ENV PATH="/root/.cargo/bin:${PATH}"

# Install Solana CLI
RUN sh -c "$(curl -sSfL https://release.anza.xyz/v${SOLANA_CLI_VERSION}/install)" && \
    mv /root/.local/share/solana/install/active_release/bin/* /usr/local/bin/

# Install solana-verify for post-deploy hash verification
RUN cargo install solana-verify --locked

# Copy hyperlane-monorepo from build context (cloned to ~/cerc/ by laconic-so)
WORKDIR /build/hyperlane-monorepo
COPY . .

# Install the Rust toolchain specified by the monorepo
RUN rustup show

# Run cargo-audit on sealevel dependencies
RUN cargo install cargo-audit --locked && \
    cd rust && cargo audit || true

# Build sealevel-client (the deployment CLI)
RUN cd rust/sealevel \
    && cargo build --release --locked --bin hyperlane-sealevel-client

# Build all .so program artifacts via Solana BPF toolchain
RUN cd rust/sealevel \
    && cargo build-sbf --manifest-path programs/mailbox/Cargo.toml \
    && cargo build-sbf --manifest-path programs/validator-announce/Cargo.toml \
    && cargo build-sbf --manifest-path programs/ism/multisig-ism-message-id/Cargo.toml \
    && cargo build-sbf --manifest-path programs/hyperlane-sealevel-igp/Cargo.toml \
    && cargo build-sbf --manifest-path programs/hpl-hook-merkle-tree/Cargo.toml \
    && cargo build-sbf --manifest-path programs/hyperlane-sealevel-token/Cargo.toml \
    && cargo build-sbf --manifest-path programs/hyperlane-sealevel-token-native/Cargo.toml \
    && cargo build-sbf --manifest-path programs/hyperlane-sealevel-token-collateral/Cargo.toml

# ============================================================
# Stage 2: Runtime — minimal image with binaries only
# ============================================================
FROM ubuntu:22.04 AS runtime

ARG SOLANA_CLI_VERSION=1.18.18
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
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

# Copy .so program artifacts
COPY --from=builder /build/hyperlane-monorepo/rust/sealevel/target/deploy/*.so /opt/hyperlane/programs/

# Copy entrypoint (placed into build context by build.sh)
COPY entrypoint.sh /opt/hyperlane/entrypoint.sh
RUN chmod +x /opt/hyperlane/entrypoint.sh

WORKDIR /opt/hyperlane

ENTRYPOINT ["/opt/hyperlane/entrypoint.sh"]
