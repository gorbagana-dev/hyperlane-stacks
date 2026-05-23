# Hyperlane Stacks Status

_As of 2026-05-23_

## What Is This Repo

`hyperlane-stacks` packages the Hyperlane cross-chain token bridge between
Gorchain (custom SVM) and Solana as a set of `laconic-so` stacks deployable
to a single `k8s-kind` cluster.

The repo contains everything needed to stand up the bridge end-to-end:
stack definitions and compose files for the eight components, ConfigMap
source content (scripts, templates, static configs), production
deployment specs, Ansible ops playbooks, two source-controlled services
(`hyperlane-kms-proxy` Go sidecar, `hyperlane-gas-oracle` Node.js service),
and a Python + pytest E2E suite that exercises real cross-chain transfers
on a local Kind cluster.

The companion `stack-orchestrator` repo (`../stack-orchestrator/`) is the
runtime engine — `laconic-so` — that interprets the stack definitions and
manages the Kind cluster.

---

## The 8 Stacks

Deployment order is significant: the two Jobs must succeed before any of
the long-running Pods can start.

| # | Stack | Type | Purpose |
|---|-------|------|---------|
| 1 | hyperlane-minio | Pod | S3-compatible storage for validator checkpoint signatures |
| 2 | hyperlane-svm-deployer | Job | Deploys Hyperlane core programs (mailbox, IGP, ISM, validator-announce) on both chains; writes program IDs + agent-config as state files |
| 3 | hyperlane-svm-warp-deployer | Job | Deploys warp route (token bridge) contracts on top of core; writes warp addresses |
| 4 | hyperlane-validator × 2 | Pod | Validator agent + Privy KMS proxy sidecar, one per chain |
| 5 | hyperlane-relayer | Pod | Cross-chain message relayer + IGP fee-claim sidecar |
| 6 | hyperlane-gas-oracle | Pod | Periodically updates IGP gas prices on both chains |
| 7 | hyperlane-monitoring | Pod | Prometheus + Grafana + balance-monitor + pushgateway |
| 8 | hyperlane-warp-ui | Pod | Browser bridge UI |

Each stack has its own k8s namespace (`laconic-hyperlane-{stack}`).
Cross-namespace communication (e.g. relayer → MinIO) goes via FQDN.

---

## Architecture Highlights

### Host-path "state bus" between deployer Jobs and consumer Pods

Deployer Jobs write JSON state files (program IDs, agent-config, warp
addresses, etc.) to a host directory mounted into the Kind cluster via
`extraMounts`. Before each consumer stack's `deployment start`, a Python
helper (`tests/e2e/lib/state_loader.py:BridgeStateLoader.populate`) on
the host copies the relevant subset of state files into the consumer's
`{deploy_dir}/configmaps/<name>/`. SO then creates those as native k8s
ConfigMaps in the consumer's own namespace.

The hardcoded mapping of which consumer reads which state file lives in
`CONSUMER_STATE_FILES` at the top of `lib/state_loader.py`. Some
consumers (gas-oracle, warp-ui, monitoring) instead receive state values
through env-var injection during spec patching.

### SO manages the Kind cluster

`laconic-so deployment start --perform-cluster-management` is what
creates the Kind cluster (idempotent — reuses an existing one). The
e2e harness stages `/etc/hosts` entries, an mkcert TLS cert, and a
Caddy cert-backup file *before* the first `deploy start`, so those
artifacts are ready when SO mounts them into the cluster.

### Caddy ingress with pre-seeded TLS

Ingress is handled by Caddy. mkcert generates a multi-SAN cert covering
all test hostnames; the cert is wrapped into k8s Secret manifests in
CertMagic's expected layout and dropped into the host-path mount. SO's
`_restore_caddy_certs` reads that backup at startup (via an alpine
container, since the file is root-owned) and pre-seeds the secrets so
Caddy skips ACME entirely. This is the same code path production uses,
just fed mkcert certs in test instead of Let's Encrypt-issued ones.

---

## E2E Test Suite

Under `tests/e2e/`. Python + pytest. Brings up the full bridge on
a fresh Kind cluster and exercises real cross-chain transfers.

| File | What it covers |
|------|----------------|
| `test_00_cluster_helpers.py` | Unit tests for cert-backup helpers |
| `test_01_deployer.py` | Core deployer Job — verifies state files written |
| `test_02_warp_deployer.py` | Warp deployer Job — verifies warp addresses written |
| `test_03_minio.py` | MinIO pod health, bucket creation, API |
| `test_04_validator.py` | Validators (Gorchain + Solana) — health, KMS proxy, metrics, checkpoint writes to MinIO |
| `test_05_relayer.py` | Relayer pod health + metrics |
| `test_06_gas_oracle.py` | Gas oracle — waits for first price update, verifies on-chain IGP config |
| `test_07_monitoring.py` | Prometheus / Grafana / balance-monitor |
| `test_08_bridge.py` | Cross-chain warp route transfers (the real bridge test) |
| `test_09_fee_claim.py` | IGP fee claims on both chains |
| `test_10_warp_ui.py` | Warp UI HTTP smoke (HTML, sentinel replacement, chain config) |
| `test_11_warp_ui_bridge.py` | Warp UI Playwright browser test — real bridge through the UI |

Tests are ordered and run sequentially; later tests depend on state from
earlier ones. `--skip-cleanup` and per-stack `--skip-*-deploy` flags
allow iterative reruns against a kept cluster.

---

## Documentation Map

| Path | Contents |
|------|----------|
| `README.md` | High-level overview, deployment order, repo layout |
| `CLAUDE.md` | Conventions, "keep in sync" rules, SO behavior notes |
| `specs/stack-specifications.md` | Detailed per-stack specs |
| `specs/e2e-test-spec.md` | E2E test plan and infrastructure |
| `specs/ansible-spec.md` | Ops job playbook design |
| `docs/architecture-decisions.md` | Design rationale and build strategy |
| `docs/ops-decisions.md` | Operational design decisions |
| `docs/production-readiness-gaps.md` | Production-vs-current gap analysis (security, gas economics, observability, etc.) |
| `docs/supply-chain-security.md` | Container image + dependency provenance |
| `tests/e2e/README.md` | E2E test setup + run instructions |

---

## Current Status

- **Eight stacks defined, building, and deploying** to a local Kind
  cluster via the E2E harness.
- **End-to-end bridge transfers work** in both directions
  (Gorchain ↔ Solana) under the test setup.
- **Production deployment** via the Ansible playbooks under
  `deployment/ops/` is structured but not yet a routine operation.
- **Production readiness** — significant gaps remain in key management,
  multisig sizing, network security, observability, and incident
  response. See `docs/production-readiness-gaps.md` for the full list.

---

## Where to Look First

- New to the repo → `README.md`, then `specs/stack-specifications.md`.
- Running the tests → `tests/e2e/README.md`.
- Understanding why something is built the way it is → `docs/architecture-decisions.md`.
- Production readiness questions → `docs/production-readiness-gaps.md`.
