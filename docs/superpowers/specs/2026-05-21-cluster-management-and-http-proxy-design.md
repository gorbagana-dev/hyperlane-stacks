# Cluster Management Switch + http-proxy Wiring — Design

**Date:** 2026-05-21
**Author:** Brainstormed with Claude, approved by Rick
**Status:** Approved, ready for implementation plan
**Related:**
- `docs/architecture-decisions.md` ("Kind Cluster Management" section, to be rewritten by this PR)
- `docs/superpowers/specs/2026-05-20-bridge-state-extract-and-distribution-design.md` (this PR is the planned follow-up that section anticipated)
- Woodburn deployer (`/home/dev/git_puller/repos/woodburn_deployer/`) — pattern reference for TLS via mkcert + Caddy cert-backup

## Goal

Hand the kind-cluster + ingress + TLS-termination story off to stack-orchestrator's first-class mechanisms. After this PR:

- SO owns kind cluster lifecycle (`--perform-cluster-management` on every k8s-kind `deploy_start`).
- SO installs Caddy as the ingress controller; Caddy serves TLS autonomously using certs pre-loaded into its `secret_store` at the fake-ACME path.
- Every long-running stack spec is self-sufficient enough to bootstrap on its own host (multi-machine prod principle).
- Tests use the same Caddy + Ingress + TLS-termination path as prod. Only the cert origin differs — mkcert in dev, Let's Encrypt in prod.

## Non-goals

- No `stack-orchestrator` source changes. Everything we need is already in the pinned SO version (`v1.1.0-b3e9366-202605111309`).
- No changes to the bridge-state distribution mechanism (already shipped).
- No re-introduction of MinIO API Ingress — PR2's `external-services:` is the right vehicle.
- No ansible work — PR3 handles multi-machine prod rollout.
- No changes to deployer / warp-deployer Jobs beyond declaring `kind-mount-root` (which the deployer already does).

## Architecture

### Three-layer model

```
┌──────────────────────────────────────────────────────────────────────┐
│ Layer 3: Stack specs (per stack, per environment)                    │
│   network.http-proxy: [ {host-name, routes: [{path, proxy-to}]} ]    │
│   kind-mount-root: <per-host path>                                   │
│   acme-email: <operator email or test sentinel>                      │
└──────────────────────────────────────────────────────────────────────┘
                              │ deploy_start --perform-cluster-management
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Layer 2: Stack-orchestrator (already in place)                       │
│   • Cluster: create_cluster() — single-cluster-per-host, reuse       │
│   • Ingress controller: install_ingress_for_kind()                   │
│     - phase 1: caddy-system namespace + RBAC + ConfigMap + Service   │
│     - phase 2: _restore_caddy_certs(kind_mount_root)                 │
│     - phase 3: start Caddy Deployment (reads pre-loaded certs)       │
│     - phase 4: install caddy-cert-backup CronJob                     │
│   • Per-stack Ingress: emitted from network.http-proxy with          │
│     kubernetes.io/ingress.class: caddy annotation                    │
└──────────────────────────────────────────────────────────────────────┘
                              │ reads from
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Layer 1: Per-host on-disk state                                      │
│   <kind_mount_root>/                                                 │
│     caddy-cert-backup/caddy-secrets.yaml  ← TLS certs                │
│     state/                                ← bridge state files       │
│     warp-deploy-outputs/                  ← warp deploy artifacts    │
│     local-certs/                          ← mkcert cert source (dev) │
│                                                                      │
│   Populated by: pytest fixture (dev) / ansible task (prod)           │
└──────────────────────────────────────────────────────────────────────┘
```

The `kind-mount-root` umbrella is the single per-host directory holding everything stateful — bridge state, warp deploy outputs, and TLS material. The fixture (or ansible) writes the cert backup file before any `deploy_start`; SO's `_restore_caddy_certs` reads it during `install_ingress_for_kind` phase 2; Caddy serves the loaded certs without ever attempting ACME.

### Bootstrap flow (whichever stack is first)

```
deploy_start --perform-cluster-management on stack X
  └─ SO:
       1. create_cluster() — reuses existing or creates new (idempotent)
       2. check_mounts_compatible() — fails fast on kind-mount-root drift
       3. install_ingress_for_kind() if not is_ingress_running()
            → reads <kind_mount_root>/caddy-cert-backup/caddy-secrets.yaml
            → creates secrets in caddy-system namespace
            → starts Caddy (picks up secrets at startup)
            → installs backup CronJob
       4. ensure_namespace(laconic-<stack-name>)
       5. create stack's own k8s resources, including Ingress emitted from
          network.http-proxy:

Subsequent deploy_start --perform-cluster-management on stack Y
  └─ SO:
       1. create_cluster() — reuses existing (no-op)
       2. check_mounts_compatible() — passes (both specs declare same root)
       3. install_ingress_for_kind() — skipped (is_ingress_running() returns true)
       4. ensure_namespace(laconic-<stack-name>) — creates stack Y's namespace
       5. create stack Y's resources, including its own Ingress
```

No stack assumes another stack is first. This works the same way in single-host dev (where one stack happens to start first) and multi-machine prod (where each host bootstraps independently).

## Components

### 1. Stack-orchestrator (no changes)

Locked in:

| Capability | Source | Why we rely on it |
|---|---|---|
| `create_cluster()` reuse semantics | `helpers.py:385-409` | Lets every stack pass `--perform-cluster-management`; first creates, rest no-op |
| `check_mounts_compatible()` | `helpers.py:202+`, called at `deploy_k8s.py:880` | Validates per-host `kind-mount-root` consistency |
| `install_ingress_for_kind()` phase ordering | `helpers.py:464-552` | Cert pre-load happens before Caddy starts |
| `_restore_caddy_certs(kind_mount_root)` | `helpers.py:103-150` | Reads our pre-written `caddy-secrets.yaml` |
| `_install_caddy_cert_backup` CronJob | `helpers.py` (phase 4) | Backs Caddy's live secret store to disk; survives cluster recreation |
| Ingress emission with `kubernetes.io/ingress.class: caddy` | `cluster_info.py:278` | Caddy picks up the Ingress automatically |
| Skipping `tls:` block on Kind | `deploy_k8s.py:916` (`use_tls = http_proxy_info and not is_kind()`) | Correct — Caddy on Kind serves TLS autonomously, no cert-manager TLS Secret needed |

One thing to know but not fix: the `cert-manager.io/cluster-issuer: letsencrypt-prod` annotation gets added to Ingress objects when `not certificates` (`cluster_info.py:280-281`). On Kind it's inert (no cert-manager running). Harmless — Caddy ignores annotations it doesn't act on.

### 2. Stack specs

Every long-running stack spec gets two new top-level fields:

```yaml
kind-mount-root: <per-environment value>
acme-email: <operator-or-test value>
```

Plus, for stacks with externally-reachable HTTP endpoints, an `http-proxy:` block under `network:`. The full stack matrix:

| Stack | Type | `kind-mount-root` | `acme-email` | `http-proxy:` |
|---|---|---|---|---|
| hyperlane-svm-deployer | Job | ✓ already | ✓ new | — |
| hyperlane-svm-warp-deployer | Job | ✓ already | ✓ new | — |
| hyperlane-validator-gorchain | Pod | ✓ new | ✓ new | `validator-gorchain.<domain>` → `validator:9090` |
| hyperlane-validator-solana | Pod | ✓ new | ✓ new | `validator-solana.<domain>` → `validator:9090` |
| hyperlane-relayer | Pod | ✓ new | ✓ new | `relayer.<domain>` → `relayer:9091` |
| hyperlane-minio | Pod | ✓ new | ✓ new | `minio-console.<domain>` → `minio:9001` |
| hyperlane-gas-oracle | Pod | ✓ new | ✓ new | — |
| hyperlane-monitoring | Pod | ✓ new | ✓ new | `grafana.<domain>` → `grafana:3000`, `prometheus.<domain>` → `prometheus:9090` |
| hyperlane-warp-ui | Pod | ✓ new | ✓ new | `bridge.<domain>` → `warp-ui:3000` |

Per-environment values (no placeholders for `kind-mount-root`):

| Field | Test fixture | Prod spec |
|---|---|---|
| `kind-mount-root` | `/tmp/hyperlane-bridge-e2e` (hardcoded) | `/srv/kind/hyperlane-bridge` (hardcoded) |
| `acme-email` | `e2e@example.test` (literal — Caddy never reaches ACME with pre-loaded certs) | `ops@example.com` (commented as placeholder for operator email) |
| hostname suffix | `*.test` | `*.example.com` (existing convention; operator replaces) |

### 3. Test fixtures

Concrete changes in `tests/e2e/`:

**`lib/cluster.py`** — removed: `create_kind_cluster`, `install_cert_manager`, `create_selfsigned_issuer`, `install_ingress_nginx`. Kept: `destroy_kind_cluster`, `ensure_hosts_entry`, `create_namespace`, `get_host_ip`. Added:

```python
KIND_CLUSTER_NAME = "hyperlane"  # matches kind-cluster-name: in every spec
TEST_HOSTNAMES: tuple[str, ...] = (
    "bridge.test",
    "grafana.test",
    "prometheus.test",
    "validator-gorchain.test",
    "validator-solana.test",
    "relayer.test",
    "minio-console.test",
)

def ensure_mkcert_installed() -> None:
    """Idempotently run `mkcert -install`. Skips if mkcert CAROOT already
    contains a CA. Raises if mkcert isn't on PATH (with a pointer to README)."""

def ensure_mkcert_cert(cert_dir: Path, hostnames: list[str]) -> tuple[Path, Path]:
    """Generate (or reuse) a multi-SAN mkcert cert covering hostnames.
    Returns (cert_path, key_path). Idempotent — checks existing cert's SANs
    against the requested list and regenerates if any differ."""

def write_caddy_cert_backup(
    backup_path: Path, cert_path: Path, key_path: Path, hostnames: list[str]
) -> None:
    """Render caddy-secrets.yaml with one k8s Secret per hostname referencing
    the same cert+key. Writes to backup_path."""
```

**`conftest.py`** — removed: `GRAFANA_INGRESS_TEMPLATE`, `PROMETHEUS_INGRESS_TEMPLATE`, `WARP_UI_INGRESS_TEMPLATE`, all `kubectl apply -f -` Ingress blocks (around `:1485-1508` and `:1897-1910`), all `kubectl get certificate` polling loops, the `REPLACE_KIND_MOUNT_ROOT` entry in `SPEC_REPLACEMENTS`, imports for `create_selfsigned_issuer` / `install_cert_manager` / `install_ingress_nginx`. The `kind_cluster` session fixture is renamed `host_prep` and its body becomes:

```python
for h in TEST_HOSTNAMES:
    ensure_hosts_entry(h)
ensure_mkcert_installed()
cert, key = ensure_mkcert_cert(bridge_state_root / "local-certs", list(TEST_HOSTNAMES))
write_caddy_cert_backup(
    bridge_state_root / "caddy-cert-backup" / "caddy-secrets.yaml",
    cert, key, list(TEST_HOSTNAMES),
)
# yield
if not skip_cleanup and not skip_setup:
    destroy_kind_cluster()
```

Monitoring + warp-ui fixtures lose their Ingress-creation + cert-wait blocks and replace them with one readiness probe loop hitting `https://<hostname>/<health-path>` for 200.

**`lib/deploy.py`** — `deploy_start()` adds `--perform-cluster-management` to the subprocess command unconditionally on k8s-kind deployments. SO's idempotency makes this safe; matches what prod ansible will do.

**`fixtures/`** — delete `kind-config.yaml` (SO generates it from `kind-mount-root` + `network.http-proxy`) and `cert-manager-issuer.yaml` (no cert-manager). Every `test-spec-*.yml` for a long-running stack gets the new `kind-mount-root`, `acme-email`, and (where applicable) `network.http-proxy:` block.

### 4. Endpoint-probe test

New file `tests/e2e/test_ingress_endpoints.py` with one test per Ingress URL — hits the URL with `requests` (cert trusted by mkcert install) and asserts 200, plus expected content checks for `/metrics` endpoints. Covers:

- `https://bridge.test/`
- `https://grafana.test/api/health`
- `https://prometheus.test/-/healthy`
- `https://validator-gorchain.test/metrics`
- `https://validator-solana.test/metrics`
- `https://relayer.test/metrics`
- `https://minio-console.test/minio/health/cluster`

This is the safety net for Ingress misconfig — silent drift in `http-proxy:` declarations gets caught here.

### 5. README updates

`tests/e2e/README.md` gets:

- A new "First-time machine setup" section near the top covering `mkcert` install (apt + binary download for Linux, `brew install mkcert nss` for macOS, then `mkcert -install`).
- The existing "TLS in tests" section rewritten to describe the Caddy + mkcert + cert-backup flow.

### 6. CI workflow updates

`.github/workflows/e2e.yml` adds a step before "Run E2E tests":

```yaml
- name: Install mkcert
  run: |
    sudo apt-get install -y libnss3-tools
    curl -L -o /tmp/mkcert \
      https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-linux-amd64
    sudo install /tmp/mkcert /usr/local/bin/mkcert
    mkcert -install
```

### 7. Documentation updates

`docs/architecture-decisions.md`:

- **Rewrite** the "Kind Cluster Management" section (`:562-595`) to describe the new SO-owned model, the three consequences (per-spec self-sufficiency, Caddy autonomous TLS, per-host cert backup), and explicitly state "No cert-manager. No nginx-ingress. No hand-rolled Ingress in test code."
- **Add** a new "Multi-Machine Prod Principle" section immediately after Kind Cluster Management:

  > Every long-running stack spec is self-sufficient enough to bootstrap on its own host. No spec assumes "some other stack ran here first". Production fans stacks out across machines via ansible (PR3); on each host, whichever stack runs first triggers cluster creation + Caddy install via SO's `--perform-cluster-management` path. Concrete applications: every long-running spec declares `kind-mount-root` and `acme-email`; every spec with externally-reachable HTTP declares `network.http-proxy:`; cross-stack artifacts (state files, certs, backups) live on disk under `kind-mount-root`, populated by fixture/ansible, never assumed to come from a peer pod.

- **Append** to the "Artifact Passing" section's bullet list:

  > The `kind-mount-root` umbrella mount also hosts Caddy's cert backup (`<kind_mount_root>/caddy-cert-backup/`), making it the single per-host directory for everything stateful — bridge state, warp deploy outputs, and TLS material.

## Data flow

### Dev session

1. **`bridge_state_root` fixture** — `mkdir -p /tmp/hyperlane-bridge-e2e` (existing).
2. **`host_prep` fixture** (renamed from `kind_cluster`):
   - `ensure_hosts_entry()` for each of the 7 hostnames.
   - `ensure_mkcert_installed()`.
   - `ensure_mkcert_cert(<state-root>/local-certs, TEST_HOSTNAMES)` → writes `hyperlane.test.crt` + `hyperlane.test.key`.
   - `write_caddy_cert_backup(<state-root>/caddy-cert-backup/caddy-secrets.yaml, cert, key, TEST_HOSTNAMES)` → renders one k8s Secret per hostname at the fake-ACME path.
   - `get_host_ip()` → populates `SPEC_REPLACEMENTS["REPLACE_HOST_IP"]` (existing).
3. **`chain_nodes` fixture** — starts gorchain + solana on host (existing, unchanged).
4. **`deployer_deployment` fixture** — `deploy_start --perform-cluster-management` triggers SO's first-deploy path: cluster creation, Caddy install, cert pre-load, deployer Job.
5. **All subsequent fixtures** — `deploy_start --perform-cluster-management` reuses existing cluster, skips Caddy install (already running), creates its own namespace + Ingresses.
6. **Probe tests** — `requests.get(f"https://{hostname}/<health-path>")` succeeds with mkcert-trusted cert.
7. **Teardown** — `destroy_kind_cluster()` removes the cluster; `force_rmtree(BRIDGE_STATE_ROOT)` wipes state, certs, backups.

### Prod session (informational — PR3)

1. Ansible renders the spec with the per-host `kind-mount-root` value, real `acme-email`, real hostnames.
2. Ansible writes `<kind_mount_root>/caddy-cert-backup/caddy-secrets.yaml` if pre-loading certs (e.g. from operator-provisioned mkcert / acme.sh / etc.), or skips this step if relying on Caddy ACME at startup.
3. Ansible runs first-stack `deploy_start --perform-cluster-management` — that stack triggers cluster creation + Caddy install on this host.
4. Ansible runs subsequent stacks with `--perform-cluster-management` (no-op at cluster level).

## Error handling

- **Missing mkcert binary**: `ensure_mkcert_installed()` fails with a pointer to the README install section.
- **Pre-existing kind cluster with wrong mount config**: SO's `check_mounts_compatible()` fails fast at `deploy_start` with the conflicting paths printed. User runs `kind delete cluster --name hyperlane` and retries.
- **Caddy cert backup malformed**: SO's `_restore_caddy_certs` raises; cluster comes up without certs; subsequent probe tests fail at the TLS handshake with a clear error.
- **`http-proxy:` host name not in `/etc/hosts`**: probe test fails with DNS-resolution error pointing at the hostname; `ensure_hosts_entry()` covers all known test hostnames so this only fires if `TEST_HOSTNAMES` and `http-proxy:` entries drift apart (caught at probe-test time).
- **SO version drift**: pinned in `.github/workflows/e2e.yml` + `tests/e2e/README.md`. CI bumps require both updates.

## Testing

| Test | What it covers |
|---|---|
| Existing functional tests (grafana dashboard, prometheus query, warp-ui Playwright) | Re-routed transparently through SO's `http-proxy:` Ingresses + Caddy. No assertion changes needed — URLs stay `https://*.test`. |
| New `test_ingress_endpoints.py` (7 sub-tests) | Each new + existing Ingress gets a probe test. Catches silent `http-proxy:` drift and Caddy misconfig. |
| `force_rmtree` cleanup (existing) | Validates `--skip-cleanup` reuse works across runs and full teardown removes everything. |

CI runs the full test suite end-to-end with the new mkcert install step.

## Out of scope (explicit non-goals — repeated for cross-reference)

- SO source changes.
- PR2 work: MinIO `external-services:`, per-validator MinIO users, MinIO S3 API Ingress.
- PR3 work: ansible playbooks, multi-machine inventory, two-user privilege model.
- Per-instance namespace overrides on prod validator specs (small follow-up).
- Doc cleanup of `specs/stack-specifications.md` (separate follow-up).

## Open questions

None remaining after brainstorming. All design decisions have been resolved through the clarifying-question phase.

## Implementation order (preview, full plan in writing-plans)

1. Add `kind-mount-root` + `acme-email` to every test fixture spec.
2. Add `http-proxy:` blocks to validator, relayer, minio, monitoring test fixtures (some already in prod specs).
3. Mirror to prod specs.
4. Add mkcert helpers to `lib/cluster.py`; add `TEST_HOSTNAMES`; add `KIND_CLUSTER_NAME`.
5. Rename `kind_cluster` fixture → `host_prep`; rewrite its body.
6. Add `--perform-cluster-management` to `deploy_start()`.
7. Remove cert-manager / nginx / hand-rolled Ingress code from conftest + cluster.py.
8. Delete `fixtures/kind-config.yaml` + `fixtures/cert-manager-issuer.yaml`.
9. Add `test_ingress_endpoints.py`.
10. Update CI workflow (mkcert install step).
11. Update `README.md`.
12. Update `architecture-decisions.md` (rewrite + new section + appended bullet).
