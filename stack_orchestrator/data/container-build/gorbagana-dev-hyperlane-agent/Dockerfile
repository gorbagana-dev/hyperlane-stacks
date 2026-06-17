# syntax=docker/dockerfile:1.4
# gorbagana-dev-hyperlane-agent
# Patched build of hyperlane agent with two fixes for S3-compatible / custom endpoints:
# 1. KMS endpoint — rusoto_kms ignores AWS_ENDPOINT_URL_KMS; patch uses Region::Custom
# 2. S3 path-style — force path-style addressing when AWS_ENDPOINT_URL is set (MinIO)
#
# Build context: ~/cerc/hyperlane-monorepo (or equivalent)
# Invoked via: docker build -f <this-file> ~/cerc/hyperlane-monorepo

# ============================================================
# Stage 1: Builder — compile validator and relayer binaries
# ============================================================
FROM rust:1.88.0 AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends musl-tools clang && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    rustup target add x86_64-unknown-linux-musl && \
    cargo install --locked sccache

ENV RUSTC_WRAPPER=sccache
ENV SCCACHE_DIR=/sccache

WORKDIR /usr/src/rust/main

# Copy git metadata for vergen build-time info
COPY .git ../../.git

# Copy workspace crates
COPY rust/main/agents ./agents
COPY rust/main/applications ./applications
COPY rust/main/chains ./chains
COPY rust/main/ethers-prometheus ./ethers-prometheus
COPY rust/main/hyperlane-base ./hyperlane-base
COPY rust/main/hyperlane-core ./hyperlane-core
COPY rust/main/hyperlane-metric ./hyperlane-metric
COPY rust/main/hyperlane-test ./hyperlane-test
COPY rust/main/lander ./lander
COPY rust/main/utils ./utils
COPY rust/main/Cargo.toml ./
COPY rust/main/Cargo.lock ./
COPY rust/sealevel ../sealevel

# Build validator and relayer
# (KMS endpoint + S3 path-style support are committed in the gorbagana fork.)
# Note: we clear sccache before building to avoid stale objects from prior builds.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=$SCCACHE_DIR,sharing=locked \
    sccache --stop-server 2>/dev/null; rm -rf $SCCACHE_DIR/* && \
    RUSTFLAGS="--cfg tokio_unstable" \
    cargo build --release --bin validator --bin relayer && \
    mkdir -p /release && \
    cp target/release/validator /release && \
    cp target/release/relayer /release

# ============================================================
# Stage 2: Runtime — minimal image with patched binaries
# ============================================================
FROM ubuntu:22.04
WORKDIR /app
COPY rust/main/config /app/config
COPY rust/main/app-contexts /app/app-contexts
COPY --from=builder /release/* .

RUN apt-get update && \
    apt-get install -y --no-install-recommends openssl ca-certificates tini libcurl4 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    chmod 777 /app && \
    mkdir -p /usr/share/hyperlane && chmod 1000 /usr/share/hyperlane && \
    mkdir -p /data && chown -R 1000 /data && \
    mkdir -p /home/hyperlane && chown -R 1000:1000 /home/hyperlane

ENV HOME=/home/hyperlane
USER 1000
ENTRYPOINT ["tini", "--"]
CMD ["./validator"]
