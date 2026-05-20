# Bridge State Extract and Distribution

**Status:** design approved, awaiting implementation plan
**Date:** 2026-05-20

## Context

Development on hyperlane-stacks paused after the e2e suite turned green; production deployment infrastructure was never built. Resuming the work surfaces two concerns at once:

1. **Stack-orchestrator has moved.** SO's k8s path now enforces per-namespace ownership via a `laconic.com/deployment-dir` annotation on the namespace (`deploy_k8s.py:204-244`). Our existing pattern of putting all 8 stacks into one shared `laconic-hyperlane` namespace will fail loudly on the second `deployment start` — the safety check is doing exactly what it was written to do, and our shared-NS pattern is what it's catching.
2. **Production needs multi-machine deployment.** Hyperlane's threat model wants validators independent of one another and of the relayer/deployer. Even if v1 prod runs everything on one host, the design must not preclude moving validators to separate machines later.

The shared-namespace pattern is a 2024-vintage workaround for SO limitations that have since been fixed: SO now handles ConfigMap idempotency (`deploy_k8s.py:475-486`) and supports user-defined ConfigMap source paths. The original reason for the workaround — that deployer-job outputs couldn't be mounted as ConfigMaps via SO spec — has shifted from "SO limitation" to "questionable architecture": consumers currently bypass SO's ConfigMap mechanism entirely and `kubectl get configmap` at pod startup. That coupling also makes multi-machine deployment hard, because every validator host would need k8s API access to the deployer's cluster.

This design replaces the kubectl-pipe pattern with a disk-based artifact flow: the deployer Job writes its outputs to disk; an operator commits them to git; downstream stacks (and ansible, eventually) consume the committed files. Per-stack k8s namespaces fall out naturally because cross-stack ConfigMap coupling disappears.

## Goals

- Deployer Jobs produce a versioned, reviewable artifact set on disk; no in-cluster ConfigMap publishing.
- Consumer stacks mount ConfigMaps directly via SO's spec-driven mechanism (`configmaps:` block → `{deploy_dir}/configmaps/{name}/`). No more `kubectl get configmap` from pod startup scripts.
- Each stack runs in its own k8s namespace; SO's namespace ownership check is satisfied without modification.
- Validators can run on separate hosts/clusters from the rest of the bridge. The artifact flow does not depend on k8s API connectivity between machines.
- E2e tests pass end-to-end against the new layout, with TLS for MinIO (self-signed via cert-manager) exercising the same code paths prod will use.

## Non-goals

- Ansible roles for prod deployment — a separate PR after this lands.
- Real Let's Encrypt for prod MinIO ingress — Caddy is already integrated; this is config, not design.
- Per-validator MinIO instances (one MinIO per validator host). Documented as a future direction; v1 prod uses one shared MinIO with per-validator users + bucket-prefix policies.
- Multi-bridge tooling. The directory layout supports multiple bridges (`deployment/bridges/<bridge-name>/`) but no automation around it exists yet.

## Architecture overview

```
┌─────────────────────┐                ┌─────────────────────────────────┐
│  hyperlane-svm-     │                │  deployment/bridges/<bridge>/   │
│  deployer (Job)     │──writes────────│    generated/                   │
│                     │                │      agent-config.json          │
└─────────────────────┘                │      program-ids.json           │
           │                           │      gas-oracle-config.json     │
           │ reads program-ids.json    │      multisig-config.json       │
           ▼                           │      registry/                  │
┌─────────────────────┐                │      token-config.json          │
│  hyperlane-svm-     │──writes────────│      warp-deploy-outputs/       │
│  warp-deployer(Job) │                │    operator/                    │
└─────────────────────┘                │      minio-users.yaml           │
                                       └─────────────┬───────────────────┘
                                                     │
                       ┌─────────────────────────────┴───────────────────┐
                       │                                                 │
            ┌──────────▼──────────┐                       ┌──────────────▼──────────────┐
            │  dev (e2e):         │                       │  prod:                      │
            │  bridge_state_loader│                       │  ansible task (PR2+)        │
            │  pytest fixture     │                       │                             │
            └──────────┬──────────┘                       └──────────────┬──────────────┘
                       │                                                 │
                       │ copies state files into                         │
                       │ each consumer's                                 │
                       │ {deploy_dir}/configmaps/                        │
                       │ before `deployment start`                       │
                       ▼                                                 ▼
            ┌─────────────────────────────────────────────────────────────────┐
            │  SO `deployment start` creates k8s ConfigMaps from               │
            │  {deploy_dir}/configmaps/, mounts them as normal volumes in pods │
            └─────────────────────────────────────────────────────────────────┘
```

The deployer Jobs and the consumer stacks no longer communicate through k8s. Communication is through committed files. The "loader" — pytest fixture in dev, ansible task in prod — is the only thing that knows how to map state files into a specific deploy_dir, and it operates entirely outside the cluster.

## State directory layout

State lives under `deployment/bridges/<bridge-name>/` in this repo. The location is movable later (a separate state-files repo, or a deployment-config repo); the choice is intentionally low-commitment.

```
deployment/bridges/<bridge-name>/
├── generated/                      # deployer Job outputs — machine-produced
│   ├── agent-config.json
│   ├── program-ids.json
│   ├── gas-oracle-config.json
│   ├── multisig-config.json
│   ├── token-config.json
│   ├── registry/
│   │   ├── chains.yaml
│   │   └── addresses.yaml
│   └── warp-deploy-outputs/        # only present if warp-deployer produced files
└── operator/                       # operator-supplied identity/policy config
    └── minio-users.yaml            # per-validator MinIO users (PR2)
```

The `generated/` vs `operator/` split makes diff review unambiguous about which kind of change is happening: re-running the deployer touches `generated/`; adjusting policies or adding a validator touches `operator/`.

File naming drops the `hyperlane-` prefix that ConfigMaps currently carry. Stack namespace already conveys "hyperlane"; the prefix added no information.

## Deployer Job extract

Both `hyperlane-svm-deployer` and `hyperlane-svm-warp-deployer` mount the same host-path volume at `/state` inside the container; the container always writes to `/state`. The compose-jobs files declare the volume; the spec / test fixture / ansible provides the host-side path. In dev (Kind), the host path lives under `/tmp/hyperlane-test-state/<run-id>` and is exposed to all Kind nodes via an `extraMounts` entry in the Kind config the test fixture generates.

### Output contract (hardcoded, not discovered)

Each Job has a hardcoded list of expected output files. The script writes them. On exit, the script verifies every expected file is present and non-empty. If any expected file is missing, the script exits non-zero with a clear message naming the missing file(s).

**hyperlane-svm-deployer** writes:
- `agent-config.json`
- `program-ids.json`
- `gas-oracle-config.json`
- `multisig-config.json`
- `registry/chains.yaml`
- `registry/addresses.yaml`

**hyperlane-svm-warp-deployer** writes:
- `token-config.json`
- `warp-deploy-outputs/<file>...` (zero or more; conditional on the deploy producing output files)

Before writing anything, warp-deployer's script does its own preflight on `$STATE_OUTPUT_DIR/program-ids.json` (must exist and be valid JSON containing the chains it expects to wire warp routes against). It reads `program-ids.json` directly from disk instead of `kubectl get configmap hyperlane-program-ids`.

### What goes away

The `kubectl create configmap` calls at the end of each deploy.sh are removed. The RBAC manifest at `hyperlane-svm-warp-deployer/deploy/commands.py::rbac.yaml` is removed (warp-deployer no longer needs k8s API access). The `deploy/commands.py::create()` hook for warp-deployer becomes a no-op.

## Consumer specs

Each consumer stack's `spec.yml` lists the state ConfigMaps it consumes via the existing `configmaps:` block:

```yaml
# spec-validator-gorchain.yml
configmaps:
  agent-config: ./configmaps/agent-config
  # (existing stack-internal configmaps if any)
```

SO reads those at `deployment start` and creates real k8s ConfigMaps in the stack's namespace, populated from files the loader placed in `{deploy_dir}/configmaps/agent-config/`. Pods mount the resulting ConfigMaps as plain volumes (no `kubectl get` from inside).

| Consumer stack | State files needed |
|---|---|
| `hyperlane-svm-warp-deployer` | `program-ids.json` |
| `hyperlane-validator-{chain}` | `agent-config.json` |
| `hyperlane-relayer` | `agent-config.json`, `multisig-config.json` |
| `hyperlane-gas-oracle` | `gas-oracle-config.json`, `program-ids.json` |
| `hyperlane-warp-ui` | `registry/`, `token-config.json` |
| `hyperlane-monitoring` | `multisig-config.json`, `program-ids.json` |
| `hyperlane-minio` | — |

This mapping is encoded in code (loader logic for dev, ansible task vars for prod). Specs list the CMs they expect; loader supplies them.

## The loader

**Contract (intentionally dumb, hardcoded):** for each consumer stack about to be deployed, copy a hardcoded list of state files into `{deploy_dir}/configmaps/{cm-name}/` before running `deployment start`. The mapping above is the source of truth.

Each consumer's loader entry has its own preflight: it knows which state files it expects to find under `deployment/bridges/<bridge-name>/generated/` (or `$STATE_OUTPUT_DIR` in dev). If any are missing, the loader exits with a clear message naming the consumer and the missing file(s).

### Dev (pytest)

A new `tests/e2e/lib/state_loader.py` module exposes a `BridgeStateLoader` class. The `bridge_state_loader` fixture in `conftest.py`:

1. Creates `STATE_OUTPUT_DIR=/tmp/hyperlane-test-state/<run-id>` (host-path mount for deployer Jobs).
2. Yields the loader instance to tests that need to deploy consumers.
3. Per-consumer test fixtures call `loader.populate(consumer_name, deploy_dir)` before `deployment start`.

### Prod (ansible, PR2+)

Equivalent task in each consumer's deploy role: reads from `deployment/bridges/<bridge-name>/generated/` (committed), copies the relevant subset into `{deploy_dir}/configmaps/`, then runs `laconic-so deployment start`.

## Per-stack namespaces

Drop the `namespace: laconic-hyperlane` line from all 8 `deployment/spec-*.yml` files and from all `tests/e2e/fixtures/test-spec-*.yml` files. Each stack falls back to SO's default `laconic-{stack_name}` derivation:

```
laconic-hyperlane-svm-deployer
laconic-hyperlane-svm-warp-deployer
laconic-hyperlane-validator           (default; per-instance override below)
laconic-hyperlane-relayer
laconic-hyperlane-gas-oracle
laconic-hyperlane-minio
laconic-hyperlane-monitoring
laconic-hyperlane-warp-ui
```

For multi-validator deployments on the same chain (N validators contributing to ISM multisig threshold): each validator instance overrides `namespace:` to a unique name in its spec — e.g. `laconic-hyperlane-validator-gorchain-v1`, `-v2`, ... Ansible inventory carries the names. One stack definition, N deployments.

SO's namespace ownership check is satisfied: each stack owns its own namespace. The previously-required SO modification (to allow shared-namespace deployments) is no longer needed.

### Cross-namespace dependencies after the split

The kubectl-from-pod pattern goes away with this change, so the only runtime cross-stack dependency that survives is MinIO.

- **PR1 (this scope):** validators and relayer reach MinIO via cross-namespace FQDN: `AWS_ENDPOINT_URL_S3=http://hyperlane-minio.laconic-hyperlane-minio.svc.cluster.local:9000`. Minimum change to keep tests green.
- **PR2:** replace the FQDN with `external-services:` declarations (design below) + TLS + per-validator users.

The cross-stack ConfigMap reads (validator/relayer reading `agent-config`, etc.) are replaced by ConfigMaps the loader places into each consumer's own namespace. No cross-NS RBAC needed.

The cross-stack Service that `hyperlane-minio/deploy/commands.py` currently creates for the `hyperlane-minio` short-name in the shared namespace is no longer needed after PR2 (external-services entries replace it). In PR1, the consumers switch to FQDN env vars and the cross-stack Service can go away.

## MinIO design (implementation in PR2)

### Topology

Single shared MinIO instance on a designated host. Most natural placement: the relayer host (relayer reads checkpoints far more often than anyone writes, and co-location saves read round-trips). Each validator + the relayer reaches it as a network endpoint.

### Endpoint declaration

Every consumer that needs MinIO declares it as an `external-services:` entry in its spec — same shape in dev and prod, only the resolved target differs:

```yaml
external-services:
  hyperlane-minio:
    host: s3.bridge.example.com          # prod: real DNS + TLS via Caddy/LE
    # or: ip: 10.0.0.5  port: 9000       # alternative if no DNS available
```

In dev, the same external-services declaration is used but resolves to an in-cluster Service backed by the dev MinIO Pod (selector mode). All TLS paths exercised by dev consumers using `https://hyperlane-minio:443` (or similar), backed by a self-signed cert.

### Per-validator MinIO users + bucket-prefix policies

Each validator has its own MinIO user with a policy restricting it to its own bucket prefix. The relayer has a read-only policy across all prefixes. Configuration lives in `deployment/bridges/<bridge-name>/operator/minio-users.yaml` and looks like:

```yaml
bucket: hyperlane-checkpoints
validators:
  - label: validator-gorchain-1
    chain: gorchain
    prefix: gorchain/validator-1
relayer:
  label: relayer
```

The MinIO init Job (extends `hyperlane-minio` stack) reads this file and runs `mc` commands idempotently:

```bash
mc mb local/hyperlane-checkpoints --ignore-existing
# per-validator policy + user + attach
mc admin policy create local <chain>-<n>-policy /tmp/<chain>-<n>.json
mc admin user   add    local validator-<chain>-<n> "$VALIDATOR_<CHAIN>_<N>_SECRET"
mc admin policy attach local <chain>-<n>-policy --user validator-<chain>-<n>
# relayer
mc admin policy create local relayer-readonly /tmp/relayer-readonly.json
mc admin user   add    local relayer "$RELAYER_MINIO_SECRET"
mc admin policy attach local relayer-readonly --user relayer
```

Per-validator policy JSON (template):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": ["arn:aws:s3:::hyperlane-checkpoints/gorchain/validator-1/*"] },
    { "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::hyperlane-checkpoints"],
      "Condition": { "StringLike": { "s3:prefix": ["gorchain/validator-1/*"] } } }
  ]
}
```

### Credential delivery (k8s Secret pattern, not credentials-files)

MinIO credentials reach pods as k8s Secret objects, mounted via SO's `secrets:` mechanism (`cluster_info.py:733-741` → `envFrom: secretRef:`). The existing `hyperlane-validator-secrets` / `hyperlane-relayer-secrets` blocks already include `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`; per-validator deploys populate them with that validator's MinIO credentials.

Operator workflow: source values may live in `~/.credentials/<validator-label>.env` on the operator's host for ergonomics. Ansible reads that file and templates the values into `kubectl create secret generic` before `deployment start`. Credentials never land in `config.env`, never go through SO's `credentials-files:` path.

### Validator agent-config

```yaml
signers:
  gorchain:
    s3_bucket: hyperlane-checkpoints
    s3_folder: gorchain/validator-1     # matches the policy prefix
    s3_region: us-east-1
```

Plus env: `AWS_ENDPOINT_URL_S3=...`, `AWS_CA_BUNDLE=/etc/ssl/certs/minio-ca.pem`, `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` from the Secret.

## Testing — e2e changes

- New `tests/e2e/lib/state_loader.py` + `bridge_state_loader` fixture in `conftest.py`.
- Test fixtures (`tests/e2e/fixtures/test-spec-*.yml`) drop `namespace: REPLACE_NAMESPACE`; each consumer test patches its spec's `external-services:` for the gorchain RPC and (in PR2) MinIO.
- `tests/e2e/test_01_deployer.py` and `test_02_warp_deployer.py` are updated: they no longer call `wait_for_configmap()` on deployer outputs (which are now files on disk). Instead they verify the expected JSON files exist under `STATE_OUTPUT_DIR` and contain valid data. The `wait_for_configmap` helper survives only for any remaining stack-internal ConfigMap waits.
- TLS in dev (PR2): test fixture sets up cert-manager and a self-signed `ClusterIssuer` once per cluster; MinIO issues a `Certificate`; validators/relayer mount the CA cert via a small `minio-ca` ConfigMap and set `AWS_CA_BUNDLE`. Hyperlane agents use Rust's `rustls`/`aws-sdk-rust`, which honors `AWS_CA_BUNDLE`.

## Error handling and preflight

Three preflight gates, all fail-fast with a clear message naming the missing file or stack:

1. **Deployer Job exit:** the script verifies every file in its hardcoded output list is present and non-empty before exiting 0. Missing file → exit non-zero, Job fails, k8s surfaces the failure.
2. **Warp-deployer entry:** reads `$STATE_OUTPUT_DIR/program-ids.json` and validates structure before doing any on-chain work.
3. **Loader (dev fixture / ansible task):** before copying for any consumer, verifies the source files exist. Missing file → exit non-zero before `deployment start`.

## Migration plan

### PR1 — this design's core scope
- Deployer + warp-deployer Jobs extract to disk; remove kubectl-create-configmap calls.
- Consumer compose files use plain ConfigMap mounts (drop kubectl-get-from-pod init blocks).
- Per-stack namespaces (drop `namespace: laconic-hyperlane` from all specs).
- `bridge_state_loader` pytest fixture + `tests/e2e/lib/state_loader.py`.
- Validator + relayer use FQDN env var for MinIO (interim cross-namespace solution).
- All 11 e2e tests green.
- Docs: `architecture-decisions.md` supersedes the "single namespace" decision; the per-validator MinIO future direction is noted.

### PR2 — MinIO migration
- MinIO declared as `external-services:` in all consumers.
- cert-manager + self-signed `ClusterIssuer` for MinIO TLS in dev.
- Per-validator MinIO users + bucket-prefix policies in extended MinIO init Job.
- `operator/minio-users.yaml` and provisioning logic.
- Prod ingress (Caddy + Let's Encrypt) — config only, no design.

### PR3+ — ansible for prod deployment
- `bridge_state_loader` ansible-task equivalent.
- Two-user privilege model (woodburn pattern).
- Per-host deployment roles, multi-machine inventory.

The small `gorchain-dev-rpc` branch already committed (`56dd1d0`) is independent of all three PRs and lands first.

## Alternatives considered

Documented here per user request — none chosen, but kept for future-context.

### Pattern B — central state cluster
One control cluster hosts deployer + relayer + state stack + monitoring + MinIO. Validator clusters fetch `agent-config` from the control cluster over the network at pod startup (HTTPS endpoint exposing the ConfigMap).

Pros: lower commit ceremony — re-deploying the bridge config is automatic.
Cons: runtime cross-cluster dependency; validators can't bootstrap when the control cluster is unreachable; adds an HTTPS config service we'd otherwise not need.

Rejected because: artifact change rate is low, git is a better substrate than HTTP for an artifact that's reviewed and rarely changed; air-gappable validators are a property worth keeping.

### Pattern C — artifacts in MinIO
Deployer Job writes outputs as JSON files into MinIO. Consumers fetch from MinIO at startup. Already have MinIO deployed.

Pros: uniform fetch pattern; no commit step.
Cons: chicken-and-egg — validators need the MinIO endpoint (and CA) before they can fetch their config. Pulls MinIO availability into the bootstrap path. MinIO becomes single-point-of-truth without git's review/audit affordances.

Rejected because: the bootstrap dependency on MinIO is a regression vs. local state files; git provides the change-history we want for free.

### Shared namespace, SO modification
Modify SO to allow opt-in shared-namespace deployments — either an opt-in spec key (`shared-namespace: true`) or a per-stack annotation pattern (`laconic.com/stack.<stack_name>: <dir>`).

Pros: minimal hyperlane-stacks change; preserves the existing pattern.
Cons: the existing pattern is itself the problem — it grew out of SO workarounds we no longer need. Keeping it locks in coupling that prevents multi-machine deployment. Modifying SO to enable an anti-pattern is the wrong direction.

Rejected because: the shared-namespace pattern is a 2024-vintage workaround; the right fix is removing the workaround, not blessing it.

### Per-validator MinIO instances (Pattern 3)
Each validator runs its own MinIO; relayer is configured with multiple checkpoint sources, one per validator. Matches Hyperlane's adversarial-independence model fully.

Pros: highest validator isolation; no shared storage trust.
Cons: more moving parts; relayer config gets more complex; per-validator MinIO HA and storage management.

Deferred. v1 prod uses one shared MinIO with per-validator users + bucket-prefix policies (most of the isolation benefit without the operational cost). Move to per-validator MinIO if the trust model demands it.

## Open items

- Specific format of `minio-users.yaml` (operator-supplied) — finalized during PR2.
- Whether the dev test fixture should set up cert-manager unconditionally (slower, fuller fidelity) or behind a flag (skippable for fast iteration). Default: unconditional in PR2; revisit if setup time is painful.
- Where ansible inventory lives (this repo's `deployment/ansible/`, a separate `hyperlane-bridge-deploy` repo, or `laconic-tech-ops/infra/hyperlane/`). Decided during PR3.
- Where the deployer Job runs in prod. Two reasonable options: (a) compose-mode on the operator's machine — simplest permissions and outputs land directly on the operator's local filesystem for `git add`; (b) k8s-mode in some target cluster with a PVC the operator pulls via `kubectl cp`. (a) is simpler for v1; revisit if the deployer needs to run in a constrained environment.
