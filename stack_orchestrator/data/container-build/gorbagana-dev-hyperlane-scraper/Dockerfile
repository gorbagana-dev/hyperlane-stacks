# syntax=docker/dockerfile:1.4
# gorbagana-dev-hyperlane-scraper
# Hyperlane scraper + sea-orm init-db migrator, built from the gorbagana
# monorepo fork (pinned in the stack's stack.yml) whose indexing tolerates
# pruned slots. No KMS/S3 patches (the scraper signs nothing, writes no S3).
#
# Build context: ~/cerc/hyperlane-monorepo (cloned by laconic-so setup-repositories)
# Invoked via: docker build -f <this-file> ~/cerc/hyperlane-monorepo
# Note: build.sh copies entrypoint.sh into the monorepo context before building.

# ============================================================
# Stage 1: Builder — compile scraper + init-db
# ============================================================
FROM rust:1.88.0 AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends clang protobuf-compiler && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    cargo install --locked sccache

ENV RUSTC_WRAPPER=sccache
ENV SCCACHE_DIR=/sccache

WORKDIR /usr/src/rust/main

# Copy git metadata for vergen build-time info
COPY .git ../../.git

# Copy workspace crates (rust/main) + sealevel
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

# Build the scraper and the init-db migrator. The migration crate is a workspace
# member (a path dependency of the scraper crate), so build it with -p from the
# workspace root; both binaries land in the shared target dir.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=$SCCACHE_DIR,sharing=locked \
    RUSTFLAGS="--cfg tokio_unstable" cargo build --release --bin scraper && \
    RUSTFLAGS="--cfg tokio_unstable" cargo build --release -p migration --bin init-db && \
    mkdir -p /release && \
    cp target/release/scraper /release && \
    cp target/release/init-db /release

# ============================================================
# Stage 2: Runtime — minimal image with binaries + db client
# ============================================================
FROM ubuntu:22.04
WORKDIR /app

# postgresql-client (psql) + jq for the idempotent domain-row seed; openssl/ca
# for TLS RPC; tini for signal handling.
RUN apt-get update && \
    apt-get install -y --no-install-recommends openssl ca-certificates tini \
        libcurl4 postgresql-client jq && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    chmod 777 /app && \
    mkdir -p /home/hyperlane && chown -R 1000:1000 /home/hyperlane

# The scraper merges ./config/*.json with CONFIG_FILES; it requires a ./config
# dir to exist in its CWD. Ship the monorepo's config dir like the agent image.
COPY rust/main/config /app/config

COPY --from=builder /release/* /usr/local/bin/
COPY stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-scraper/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV HOME=/home/hyperlane
USER 1000
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
