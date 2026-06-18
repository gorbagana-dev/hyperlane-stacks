# Hyperlane SVM Bridge: Supply Chain Security

Covers build-time supply chain risks and mitigations for all bridge components.

---

## Threat Model

| Component | Threat | Impact | Likelihood |
|-----------|--------|--------|-----------|
| **Upstream agent image** (`gcr.io/abacus-labs-dev/hyperlane-agent`) | Compromised upstream CI injects malicious validator/relayer binary | CRITICAL — forge signatures, steal funds | Low (Hyperlane team maintains) |
| **Sealevel `.so` programs** (self-built) | Compromised build environment or dependency inserts backdoor into on-chain programs | CRITICAL — programs control all bridge funds | Low-Medium |
| **`hyperlane-sealevel-client`** (self-built) | Compromised CLI deploys backdoored programs or exfiltrates deployer key during deployment | CRITICAL — has access to deployer key at deploy time | Low-Medium |
| **Rust crates (cargo dependencies)** | Malicious crate via typosquat, compromised maintainer, or supply chain attack | HIGH — `build.rs` executes at compile time with full system access | Medium |
| **Docker base image** (Ubuntu 22.04) | Compromised base image | MEDIUM — affects all containers | Low |
| **Warp UI npm dependencies** | Malicious npm package injects code into browser bundle | MEDIUM — could steal user wallet keys or redirect transactions | Medium |

---

## Mitigations

### Image Pinning

**Decision:** Pin all images by digest, not just tag.

```yaml
# Good — pinned by digest
image: gcr.io/abacus-labs-dev/hyperlane-agent@sha256:<digest>

# Bad — tag can be overwritten
image: gcr.io/abacus-labs-dev/hyperlane-agent:agents-v2.0.0
```

- Upstream agent image: pin by digest in docker-compose / k8s manifests
- Ubuntu base image: pin by digest in Dockerfiles
- Record the digest-to-tag mapping in a version manifest file

### Cargo.lock + `--locked` Builds

**Decision:** All Rust builds use `--locked` flag.

- The `Cargo.lock` file from the gorbagana `hyperlane-monorepo` fork at `v2.2.0-gorbagana.1` is committed and used as-is
- `cargo build --release --locked` ensures no dependency resolution drift
- If `Cargo.lock` doesn't match `Cargo.toml`, the build fails rather than silently resolving different versions

### Dependency Auditing

**Decision:** Run `cargo-audit` as part of the Docker build.

```dockerfile
RUN cargo install cargo-audit && cargo audit
```

- Checks all transitive Rust dependencies against the RustSec Advisory Database
- Build fails if known vulnerabilities are found
- Advisory database is fetched at build time (requires network access during build)

For npm (Warp UI):
```dockerfile
RUN pnpm audit --audit-level=high
```

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
| Rebuild upstream agent image from source | Skipped | Trust Hyperlane team's CI. Image pinned by digest mitigates tag overwrite risk. |
| `cargo-vet` (dependency review) | Deferred | High maintenance overhead for v1; `cargo-audit` catches known CVEs |
| SBOM generation | Deferred | Low priority for v1 |
| Binary signing (GPG/sigstore) | Deferred | No distribution to third parties in v1 |

---

## Version Manifest

Maintain a `versions.json` file in the repo that records all pinned versions and digests:

```json
{
  "hyperlane_deployer_source": "gorbagana-dev/hyperlane-monorepo@v2.2.0-gorbagana.1",
  "hyperlane_agent_tag": "agents-v2.0.0",
  "agent_image": "gcr.io/abacus-labs-dev/hyperlane-agent@sha256:<digest>",
  "ubuntu_base": "ubuntu:22.04@sha256:<digest>",
  "solana_cli": "3.0.14",
  "rust_version": "<from monorepo rust-toolchain.toml>",
  "warp_ui_repo": "hyperlane-xyz/hyperlane-warp-ui-template",
  "warp_ui_commit": "6227c04350c27c208c5512ef40776f8181ab022a"
}
```

This file serves as the audit trail for what was built and deployed.
