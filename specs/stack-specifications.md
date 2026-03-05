# Hyperlane SVM Bridge — Stack-Orchestrator Specifications

## How Stack-Orchestrator Works

Stack-orchestrator (`laconic-so`) deploys containerized applications via three backends: `compose` (Docker Compose), `k8s` (external Kubernetes), and `k8s-kind` (local Kind cluster). We target `k8s-kind`.

### Key Concepts

**stack.yml** defines a deployable unit:
- `repos:` — Git repositories cloned by `laconic-so setup-repositories` to `~/cerc/`. Used as build context for container images. Format: `host/org/repo@branch`. The `build.sh` scripts reference cloned source via `~/cerc/{repo-name}/`.
- `containers:` — Image names built by `laconic-so build-containers`. Each name maps to a `container-build/{name-with-slashes-replaced-by-hyphens}/build.sh` script. Also determines which images get loaded into Kind via `kind load docker-image`.
- `pods:` — List of pod names, each mapping to `compose/docker-compose-{pod-name}.yml`. All services in all pods become containers in ONE k8s Deployment (single Pod).
- `jobs:` — List of job names, each mapping to `compose-jobs/docker-compose-{job-name}.yml`. Jobs become k8s Jobs (restartPolicy: Never, backoffLimit: 0) instead of Deployments — appropriate for one-shot containers that should not restart on completion or failure.

**spec.yml** controls deployment configuration:
- `stack:` — Reference to the stack (path or name)
- `deploy-to:` — `k8s-kind`, `k8s`, or `compose`
- `config:` — Environment variables injected into all containers
- `volumes:` — Named volumes → PVCs; path volumes → HostPath mounts
- `configmaps:` — Directory → k8s ConfigMap, mounted as volume
- `network.http-proxy:` — Ingress routing rules
- `network.ports:` — NodePort mappings
- `security:` — privileged, capabilities, unlimited-memlock
- `resources:` — CPU/memory limits
- `registry-credentials:` — Private registry auth

**deploy/commands.py** hooks:
- `init()` — Returns default spec.yml content (called during `deploy init`)
- `create()` — Post-create hook with access to `DeploymentContext` (called during `deploy create`)

### k8s-kind Deployment Flow

1. `laconic-so --stack <name> setup-repositories` — Clones repos listed in stack.yml to `~/cerc/`
2. `laconic-so --stack <name> build-containers` — Builds images listed in stack.yml `containers:` field
3. `laconic-so --stack <name> deploy init --output spec.yml` — Creates spec.yml template
4. `laconic-so deploy create --spec-file spec.yml --deployment-dir <dir>` — Creates deployment directory
5. `laconic-so deployment --dir <dir> start` — Creates Kind cluster, loads images, generates k8s manifests from compose files, deploys

### Critical Constraint

**All services across all pods in a stack become containers in a single k8s Pod.** Services needing localhost communication (e.g., validator + KMS proxy sidecar) MUST be in the same stack. Services communicating over the network SHOULD be separate stacks.

---

## Stack Decomposition

The architecture doc (`docs/architecture-decisions.md`) describes 5 logical stacks, including a single `hyperlane-svm-agents` stack containing all agents, MinIO, and monitoring. However, SO's constraint that **all services in a stack = one k8s Pod** forces splitting the agents stack into separate stacks — services that need independent restart, scaling, or lifecycle must be separate stacks.

## Stack Inventory

| # | Stack | Type | Services | Purpose |
|---|-------|------|----------|---------|
| 1 | `hyperlane-svm-deployer` | One-time | deployer | Deploy core Hyperlane contracts on both chains |
| 2 | `hyperlane-svm-warp-deployer` | One-time | warp-deployer | Deploy warp route contracts (collateral + synthetic) |
| 3 | `hyperlane-validator` | Long-running | validator + kms-proxy | Checkpoint signing via Privy KMS proxy (one deployment per chain) |
| 4 | `hyperlane-relayer` | Long-running | relayer + igp-fee-claim | Message delivery + periodic IGP fee claiming |
| 5 | `hyperlane-minio` | Long-running | minio + minio-init | S3-compatible checkpoint storage |
| 6 | `hyperlane-gas-oracle` | Long-running | gas-oracle | Periodic IGP gas oracle updates via Privy |
| 7 | `hyperlane-monitoring` | Long-running | prometheus + pushgateway + grafana + balance-monitor | Metrics, alerting, dashboards |
| 8 | `hyperlane-warp-ui` | Long-running | warp-ui | Browser-based bridge UI |
| - | `ops/` | On-demand | kubectl Jobs | Kill switch, restore, teardown, ownership verify |

### stack.yml Summary

| # | Stack | repos: | containers: | pods: | jobs: |
|---|-------|--------|-------------|-------|-------|
| 1 | `hyperlane-svm-deployer` | `github.com/hyperlane-xyz/hyperlane-monorepo@16c056a` | `laconic/hyperlane-svm-deployer` | — | `hyperlane-svm-deployer` |
| 2 | `hyperlane-svm-warp-deployer` | `github.com/hyperlane-xyz/hyperlane-monorepo@16c056a` | `laconic/hyperlane-svm-deployer` | — | `hyperlane-svm-warp-deployer` |
| 3 | `hyperlane-validator` | `git.vdb.to/LaconicNetwork/hyperlane-stacks` | `laconic/hyperlane-kms-proxy` | `hyperlane-validator` | — |
| 4 | `hyperlane-relayer` | `github.com/hyperlane-xyz/hyperlane-monorepo@16c056a` | `laconic/hyperlane-svm-deployer` | `hyperlane-relayer` | — |
| 5 | `hyperlane-minio` | *(none)* | *(none)* | `hyperlane-minio` | — |
| 6 | `hyperlane-gas-oracle` | `git.vdb.to/LaconicNetwork/hyperlane-stacks` | `laconic/hyperlane-gas-oracle` | `hyperlane-gas-oracle` | — |
| 7 | `hyperlane-monitoring` | *(none)* | *(none)* | `hyperlane-monitoring` | — |
| 8 | `hyperlane-warp-ui` | `github.com/hyperlane-xyz/hyperlane-warp-ui-template` | `laconic/hyperlane-warp-ui` | `hyperlane-warp-ui` | — |

- Stacks 1 and 2 use `jobs:` (not `pods:`) because deployers are one-shot containers — k8s Jobs (restartPolicy: Never, backoffLimit: 0) prevent CrashLoopBackOff that occurs when Deployments restart completed containers. Their compose files live in `compose-jobs/` instead of `compose/`.
- Stacks 5 and 7 use only upstream images — no repos or containers needed.
- Stacks 1, 2, and 4 share the same repo/container (deployer image) and are independently buildable.
- Stack 7 balance-monitor will use a lightweight image (not the heavy deployer image).
- The deployer image is built from `@hyperlane-xyz/core@10.2.0` (commit `16c056a09af862b3ce9e14bd3b5b8034750af9d0`), not the older `agents-v2.0.0` tag.

---

## Stack 1: hyperlane-svm-deployer

### Purpose
One-time Job that deploys Hyperlane core contracts (Mailbox, IGP, ISM, Validator Announce, Merkle Tree Hook) on both Gorchain (domain 99999) and Solana (domain 99998).

### How It Works
1. Stack uses `jobs:` in stack.yml — runs as a k8s Job (restartPolicy: Never, backoffLimit: 0) instead of a Deployment, preventing CrashLoopBackOff after completion
2. Deploy script (`deploy.sh`) is mounted via ConfigMap volume at `/opt/scripts/` rather than baked into the Docker image — allows script updates without rebuilding the image
3. Deploys programs on both chains via `hyperlane-sealevel-client` (from `@hyperlane-xyz/core@10.2.0`)
4. Verifies on-chain program hashes via `solana-verify`
5. Transfers ownership: program authority → hardware wallet (uses `--skip-new-upgrade-authority-signer-check` flag required by Solana CLI 3.x), IGP → Privy oracle
6. Writes deployment artifacts as k8s ConfigMaps via kubectl (requires RBAC)
7. Discards hot deployer key

### Services
| Service | Image | Notes |
|---------|-------|-------|
| deployer | `laconic/hyperlane-svm-deployer:local` | `restart: "no"`, mounts deploy script + config templates |

### ConfigMaps (input)
- `deployer-scripts-config` — deploy.sh entrypoint
- `deployer-chain-config` — gorchain.json, solana.json chain definitions
- `deployer-gas-oracle-config` — initial gas oracle configs
- `deployer-multisig-config` — per-chain multisig ISM configs
- `deployer-registry-config` — chain registry metadata.yaml

### ConfigMaps (output, created by deployer via kubectl)
- `hyperlane-program-ids` — deployed program addresses per chain
- `hyperlane-agent-config` — agent-config.json for validators/relayer
- `hyperlane-gas-oracle-config` — gas oracle configs
- `hyperlane-multisig-config` — multisig ISM configs

### deploy/commands.py
`create()` applies RBAC (Role + RoleBinding) granting the default ServiceAccount permission to create/update ConfigMaps in the deployment namespace.

### Config (spec.yml)
`GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`, `GORCHAIN_DOMAIN_ID` (99999), `SOLANA_DOMAIN_ID` (99998), `FORCE_REDEPLOY`

### Secrets (injected separately)
`DEPLOYER_KEYPAIR`, `HARDWARE_WALLET_PUBKEY`, `IGP_ORACLE_WALLET_PUBKEY`

---

## Stack 2: hyperlane-svm-warp-deployer

### Purpose
One-time Job that deploys warp route contracts (collateral on one chain, synthetic on the other) for a specific SPL token pair.

### How It Works
1. Same `jobs:` pattern as core deployer — runs as a k8s Job with ConfigMap-mounted deploy script at `/opt/scripts/`
2. Reads `hyperlane-program-ids` ConfigMap (created by Stack 1) for mailbox addresses
3. Deploys collateral + synthetic warp route programs
4. Writes `hyperlane-token-config` ConfigMap with warp route addresses

### Services
| Service | Image | Notes |
|---------|-------|-------|
| warp-deployer | `laconic/hyperlane-svm-deployer:local` | Same deployer image, different entrypoint script |

### Dependencies
- Requires Stack 1 (core deployer) to have run first

### Config (spec.yml)
`WARP_TOKEN_MINT`, `COLLATERAL_CHAIN`, `SYNTHETIC_CHAIN`, RPC URLs, domain IDs, `FORCE_REDEPLOY`

### Secrets (injected separately)
`DEPLOYER_KEYPAIR`, `HARDWARE_WALLET_PUBKEY`

---

## Stack 3: hyperlane-validator

### Purpose
Runs a Hyperlane validator for a single chain. Signs checkpoints using secp256k1/ECDSA via a Privy KMS proxy sidecar. **One deployment is created per chain** — a Gorchain validator deployment and a Solana validator deployment both use this same stack definition with different spec.yml configs.

### How It Works
1. Validator container runs unmodified hyperlane-agent with `type: "aws"` signer config
2. KMS proxy sidecar intercepts AWS KMS API calls on localhost:9999, forwards to Privy
3. Validator writes checkpoints to MinIO (separate stack) via S3 API
4. Both containers share a Pod (required for localhost:9999 communication)

### Compose: `docker-compose-hyperlane-validator.yml`

Single parameterized compose file. All chain-specific values come from env vars (set via spec.yml config per deployment).

| Service | Image | Notes |
|---------|-------|-------|
| validator | `gcr.io/abacus-labs-dev/hyperlane-agent:agents-v2.0.0` | CLI args from env vars, metrics on :9090 |
| kms-proxy | `laconic/hyperlane-kms-proxy:local` | Port 9999, proxies KMS Sign/GetPublicKey/DescribeKey to Privy |

Validator command uses env vars for all chain-specific args:
- `--origin-chain-name ${ORIGIN_CHAIN_NAME}`
- `--checkpointSyncer.bucket ${CHECKPOINT_BUCKET}`
- `--validator.id ${PRIVY_WALLET_ID}`

MinIO endpoint uses the static hostname `hyperlane-minio` — a k8s Service with this name is created by `deploy/commands.py` in stacks that need MinIO access (see Cross-Stack Communication).

### Config (spec.yml)
- `ORIGIN_CHAIN_NAME` — chain name (e.g., `gorchain` or `solanatestnet`), set per deployment
- `CHECKPOINT_BUCKET` — S3 bucket name, set per deployment
- `MINIO_ACCESS_KEY` — MinIO access key

### Secrets (injected separately)
- `PRIVY_APP_ID`, `PRIVY_APP_SECRET`, `PRIVY_WALLET_ID` — wallet ID varies per chain deployment
- `MINIO_SECRET_KEY` — MinIO secret key

### Compose Environment (hardcoded in compose, not in spec)
- `AWS_ENDPOINT_URL_KMS=http://localhost:9999` — routes to sidecar
- `AWS_ENDPOINT_URL_S3=http://hyperlane-minio:9000` — routes to MinIO k8s Service
- `CONFIG_FILES=/config/agent-config.json`

### ConfigMaps
- `agent-config` — agent-config.json (created by deployer, or template)

### Per-Chain Deployment
Two separate spec.yml files deploy this stack for each chain:
- `spec-validator-gorchain.yml` — sets `ORIGIN_CHAIN_NAME=gorchain`, `PRIVY_WALLET_ID=<gorchain-wallet>`, `CHECKPOINT_BUCKET=hyperlane-validator-gorchain`
- `spec-validator-solana.yml` — sets `ORIGIN_CHAIN_NAME=solanatestnet`, `PRIVY_WALLET_ID=<solana-wallet>`, `CHECKPOINT_BUCKET=hyperlane-validator-solana`

Each deployment gets its own PVC for validator data and its own k8s Deployment.

### Cross-Stack Dependencies
- MinIO stack must be running (S3 for checkpoints)
- Deployer stack must have run (agent-config.json)

---

## Stack 4: hyperlane-relayer

### Purpose
Delivers cross-chain messages between Gorchain and Solana. Includes an IGP fee claim sidecar.

### Compose: `docker-compose-hyperlane-relayer.yml`

| Service | Image | Notes |
|---------|-------|-------|
| relayer | `gcr.io/abacus-labs-dev/hyperlane-agent:agents-v2.0.0` | `relayer` subcommand, gas enforcement `none`, metrics on :9091 |
| igp-fee-claim | `laconic/hyperlane-svm-deployer:local` | Runs `claim-fees.sh` from ConfigMap, loops every 6h |

### IGP Fee Claim Sidecar
Script at `stack_orchestrator/data/config/igp-fee-claim-scripts-config/claim-fees.sh`. Mounted as ConfigMap volume. Claims accumulated IGP fees on both chains using the relayer key for tx fees (permissionless operation).

### Config (spec.yml)
- `GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`
- `GORCHAIN_IGP_PROGRAM_ID`, `SOLANA_IGP_PROGRAM_ID` — for igp-fee-claim sidecar
- `MINIO_ACCESS_KEY`

### Secrets (injected separately)
- `RELAYER_KEY` — hex private key for signing
- `RELAYER_KEYPAIR_JSON` — Solana keypair for igp-fee-claim
- `MINIO_SECRET_KEY`

### Compose Environment (hardcoded in compose)
- `HYP_BASE_GASPAYMENTENFORCEMENT='[{"type": "none"}]'` — disabled (Sealevel returns hardcoded zeros)
- `AWS_ENDPOINT_URL_S3=http://hyperlane-minio:9000`

---

## Stack 5: hyperlane-minio

### Purpose
S3-compatible storage for validator checkpoints. Replaces shared PVCs (RWX) with S3 API access (RWO-friendly).

### Services
| Service | Image | Notes |
|---------|-------|-------|
| minio | `minio/minio:latest` | `server /data`, ports 9000 (S3) + 9001 (console) |
| minio-init | `minio/mc:latest` | `restart: "no"`, creates validator buckets then exits |

### Buckets Created
- `hyperlane-validator-gorchain`
- `hyperlane-validator-solana`

---

## Stack 6: hyperlane-gas-oracle

### Purpose
Periodically fetches token prices and updates IGP gas oracle configurations on both chains.

### How It Works
1. Fetches GOR and SOL prices from CoinGecko
2. Computes exchange rates and gas prices
3. Signs `SetGasOracleConfigs` transactions via Privy Solana wallet (Ed25519)
4. Runs in a loop (`RUN_LOOP=true`) with configurable interval (default 15 min)

### Services
| Service | Image | Notes |
|---------|-------|-------|
| gas-oracle | `laconic/hyperlane-gas-oracle:local` | Node.js, loop mode via `RUN_LOOP=true` |

### Config (spec.yml)
`GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`, `GORCHAIN_IGP_PROGRAM_ID`, `SOLANA_IGP_PROGRAM_ID`, `GORCHAIN_DOMAIN_ID`, `SOLANA_DOMAIN_ID`, `GAS_ORACLE_INTERVAL_MS` (default 900000)

### Secrets (injected separately)
`PRIVY_APP_ID`, `PRIVY_APP_SECRET`, `PRIVY_ORACLE_WALLET_ID`

---

## Stack 7: hyperlane-monitoring

### Purpose
Prometheus metrics collection, alerting, Grafana dashboards, and wallet balance monitoring.

### Compose: `docker-compose-hyperlane-monitoring.yml`

| Service | Image | Notes |
|---------|-------|-------|
| prometheus | `prom/prometheus:latest` | 30d retention, k8s service discovery |
| pushgateway | `prom/pushgateway:latest` | Receives pushed metrics from balance monitor |
| grafana | `grafana/grafana:latest` | Auto-provisioned datasource + dashboards |
| balance-monitor | TBD (lightweight image with solana-cli + curl) | Runs `check-balance.sh` from ConfigMap, pushes to pushgateway at localhost:9091 |

### Balance Monitor
Script at `stack_orchestrator/data/config/balance-monitor-scripts-config/check-balance.sh`. Mounted as ConfigMap volume. Checks wallet balances on both chains and pushes metrics to pushgateway.

### ConfigMaps
- `prometheus-config` — prometheus.yml (with `kubernetes_sd_configs` for cross-pod scraping), alerts.yml
- `grafana-datasources-config` — Prometheus datasource
- `grafana-dashboard-config` — Dashboard provisioning config
- `grafana-dashboards` — hyperlane-overview.json

### deploy/commands.py
`create()` applies RBAC (ClusterRole + ClusterRoleBinding) for Prometheus k8s service discovery.

### Config (spec.yml)
`GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`, `MONITORED_WALLETS_GORCHAIN`, `MONITORED_WALLETS_SOLANA`, `BALANCE_THRESHOLD_SOL`, `BALANCE_CHECK_INTERVAL`

### Secrets (injected separately)
`GRAFANA_ADMIN_PASSWORD`

### Prometheus Scrape Targets
Uses `kubernetes_sd_configs` with pod annotation relabeling (`prometheus.io/scrape: "true"`) to discover validators/relayer in separate pods. Validator/relayer compose files should include prometheus annotations.

---

## Stack 8: hyperlane-warp-ui

### Purpose
Browser-based bridge UI (Next.js) for cross-chain token transfers.

### Services
| Service | Image | Notes |
|---------|-------|-------|
| warp-ui | `laconic/hyperlane-warp-ui:local` | Port 3000, sentinel placeholder substitution at startup |

### Ingress
HTTP proxy routes host → warp-ui:3000 via Caddy ingress controller with automatic ACME TLS.

---

## Ops Directory (Not a Stack)

On-demand k8s Jobs applied manually with `kubectl`. These are standalone Job manifests rather than SO-managed stacks.

| Job | Purpose | Signing |
|-----|---------|---------|
| kill-switch-job.yaml | Scale agents to 0, reconfigure ISM to null | Unsigned tx output → Ledger |
| restore-job.yaml | Restore ISM validators, scale agents up | Unsigned tx output → Ledger |
| teardown-job.yaml | Close programs, recover rent (DRY_RUN default) | Unsigned tx output → Ledger |
| verify-ownership-job.yaml | Read-only ownership verification | None |

---

## Container Builds

See `docs/architecture-decisions.md` § "Build Strategy" for full image build details (Dockerfile stages, version pinning, sentinel patterns) and § "Version Pinning" for the complete image registry table.

All container build artifacts (Dockerfile, entrypoint scripts, config files) live in `stack_orchestrator/data/container-build/{name}/`. There are no image source directories at the repo root — everything is consolidated into the SO directory structure.

Summary of custom images and their SO build pipeline:

| Container Name | Build Dir | repos: (cloned to ~/cerc/) | Source |
|---------------|-----------|---------------------------|--------|
| `laconic/hyperlane-svm-deployer` | `laconic-hyperlane-svm-deployer` | `github.com/hyperlane-xyz/hyperlane-monorepo@16c056a` | Multi-stage Rust build of `hyperlane-sealevel-client` + `.so` programs + `solana-verify`. Solana CLI 3.0.14. |
| `laconic/hyperlane-kms-proxy` | `laconic-hyperlane-kms-proxy` | `git.vdb.to/LaconicNetwork/hyperlane-stacks` | Go service, source at `hyperlane-kms-proxy/` |
| `laconic/hyperlane-gas-oracle` | `laconic-hyperlane-gas-oracle` | `git.vdb.to/LaconicNetwork/hyperlane-stacks` | Node.js, source at `hyperlane-gas-oracle/` |
| `laconic/hyperlane-warp-ui` | `laconic-hyperlane-warp-ui` | `github.com/hyperlane-xyz/hyperlane-warp-ui-template` | Next.js with sentinel placeholders, runtime sed substitution |

Each `build.sh` sources `build-base.sh` and runs `docker build` using the SO-cloned repo in `~/cerc/` as build context. Build scripts must NOT use relative paths back to the repo tree — components may move to different repos.

### Build Dir Contents

Each container-build dir is self-contained with its Dockerfile and any supporting files (entrypoint scripts, config templates):

```
container-build/laconic-hyperlane-svm-deployer/
  build.sh          # runs: docker build -f Dockerfile -t ...:local ~/cerc/hyperlane-monorepo
  Dockerfile        # COPY from ~/cerc/hyperlane-monorepo (no internal git clone)
  entrypoint.sh     # copied into image at build time

container-build/laconic-hyperlane-warp-ui/
  build.sh          # runs: docker build -f Dockerfile -t ...:local ~/cerc/hyperlane-warp-ui-template
  Dockerfile        # COPY from ~/cerc/hyperlane-warp-ui-template (no internal git clone)
  entrypoint.sh     # sentinel substitution + Next.js start
  configs/          # chains.yaml, warpRoutes.yaml, .env.sentinel — placeholder configs

container-build/laconic-hyperlane-kms-proxy/
  build.sh          # runs: docker build -t ...:local ~/cerc/hyperlane-stacks/hyperlane-kms-proxy

container-build/laconic-hyperlane-gas-oracle/
  build.sh          # runs: docker build -t ...:local ~/cerc/hyperlane-stacks/hyperlane-gas-oracle
```

Dockerfiles for deployer and warp-ui use `~/cerc/` as build context (COPY from the SO-cloned repo), not internal `git clone`. The Dockerfile and entrypoint.sh are passed via `-f` flag from the build dir.

---

## Deployment Order

```
1. hyperlane-minio                    (no dependencies)
2. hyperlane-svm-deployer             (creates ConfigMaps consumed by all agent stacks)
3. hyperlane-svm-warp-deployer        (needs program-ids from step 2)
4. hyperlane-validator (gorchain)  }
   hyperlane-validator (solana)    }  parallel, need agent-config from step 2
   hyperlane-relayer               }    + MinIO from step 1
5. hyperlane-gas-oracle               (needs IGP program IDs from step 2)
6. hyperlane-monitoring               (can run anytime, discovers pods dynamically)
7. hyperlane-warp-ui                  (needs warp route addresses from step 3)
```

Steps 4's two validator deployments use the same `hyperlane-validator` stack with different spec.yml files.

---

## spec.yml Conventions

- **`stack:`** — Full relative path: `stack_orchestrator/data/stacks/hyperlane-*`
- **`deploy-to:`** — `k8s-kind` for all stacks
- **`config:`** — Non-secret environment variables only. Secrets (keys, passwords) are NOT included in spec files — they are injected separately (e.g., via k8s Secrets or at deploy time).
- **`configmaps:`** — Relative to deployment dir: `./configmaps/{name}`. Operator populates these directories after `deploy create`.
- **`volumes:`** — Named volumes with explicit sizes. Data volumes → PVCs.
- **`network.ports:`** — Exposed service ports (NodePort mappings)
- **`network.http-proxy:`** — Ingress routing (warp-ui only)

### Volume Sizes

| Volume | Size | Used By |
|--------|------|---------|
| validator data | 5Gi | Stack 3 (per chain) |
| relayer data | 5Gi | Stack 4 |
| minio data | 10Gi | Stack 5 |
| prometheus data | 10Gi | Stack 7 |
| grafana data | 2Gi | Stack 7 |

### Spec Files

| Spec File | Stack |
|-----------|-------|
| `spec-deployer.yml` | `hyperlane-svm-deployer` |
| `spec-warp-deployer.yml` | `hyperlane-svm-warp-deployer` |
| `spec-validator-gorchain.yml` | `hyperlane-validator` (gorchain deployment) |
| `spec-validator-solana.yml` | `hyperlane-validator` (solana deployment) |
| `spec-relayer.yml` | `hyperlane-relayer` |
| `spec-minio.yml` | `hyperlane-minio` |
| `spec-gas-oracle.yml` | `hyperlane-gas-oracle` |
| `spec-monitoring.yml` | `hyperlane-monitoring` |
| `spec-warp-ui.yml` | `hyperlane-warp-ui` |

Both validator spec files reference the same stack (`stack_orchestrator/data/stacks/hyperlane-validator`) with different config values for `ORIGIN_CHAIN_NAME`, `CHECKPOINT_BUCKET`, `PRIVY_WALLET_ID`, etc.

## Compose File Conventions

- **Directory layout**: Long-running services use `compose/docker-compose-{pod-name}.yml` (referenced by `pods:` in stack.yml). One-shot deployers use `compose-jobs/docker-compose-{job-name}.yml` (referenced by `jobs:` in stack.yml).
- **Image tags**: Use `laconic/name:local` (what `build-containers` produces). Add comment noting future published version: `# TODO: use git.vdb.to/laconic/name:tag once CI publish workflows are set up`
- **Inline scripts**: No multi-line inline scripts in compose files. Extract to shell scripts in `stack_orchestrator/data/config/{name}-scripts-config/` dirs. Mount as ConfigMap volumes at `/opt/scripts/`. Reference via `command: ["/bin/bash", "/opt/scripts/script.sh"]`.
- **Environment variables**: All deployment-specific values come from env vars (set via spec.yml config). Compose files use `${VAR}` syntax.
- **Volumes**: Named volumes with `config` in the name → ConfigMaps in k8s. Other named volumes → PVCs.

## Cross-Stack Communication

All stacks deploy to the same k8s-kind cluster.

| From | To | Mechanism |
|------|----|-----------|
| Validators → MinIO | `http://hyperlane-minio:9000` | k8s Service created by `deploy/commands.py` |
| Relayer → MinIO | `http://hyperlane-minio:9000` | Same k8s Service |
| Validator → KMS proxy | `localhost:9999` | Same pod (must be same stack) |
| Balance monitor → Pushgateway | `localhost:9091` | Same pod (monitoring stack) |
| Prometheus → Agents | k8s service discovery | `kubernetes_sd_configs` with annotations |
| Deployer → k8s API | kubectl in-pod | RBAC grants ConfigMap create/update |
| Grafana → Prometheus | `localhost:9090` | Same pod (monitoring stack) |

### MinIO Service Discovery

SO-generated service names follow the pattern `{deployment-id}-service`, so compose hostnames don't resolve across stacks. To allow static `hyperlane-minio:9000` references in compose files, the `deploy/commands.py` hook in stacks that access MinIO (validator, relayer) creates a k8s Service named `hyperlane-minio` pointing to the MinIO deployment's pod. This decouples compose files from deployment IDs.
