# Hyperlane SVM Bridge: Supply Chain Security

Covers build-time supply chain risks and mitigations for all bridge components.

---

## Threat Model

| Component | Threat | Impact | Likelihood |
|-----------|--------|--------|-----------|
| **Agent image** (self-built from `hyperlane-monorepo`, patched validator/relayer) | Compromised build environment or monorepo source inserts malicious validator/relayer binary | CRITICAL — forge signatures, steal funds | Low-Medium |
| **Sealevel `.so` programs** (self-built) | Compromised build environment or dependency inserts backdoor into on-chain programs | CRITICAL — programs control all bridge funds | Low-Medium |
| **`hyperlane-sealevel-client`** (self-built) | Compromised CLI deploys backdoored programs or exfiltrates deployer key during deployment | CRITICAL — has access to deployer key at deploy time | Low-Medium |
| **Rust crates (cargo dependencies)** | Malicious crate via typosquat, compromised maintainer, or supply chain attack | HIGH — `build.rs` executes at compile time with full system access | Medium |
| **Docker base image** (Ubuntu 22.04) | Compromised base image | MEDIUM — affects all containers | Low |
| **Warp UI npm dependencies** | Malicious npm package injects code into browser bundle | MEDIUM — could steal user wallet keys or redirect transactions | Medium |

---

## Mitigations

### Image Pinning

**Decision:** Pin third-party images by version tag. Self-built images are
tagged `:latest`, and pinned by digest at deploy time via `image-overrides:`
(see `versions.json`).

Third-party images pulled from external registries are pinned to specific
version tags in compose files to prevent silent breakage:

```yaml
# Third-party — pinned to version tag
image: prom/prometheus:v3.11.3
image: grafana/grafana:13.0.1

# Self-built — :latest by default; pin by digest via image-overrides for reproducible deploys
image: ghcr.io/gorbagana-dev/hyperlane-agent:latest
```

Pinned third-party images:
- `prom/prometheus:v3.11.3` — monitoring compose
- `prom/pushgateway:v1.11.2` — monitoring compose
- `grafana/grafana:13.0.1` — monitoring compose
- `python:3.12.13-alpine` — monitoring compose (balance-monitor sidecar)
- `minio/minio:RELEASE.2025-09-07T16-13-09Z` — minio compose
- `minio/mc:RELEASE.2025-08-13T08-35-41Z` — minio stack `deploy/commands.py`

Self-built images (`:latest`, pinned at deploy time via `image-overrides:`):
- `ghcr.io/gorbagana-dev/hyperlane-agent`
- `ghcr.io/gorbagana-dev/hyperlane-svm-deployer`
- `ghcr.io/gorbagana-dev/hyperlane-kms-proxy`
- `ghcr.io/gorbagana-dev/hyperlane-warp-ui`
- `ghcr.io/gorbagana-dev/hyperlane-gas-oracle`

### Cargo.lock + `--locked` Builds

**Decision:** Builds use the monorepo's committed `Cargo.lock`; the deployer build additionally passes `--locked`.

- The `Cargo.lock` from the hyperlane-monorepo at commit `16c056a` (`@hyperlane-xyz/core@10.2.0`) is used as-is
- The deployer build runs `cargo build --release --locked --bin hyperlane-sealevel-client` — `--locked` fails the build if `Cargo.lock` doesn't match `Cargo.toml`, preventing dependency resolution drift
- The agent build (validator/relayer) compiles against the same committed `Cargo.lock` but does not pass `--locked`

### Dependency Auditing

**Decision:** Run `cargo-audit` as part of the deployer Docker build.

```dockerfile
RUN cargo install cargo-audit --locked && cd rust && cargo audit || true
```

- Checks all transitive Rust dependencies against the RustSec Advisory Database
- The `|| true` means findings are logged but do not fail the build
- Advisory database is fetched at build time (requires network access during build)

### Post-Deploy Program Hash Verification

**Decision:** Deployer job verifies deployed programs match build output.

After deploying each `.so` program, the deployer compares the on-chain program hash against the locally built binary:

```bash
# Get hash of local binary
solana-verify get-executable-hash target/deploy/hyperlane_sealevel_mailbox.so

# Get hash of deployed program
solana-verify get-program-hash -u $RPC_URL $MAILBOX_PROGRAM_ID

# Compare — fail if mismatch
```

This is included as a verification step in the deployer job. If any hash mismatch is detected, the deployer logs an error and exits non-zero. This catches:
- Corrupted uploads during deployment
- Man-in-the-middle attacks on the RPC connection
- Unexpected program modifications

**Requires:** `solana-verify` CLI installed in the deployer image.

### Build Environment Isolation

**Decision:** Docker multi-stage builds provide isolation.

- Builder stage compiles Rust code — procedural macros and `build.rs` scripts execute here
- Runtime stage copies only the final binaries — no build tools, source code, or compiler in the runtime image
- Build context is ephemeral — no persistent compromise from malicious `build.rs`

---

## What We Are NOT Doing (Accepted Risks)

| Mitigation | Status | Rationale |
|-----------|--------|-----------|
| Reproducible builds via `solana-verify build` (deterministic Docker) | Deferred | Adds build complexity; post-deploy hash check provides sufficient assurance for v1 |
| On-chain verification PDA (OtterSec) | Deferred | Relevant for public-facing programs; our deployment is controlled scope |
| `cargo-vet` (dependency review) | Deferred | High maintenance overhead for v1; `cargo-audit` catches known CVEs |
| SBOM generation | Deferred | Low priority for v1 |
| Binary signing (GPG/sigstore) | Deferred | No distribution to third parties in v1 |

---

## CI Tool Pinning

Tools installed during CI runs are pinned to specific versions to prevent
drift between runs:

| Tool | Version | Where |
|------|---------|-------|
| kubectl | v1.35.0 | Dockerfile (deployer), `.github/workflows/e2e.yml` |
| Solana CLI | 3.0.14 | Dockerfile ARG, `.github/workflows/e2e.yml` |
| kind | v0.31.0 | `.github/workflows/e2e.yml` |
| mkcert | v1.4.4 | `.github/workflows/e2e.yml` |
| laconic-so | `v1.1.0-b3e9366-202605111309` | Both CI workflows (env `LACONIC_SO_VERSION`) |
| Rust | Pinned via `rust-toolchain.toml` in monorepo | Dockerfile (rustup installs toolchain from file) |

---

## Version Manifest

[versions.json](../versions.json) at the repo root records every pinned
version in one place — self-built image digests, third-party images, base
images, sources, CI tools. It is the audit trail; update it alongside the
source file when bumping a pin.
