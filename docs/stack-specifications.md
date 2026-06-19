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
- `volumes:` — Named volumes with explicit host paths → HostPath PVs under `/srv/kind/hyperlane/`; empty value → dynamic PVC (avoid for data that must survive cluster recreation)
- `configmaps:` — Directory → k8s ConfigMap, mounted as volume
- `network.http-proxy:` — Ingress routing rules
- `network.ports:` — NodePort mappings
- `security:` — privileged, capabilities, unlimited-memlock
- `resources:` — CPU/memory limits
- `image-pull-secret:` — Private registry auth

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

SO's constraint that **all services in a stack = one k8s Pod** means services needing independent restart, scaling, or lifecycle must be separate stacks. This drives the 9-stack decomposition below.

## Stack Inventory

| # | Stack | Type | Services | Purpose |
|---|-------|------|----------|---------|
| 1 | `hyperlane-svm-deployer` | One-time | deployer | Deploy core Hyperlane contracts on both chains |
| 2 | `hyperlane-svm-warp-deployer` | One-time | warp-deployer | Deploy warp route contracts (collateral + synthetic) |
| 3 | `hyperlane-validator` | Long-running | validator + kms-proxy | Checkpoint signing via Privy KMS proxy (one deployment per chain) |
| 4 | `hyperlane-relayer` | Long-running | relayer + igp-fee-claim | Message delivery + periodic IGP fee claiming |
| 5 | `hyperlane-minio` | Long-running | minio + minio-init | S3-compatible checkpoint storage |
| 6 | `hyperlane-gas-oracle` | Long-running | gas-oracle | Periodic IGP gas oracle updates via Privy |
| 7 | `hyperlane-monitoring` | Long-running | prometheus + grafana + balance-monitor | Metrics, dashboards, Slack balance alerts |
| 8 | `hyperlane-warp-ui` | Long-running | warp-ui | Browser-based bridge UI |
| 9 | `hyperlane-explorer` | Long-running | db + scraper + hasura + explorer | Self-hosted message indexer + search UI |
| - | `ops/` | On-demand | kubectl Jobs | Kill switch, restore, teardown, ownership verify |

### stack.yml Summary

| # | Stack | repos: | containers: | pods: | jobs: |
|---|-------|--------|-------------|-------|-------|
| 1 | `hyperlane-svm-deployer` | `github.com/gorbagana-dev/hyperlane-monorepo@v2.2.0-gorbagana.4` | `gorbagana-dev/hyperlane-svm-deployer` | — | `hyperlane-svm-deployer` |
| 2 | `hyperlane-svm-warp-deployer` | `github.com/gorbagana-dev/hyperlane-monorepo@v2.2.0-gorbagana.4` | `gorbagana-dev/hyperlane-svm-deployer` | — | `hyperlane-svm-warp-deployer` |
| 3 | `hyperlane-validator` | `github.com/gorbagana-dev/hyperlane-monorepo@v2.2.0-gorbagana.1` | `gorbagana-dev/hyperlane-kms-proxy`, `gorbagana-dev/hyperlane-agent` | `hyperlane-validator` | — |
| 4 | `hyperlane-relayer` | *(none)* | *(none)* — reuses `gorbagana-dev/hyperlane-agent` | `hyperlane-relayer` | — |
| 5 | `hyperlane-minio` | *(none)* | *(none)* | `hyperlane-minio` | — |
| 6 | `hyperlane-gas-oracle` | `github.com/gorbagana-dev/hyperlane-stacks` | `gorbagana-dev/hyperlane-gas-oracle` | `hyperlane-gas-oracle` | — |
| 7 | `hyperlane-monitoring` | *(none)* | *(none)* | `hyperlane-monitoring` | — |
| 8 | `hyperlane-warp-ui` | `github.com/gorbagana-dev/hyperlane-warp-ui-template@v2.0.0-gorbagana.6` | `gorbagana-dev/hyperlane-warp-ui` | `hyperlane-warp-ui` | — |
| 9 | `hyperlane-explorer` | `github.com/gorbagana-dev/hyperlane-explorer@v12.0.0-gorbagana.3`, `github.com/gorbagana-dev/hyperlane-monorepo@v2.2.0-gorbagana.1` | `gorbagana-dev/hyperlane-explorer`, `gorbagana-dev/hyperlane-scraper` | `hyperlane-explorer` | — |

- Stacks 1 and 2 use `jobs:` (not `pods:`) because deployers are one-shot containers — k8s Jobs (restartPolicy: Never, backoffLimit: 0) prevent CrashLoopBackOff that occurs when Deployments restart completed containers. Their compose files live in `compose-jobs/` instead of `compose/`.
- Stacks 5 and 7 use only upstream images — no repos or containers needed.
- Stacks 1 and 2 share the same repo/container (deployer image) and are independently buildable. The relayer (stack 4) builds nothing — it reuses the `hyperlane-agent` image built by the validator stack.
- Stack 7 balance-monitor will use a lightweight image (not the heavy deployer image).
- The deployer image is built from the gorbagana Hyperlane fork at `v2.2.0-gorbagana.4` — the same fork line as the agents/scraper (which track `vX.Y.Z-gorbagana.N`); the deployer additionally builds the on-chain programs. `build-programs.sh` builds every `.so` program for both SBPFv0 and SBPFv3, emitting one set per arch under `target/deploy/{v0,v3}/`, and `deploy.sh` selects the SBPF version per target chain at runtime via that cluster's SBPFv3-deployment feature gate. Solana devnet requires SBPFv3; gorchain (Agave 3.0.0) and Solana mainnet still require SBPFv0. Program logic and on-chain account layouts are unchanged, so they remain compatible with the scraper.
- Stack 9 lists its `containers:` commented out so `deploy start` doesn't `kind load` them — k8s pulls `hyperlane-explorer`/`hyperlane-scraper` from the registry instead (hasura is the upstream image). Uncomment for local builds.

---

## Stack 1: hyperlane-svm-deployer

### Purpose
One-time Job that deploys Hyperlane core contracts (Mailbox, IGP, ISM, Validator Announce, Merkle Tree Hook) on both Gorchain (domain 99999) and Solana (domain 99998).

### How It Works
1. Stack uses `jobs:` in stack.yml — runs as a k8s Job (restartPolicy: Never, backoffLimit: 0) instead of a Deployment, preventing CrashLoopBackOff after completion
2. Deploy script (`deploy.sh`) is mounted via ConfigMap volume at `/opt/scripts/` rather than baked into the Docker image — allows script updates without rebuilding the image
3. Deploys programs on both chains via `hyperlane-sealevel-client` (from `@hyperlane-xyz/core@10.2.0`)
4. Verifies on-chain program hashes via `solana-verify`
5. Transfers ownership: program authority → bridge owner (the Privy bridge-owner wallet; uses `--skip-new-upgrade-authority-signer-check` flag required by recent Solana CLI), IGP → Privy oracle
6. Writes deployment artifacts as k8s ConfigMaps via kubectl (requires RBAC)
7. Discards hot deployer key

### Domain / chain IDs

Both chains are **SVM** (Solana / agave fork) — there is no EIP-155 `chainId`;
an SVM chain identifies by its genesis hash. Hyperlane assigns a `u32` **domain**
derived from the chain name and sets `chainId == domainId`. The derivation: the
first ASCII chars of the name as big-endian bytes, then a trailing **network
byte** (`0x4D`/`0x4E`/`0x4F` for mainnet/testnet/devnet):

```
"Sol" = 0x53 0x6F 0x6C
  solana mainnet  0x536F6C4D = 1399811149   (canonical Hyperlane value)
  solana testnet  0x536F6C4E = 1399811150
  solana devnet   0x536F6C4F = 1399811151

"Gor" = 0x47 0x6F 0x72
  gorchain mainnet 0x476F724D = 1198486093   (prod)
  gorchain devnet  0x476F724F = 1198486095   (staging)
```

Solana uses its canonical registered values; gorchain has no canonical Hyperlane
domain (we deploy our own core on it), so we mint one the same way. These are
**immutable once deployed** (baked into the on-chain contracts) and live as
committed `config:` literals in the per-env specs (prod `deployment/spec-*.yml`,
staging `deployment/staging/spec-*.yml`; local/e2e use 99999/99998). Verify a
value with:

```python
python3 -c "b=b'Gor'+bytes([0x4D]); print(int.from_bytes(b,'big'))"  # 1198486093
```

### Services
| Service | Image | Notes |
|---------|-------|-------|
| deployer | `gorbagana-dev/hyperlane-svm-deployer:local` | `restart: "no"`, mounts deploy script + config templates |

### ConfigMaps (input)
- `deployer-scripts-config` — deploy.sh entrypoint
- `deployer-gas-oracle-config` — initial gas oracle configs
- `deployer-multisig-config` — per-chain multisig ISM configs (`.json.tmpl` templates rendered via envsubst)
- `deployer-registry-config` — chain registry metadata.yaml template

### ConfigMaps (output, created by deployer via kubectl)
- `hyperlane-program-ids` — deployed program addresses per chain
- `hyperlane-agent-config` — agent-config.json for validators/relayer
  (`rpcUrls` are placeholders; real URLs are env-injected — see the
  validator/relayer sections)
- `hyperlane-gas-oracle-config` — gas oracle configs
- `hyperlane-multisig-config` — multisig ISM configs

### deploy/commands.py
`create()` applies RBAC (Role + RoleBinding) granting the default ServiceAccount permission to create/update ConfigMaps in the deployment namespace.

### Config (spec.yml)
`GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`, `GORCHAIN_DOMAIN_ID` (99999), `SOLANA_DOMAIN_ID` (99998), `FORCE_REDEPLOY`

**IGP fee beneficiary.** The deployer sets the InterchainGasPaymaster beneficiary
(the account that `igp claim` pays accumulated gas fees to) via the optional
`IGP_BENEFICIARY_PUBKEY`. It is applied on both chains by `deploy.sh`
(`igp set-igp-beneficiary`, deployer-signed) immediately before IGP ownership is
handed to the oracle wallet — so the deployer must still be the IGP owner at that
point. When unset it defaults to `BRIDGE_OWNER_PUBKEY`; if neither is set the
beneficiary stays the deployer key (pre-existing behavior). The base IGP account
carries the beneficiary; the overhead IGP has none and is untouched.

### Secrets (injected separately)
`DEPLOYER_KEYPAIR`, `BRIDGE_OWNER_PUBKEY`, `IGP_ORACLE_PUBKEY`, `IGP_BENEFICIARY_PUBKEY`

---

## Stack 2: hyperlane-svm-warp-deployer

### Purpose
One-time Job that deploys the warp route contracts for the routes selected by `WARP_ROUTES`. A route is a set of fields where each side has an explicit token type: an origin side (`native` or `collateral`) and a remote side (`synthetic` or `collateral`). Route definitions are a checked-in menu; a single deployment can deploy several of them.

### How It Works
1. Same `jobs:` pattern as core deployer — runs as a k8s Job with ConfigMap-mounted deploy script at `/opt/scripts/`
2. Reads `hyperlane-program-ids` ConfigMap (created by Stack 1) for mailbox addresses
3. `deploy.sh` loops over the comma- or space-separated `WARP_ROUTES` selection. For each route it reads `/config/warp-routes/<stem>.json` (from the `warp-routes-config` ConfigMap) and builds the on-chain token-config generically with `jq` from that route's fields (origin/remote chain, type, token, and metadata) — there is no per-token template
4. Deploys the warp route programs for both sides and writes each route's addresses to state under `/state/warp-routes/<name>/`

### Warp route token model

A `collateral-and-synthetic` route spans two chains, and each chain has two
distinct on-chain objects: the Hyperlane **token-router program** and the SPL
**mint** it operates on. That 2×2 is the whole model — the four warp variables
the downstream warp-UI consumes are one per cell:

| | Token-router program | SPL mint |
|---|---|---|
| **Collateral side** (Solana) | `WARP_COLLATERAL_ADDRESS` | `WARP_TOKEN_MINT` |
| **Synthetic side** (gorchain) | `WARP_SYNTHETIC_ADDRESS` | `WARP_SYNTHETIC_MINT` |

- **`WARP_COLLATERAL_ADDRESS`** — the warp program on Solana that escrows real
  USDC. Created by the warp deploy; read from
  `warp-deploy-outputs/program-ids.json` (`<collateral-chain>.base58`).
- **`WARP_SYNTHETIC_ADDRESS`** — the warp program on gorchain that mints/burns
  USDC. Created by the warp deploy; `<synthetic-chain>.base58` from the same file.
- **`WARP_TOKEN_MINT`** — the SPL mint of the **collateral** token = the real
  Solana USDC mint. It **pre-exists** (Circle's mint); the operator supplies it
  as the deployer input `WARP_TOKEN_MINT`, and the deployer echoes it back out as
  `token-config.json` → `warpRoute.tokenMint`. Named from the deployer's "token
  to bridge" perspective; conceptually it is the collateral-side mint.
- **`WARP_SYNTHETIC_MINT`** — the SPL mint of the **synthetic** token = the USDC
  mint on gorchain. It is **created** by the warp deploy (that is what "synthetic"
  means — the wrapped token is minted into existence). Its address is the PDA
  `find_program_address(["hyperlane_token","-","mint"], WARP_SYNTHETIC_ADDRESS)`,
  so it is fully determined by the synthetic program.

Pre-exists vs minted is exactly what makes this route `collateral-and-synthetic`
rather than `collateral-collateral`; it holds only because gorchain has no native
canonical USDC.

**Why the synthetic mint must be emitted, not left blank.** Although the mint is a
deterministic PDA, the warp-UI's Hyperlane SDK (`@hyperlane-xyz/sdk`) does **not**
auto-derive it: `Token.getHypAdapter`'s `SealevelHypSynthetic` branch asserts
`collateralAddressOrDenom` is present and feeds it to the adapter as the mint used
for balance/ATA lookups — an empty value throws and the gorchain side of the UI
fails to construct. So the deployer resolves it explicitly: after deploy it runs
`hyperlane-sealevel-client token query --program-id <synthetic-program> synthetic`,
parses the `Mint / Mint Authority:` line, and writes it to `token-config.json` →
`warpRoute.synthetic.mint`. The deployer then aggregates all route configs into
`/state/warp-routes/warpRoutes.yaml` (a Hyperlane `WarpCoreConfig`), which the ops
layer distributes into the `warp-ui-config` ConfigMap for the warp-UI to serve
directly. (The e2e suite reads this file from state via `bridge_state_loader` and
asserts it in `test_02_warp_deployer.py`, and `test_10_warp_ui.py` verifies it is
served by the warp-UI container.)

### Services
| Service | Image | Notes |
|---------|-------|-------|
| warp-deployer | `gorbagana-dev/hyperlane-svm-deployer:local` | Same deployer image, different entrypoint script |

### Dependencies
- Requires Stack 1 (core deployer) to have run first

### Config (spec.yml)
The spec selects routes and carries shared chain/control config; the per-route
fields live in the menu, not the spec.

- **`WARP_ROUTES`** — comma- or space-separated list of route stems to deploy (e.g. `"usdc"`
  in prod, `"usdc sol"` locally/e2e). Each stem must have a menu file (below) and
  must match the `warp_routes` selection in
  `ops/inventories/{prod,local}/group_vars/all.yml`.
- Shared chain config (`GORCHAIN_RPC_URL`, domain/chain IDs, `*_IS_TESTNET`) and
  `FORCE_REDEPLOY`. The Solana origin RPC (`SOLANA_RPC_URL`) is a secret.
- `configmaps:` includes `warp-routes-config`, the runtime-populated ConfigMap
  that carries the selected routes (see below).

#### Route menu
Each route is defined in a checked-in per-env menu file. The deployment menus —
`deployment/bridges/default/warp-routes/<stem>.yml` (prod) /
`deployment/local/bridges/default/warp-routes/<stem>.yml` (local) — ship the
operator routes; the e2e suite owns a parallel menu under
`tests/e2e/fixtures/warp-routes/<stem>.yml`. A menu file describes one route: its
`name`, the origin and remote sides (`chain`/`type`/`token`/`name`/`symbol`/
`decimals`, each side may label or scale the asset independently), the
synthetic-side `metadataUri`, and an optional `logoURI` (the token logo the
warp-UI shows for both sides — it reads `warpRoutes.yaml`, not the on-chain
Token-2022 metadata, so this is distinct from `metadataUri`). Deployment menus
ship `usdc`; the e2e menu adds `sol` (a native-route test vehicle).

#### warp-routes-config ConfigMap
The selected routes are carried into the deployer as the `warp-routes-config`
ConfigMap (mounted at `/config/warp-routes/`), one `<stem>.json` per selected
route. Like `agent-config`, it is runtime-populated and has no `data/config/`
source dir: the ops layer renders the deployment menu YAML→JSON
(`ops/roles/common/tasks/load_warp_routes.yml`), and e2e renders its own fixtures
menu the same way in conftest's `_write_warp_menu`. `deploy.sh` reads `/config/warp-routes/<stem>.json`
for each selected route.

### Secrets (injected separately)
`DEPLOYER_KEYPAIR`, `BRIDGE_OWNER_PUBKEY`

### Multiple routes

A single deployment deploys all routes named in `WARP_ROUTES`. To add a route,
check a menu file into `bridges/default/warp-routes/`, add its stem to the
spec's `WARP_ROUTES`, and re-run the deployment (ops derives its render list from
`WARP_ROUTES`). Each route keeps its state under `/state/warp-routes/<name>/`
(where `<name>` is the menu file's `name:` field), so routes do not collide and
the deploy script's idempotency check is scoped per route: an already-deployed
route self-skips (its `token-config.json` exists) unless `FORCE_REDEPLOY=true`.

Each route also gets a scoped, RPC-redacted `deploy.log` under
`/state/warp-routes/<name>/`, which rides the existing `publish-bridge-state.yml`
flow into git alongside the other bridge state.

After all selected routes are deployed, `deploy.sh` invokes
`build-warp-ui-config.sh`, which aggregates every route's `token-config.json` into
a single Hyperlane `WarpCoreConfig` and writes it to
`/state/warp-routes/warpRoutes.yaml`. The ops layer distributes this file into the
`warp-ui-config` ConfigMap (via `state_distribute`) so the warp-UI container can
serve it directly at startup — no separate build step is needed.

#### Idempotent re-runs
The warp-deployer compose service carries a `laconic.recreate-job: "true"`
label. On `deployment start`, stack-orchestrator deletes and recreates the
completed Job rather than treating it as already-applied, so re-running the
deployment picks up newly-selected routes while finished routes skip via the
per-route idempotency check.

The relayer, gas-oracle, validators, and storage are route-agnostic — they
operate at the mailbox/domain level — so adding a route needs no per-route
changes to those stacks.

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
| agent-config-init | `alpine/kubectl:1.35.3` | Init container — fetches `hyperlane-agent-config` ConfigMap to shared PVC |
| validator | `ghcr.io/gorbagana-dev/hyperlane-agent:latest` | CLI args from env vars, metrics on :9090 |
| kms-proxy | `ghcr.io/gorbagana-dev/hyperlane-kms-proxy:latest` | Port 9999, proxies KMS Sign/GetPublicKey/DescribeKey to Privy |

Validator command uses env vars for all chain-specific args:
- `--origin-chain-name ${ORIGIN_CHAIN_NAME}`
- `--checkpointSyncer.bucket ${CHECKPOINT_BUCKET}`
- `--validator.id ${PRIVY_WALLET_ID}`

MinIO endpoint uses the static hostname `hyperlane-minio` — a k8s Service with this name is created by `deploy/commands.py` in stacks that need MinIO access (see Cross-Stack Communication).

### Config (spec.yml)
- `ORIGIN_CHAIN_NAME` — chain name (e.g., `gorchain` or `solana`), set per deployment
- `CHECKPOINT_BUCKET` — S3 bucket name, set per deployment
- `PRIVY_WALLET_ID` — Privy wallet ID (varies per chain deployment)
- `HYP_CHAINS_GORCHAIN_CUSTOMRPCURLS` — real gorchain RPC URL override (compose
  default keeps it parseable on the solana validator); the solana validator
  gets `HYP_CHAINS_SOLANA_CUSTOMRPCURLS` via `secrets:` instead

### Secrets (injected separately)
- `PRIVY_APP_ID`, `PRIVY_APP_SECRET` — Privy API credentials for KMS proxy
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` — MinIO credentials for checkpoint storage
- `HYP_DEFAULTSIGNER_KEY` — ed25519 hex key for on-chain announce tx (hot key, separate from KMS validator key)

### Compose Environment (hardcoded in compose, not in spec)
- `AWS_ENDPOINT_URL_KMS=http://localhost:9999` — routes to sidecar
- `AWS_ENDPOINT_URL_S3=http://hyperlane-minio:9000` — routes to MinIO k8s Service.
  In prod this endpoint is topology-aware: on single-host the `hyperlane-minio`
  Service is a selector-mode `external-services:` entry backed by the MinIO pod
  (in-cluster, plain HTTP — no Caddy hairpin); on multi-host, specs render
  `AWS_ENDPOINT_URL_S3` to `https://s3.<base_domain>` instead. The compose
  literal is always `http://hyperlane-minio:9000`; `render_spec` swaps the target
  at deploy time.
- `CONFIG_FILES=/config/agent-config.json`

### ConfigMaps
- `agent-config` — agent-config.json (created by deployer, or template)

### Per-Chain Deployment
Two separate spec.yml files deploy this stack for each chain:
- `spec-validator-gorchain.yml` — sets `ORIGIN_CHAIN_NAME=gorchain`, `PRIVY_WALLET_ID=<gorchain-wallet>`, `CHECKPOINT_BUCKET=hyperlane-validator-gorchain`
- `spec-validator-solana.yml` — sets `ORIGIN_CHAIN_NAME=solana`, `PRIVY_WALLET_ID=<solana-wallet>`, `CHECKPOINT_BUCKET=hyperlane-validator-solana`

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
| agent-config-init | `alpine/kubectl:1.35.3` | Init container — fetches `hyperlane-agent-config` ConfigMap to shared PVC |
| relayer | `ghcr.io/gorbagana-dev/hyperlane-agent:latest` | `relayer` subcommand, gas enforcement `none`, metrics on :9091 |
| igp-fee-claim | `ghcr.io/gorbagana-dev/hyperlane-svm-deployer:latest` | Runs `claim-fees.sh` from ConfigMap, loops every `CLAIM_INTERVAL_SECONDS` (default 6h) |

### IGP Fee Claim Sidecar
Script at `stack_orchestrator/data/config/igp-fee-claim-scripts-config/claim-fees.sh`. Mounted as ConfigMap volume. Claims accumulated IGP fees on both chains using the relayer key for tx fees (permissionless operation).

### Config (spec.yml)
- `GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`
- `HYP_CHAINS_GORCHAIN_CUSTOMRPCURLS` / `HYP_CHAINS_SOLANA_CUSTOMRPCURLS` —
  real chain RPC URLs overriding the placeholder `rpcUrls` in agent-config.json
  (solana via `secrets:` — the Helius URL embeds an API key)
- `GORCHAIN_IGP_PROGRAM_ID`, `SOLANA_IGP_PROGRAM_ID` — for igp-fee-claim sidecar
- `GORCHAIN_IGP_ACCOUNT`, `SOLANA_IGP_ACCOUNT` — IGP account addresses for fee claims
- `CLAIM_INTERVAL_SECONDS` — igp-fee-claim loop interval, operator-tunable (default 21600 = 6h)

### Secrets (injected separately)
- `HYP_CHAINS_GORCHAIN_SIGNER_KEY` — hex ed25519 key for Gorchain delivery txs
- `HYP_CHAINS_SOLANA_SIGNER_KEY` — hex ed25519 key for Solana delivery txs
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` — MinIO credentials for reading checkpoints
- `RELAYER_KEYPAIR_JSON` — Solana keypair JSON (byte array) for igp-fee-claim

### Compose Environment (hardcoded in compose)
- `HYP_GASPAYMENTENFORCEMENT='[{"type": "none"}]'` — disabled (Sealevel returns hardcoded zeros)
- `AWS_ENDPOINT_URL_S3=http://hyperlane-minio:9000` — topology-aware in prod (same
  as the validator: in-cluster selector-mode Service on single-host,
  `https://s3.<base_domain>` on multi-host)

---

## Stack 5: hyperlane-minio

### Purpose
S3-compatible storage for validator checkpoints. Replaces shared PVCs (RWX) with S3 API access (RWO-friendly).

### Services
| Service | Image | Notes |
|---------|-------|-------|
| minio | `minio/minio:RELEASE.2025-09-07T16-13-09Z` | `server /data`, ports 9000 (S3) + 9001 (console) |
| minio-init | `minio/mc:RELEASE.2025-08-13T08-35-41Z` | `restart: "no"`, creates validator buckets then exits |

### Buckets Created
- `hyperlane-validator-gorchain`
- `hyperlane-validator-solana`

### Prod topology note
MinIO itself is always accessed at `http://hyperlane-minio:9000` by validator and relayer
pods. In prod, `AWS_ENDPOINT_URL_S3` is topology-aware: on single-host, `render_spec`
injects a selector-mode `external-services:` block so the `hyperlane-minio` hostname
resolves in-cluster (no Caddy loopback); on multi-host it renders to the public
`https://s3.<base_domain>` Caddy front instead.

---

## Stack 6: hyperlane-gas-oracle

### Purpose
Periodically fetches token prices and updates IGP gas oracle configurations on both chains.

### How It Works
1. Fetches sGOR and SOL prices from CoinGecko (or configurable price endpoint)
2. Converts sGOR price to gGOR (Gorchain native token) via configurable multiplier
3. Computes exchange rates using `@hyperlane-xyz/sdk` `getLocalStorageGasOracleConfig()` (1e19 Sealevel scale, with margin)
4. Builds `SetGasOracleConfigs` instructions using SDK Borsh serialization
5. Signs via Privy server wallet (production) or local keypair (testing)
6. Runs in a loop (`RUN_LOOP=true`) with configurable interval (default 15 min)

### Services
| Service | Image | Notes |
|---------|-------|-------|
| gas-oracle | `ghcr.io/gorbagana-dev/hyperlane-gas-oracle:latest` | TypeScript, loop mode via `RUN_LOOP=true` |

### Config (spec.yml)
`GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`, `GORCHAIN_IGP_PROGRAM_ID`, `SOLANA_IGP_PROGRAM_ID`, `GORCHAIN_DOMAIN_ID`, `SOLANA_DOMAIN_ID`, `GAS_ORACLE_INTERVAL_MS` (default 900000), `GAS_PRICE` (default "0.000000001"), `GAS_OVERHEAD` (default 200000), `EXCHANGE_RATE_MARGIN_PCT` (default 10), `MIN_USD_COST` (default "0.50"), `GORCHAIN_NATIVE_TOKEN_MULTIPLIER` (default 100), `SIGNER_MODE` ("privy" or "keypair")

### Secrets (injected separately)
Privy mode: `PRIVY_APP_ID`, `PRIVY_APP_SECRET`, `PRIVY_ORACLE_WALLET_ID`
Keypair mode: `ORACLE_KEYPAIR`

---

## Stack 7: hyperlane-monitoring

### Purpose
Prometheus metrics collection, Grafana dashboards, and signer balance monitoring with Slack alerts.

### Compose: `docker-compose-hyperlane-monitoring.yml`

| Service | Image | Notes |
|---------|-------|-------|
| prometheus | `prom/prometheus:latest` | 30d retention, k8s service discovery |
| grafana | `grafana/grafana:latest` | Auto-provisioned datasource + dashboards |
| balance-monitor | `ghcr.io/gorbagana-dev/hyperlane-balance-monitor:latest` | Runs `check-balance.py` from ConfigMap, posts low-balance alerts to Slack |

### Balance Monitor
Script at `stack_orchestrator/data/config/balance-monitor-scripts-config/check-balance.py`.
Reads `/config/watches.json` (the `balance-monitor-config` ConfigMap) and, every
`BALANCE_CHECK_INTERVAL` seconds, checks each account's native and/or SPL token
balances against per-token thresholds. Below threshold → batched POST to
`SLACK_WEBHOOK_URL` (empty disables alerting); re-alerts every `ALERT_REPEAT_SECONDS`
while still low, posts one recovery message on return above. RPC URLs come from
`GORCHAIN_RPC_URL` / `SOLANA_RPC_URL` (kept out of the watch file). No
Prometheus/Pushgateway metric is emitted for balances — the Pushgateway service, its
scrape job, the `WalletBalanceLow` alert rule, and the Grafana overview balance panel
were removed. Watch schema: `{"watches":[{"chain","label","address","tokens":[{"symbol","mint","threshold"}]}]}`;
`mint:"native"` (or omitted) → native gas balance, any other `mint` → that SPL token's
balance (summed, decimals from RPC).

### ConfigMaps
- `prometheus-config` — prometheus.yml (with `kubernetes_sd_configs` for cross-pod scraping), alerts.yml
- `grafana-datasources-config` — Prometheus datasource
- `grafana-dashboard-config` — Dashboard provisioning config
- `grafana-dashboards` — hyperlane-overview.json
- `balance-monitor-scripts-config` — `check-balance.py`
- `balance-monitor-config` — `watches.json` (runtime-generated; in ops, rendered from the bridge's own signers)

### deploy/commands.py
`create()` applies RBAC (ClusterRole + ClusterRoleBinding) for Prometheus k8s service discovery.

### Config (spec.yml)
`GORCHAIN_RPC_URL`, `BALANCE_CHECK_INTERVAL`, `ALERT_REPEAT_SECONDS`, `PROMETHEUS_VALIDATOR_TARGETS`, `PROMETHEUS_RELAYER_TARGETS`

### Secrets (injected separately)
`GF_SECURITY_ADMIN_PASSWORD`, `SLACK_WEBHOOK_URL` (empty disables alerting), `SOLANA_RPC_URL` (Helius URL embeds an API key)

### Prometheus Scrape Targets
Uses `kubernetes_sd_configs` with pod annotation relabeling (`prometheus.io/scrape: "true"`) to discover validators/relayer in separate pods. Validator/relayer compose files should include prometheus annotations.

### Resources
All three containers reserve `0.1` CPU each (300m total) with no CPU limit, so they
burst when needed. The reservations are deliberately small because these services
idle low; the single-host node is sized for headroom rather than padded per-stack
reservations (see `ops/runbooks/prod.md` — co-locating the full bridge + both
validators + explorer needs ≥ 8 vCPU).

---

## Stack 8: hyperlane-warp-ui

### Purpose
Browser-based bridge UI (Next.js) for cross-chain token transfers.

### Services
| Service | Image | Notes |
|---------|-------|-------|
| warp-ui | `gorbagana-dev/hyperlane-warp-ui:local` | Port 3000, sentinel placeholder substitution at startup |

### Ingress
HTTP proxy routes host → warp-ui:3000 via Caddy (SO's ingress controller) with automatic ACME TLS.

### Solana RPC proxy (key protection)
The browser-facing `chains.yaml` must not carry the keyed `SOLANA_RPC_URL` (the
Helius URL embeds an API key). When the spec sets `WARP_UI_PUBLIC_URL`
(staging/prod), the entrypoint renders solana's rpcUrl as
`${WARP_UI_PUBLIC_URL}/api/rpc/solana` — a same-origin Next.js route in the UI
that forwards an allowlisted set of JSON-RPC methods to `SOLANA_RPC_URL`
server-side. Rejected methods return JSON-RPC `-32601` and log in the pod.
When `WARP_UI_PUBLIC_URL` is unset (e2e/local, keyless localhost RPCs), the
direct URL is rendered as before.

### Chain display (name + logo)
The entrypoint renders each chain's `displayName` and `logoURI` into
`chains.yaml`, defaulting to `Gorbagana`/`/gorbagana-logo.jpg` and
`Solana`/`/solana-logo.png` (overridable via `*_DISPLAY_NAME`/`*_LOGO_URI`
env). The logos are checked into the UI image's `public/` and served
same-origin (next/image won't render SVG without `dangerouslyAllowSVG`). The
fork also empties the upstream template's hardcoded SVM chain list
(`consts/chains.ts`), which otherwise added empty, un-bridgeable chains — and a
duplicate "Solana" — to the selector.

### Explorer link
The transfer-details modal shows a "View in Explorer" link to
`<EXPLORER_URL>/message/<msgId>` (the Hyperlane explorer message scheme) when the
spec sets the optional `EXPLORER_URL` (staging → `explorer.staging.gorbagana.wtf`,
prod → `explorer.bridge.gorbagana.wtf`); blank/unset (local, e2e) hides the link.
Like the WalletConnect id, `EXPLORER_URL` is a build-time `NEXT_PUBLIC_*` value, so
the image bakes a `__NEXT_PUBLIC_EXPLORER_URL__` sentinel and the entrypoint
substitutes the per-deployment value at container start — one image serves every
environment. (The explorer service itself is a separate, future stack.)

### Known limitation: wallet-side confirmation
After signing, Backpack submits the transaction itself and waits for its own
confirmation (WebSocket `signatureSubscribe` against the **wallet's**
configured RPC) before answering the dapp. On an RPC whose WS drops
notifications (e.g. the public devnet endpoint), Backpack hangs at
"Confirming Transaction" and the UI stays at "Sign transfer transaction…"
even though the transfer lands and delivery completes. This is inside the
wallet — the UI's HTTP-polling confirm (widgets patch, hyp-915) only runs
after the wallet responds. Mitigation is wallet-side RPC choice (see the
staging runbook); mainnet wallets default to reliable infrastructure.

---

## Stack 9: hyperlane-explorer

### Purpose
Self-hosted Hyperlane message explorer — indexes both chains' mailboxes into
Postgres and serves a Next.js search UI for cross-chain messages. Replaces the
hosted explorer.hyperlane.xyz, which does not know about the custom gorchain
domain.

### Compose: `docker-compose-hyperlane-explorer.yml`

One pod, four services:

| Service | Image | Notes |
|---------|-------|-------|
| db | `postgres:15` | Upstream image; persistent volume `explorer-postgres-data` (20Gi prod). Named `db` (not `postgres`) so SO's localhost-rewrite of sibling service names doesn't corrupt the word "postgres" in other services' env values. |
| scraper | `ghcr.io/gorbagana-dev/hyperlane-scraper:latest` | Built from the gorbagana monorepo fork `@v2.2.0-gorbagana.1`; indexes both mailboxes into Postgres; metrics on :9090 |
| hasura | `hasura/graphql-engine:v2.36.0.cli-migrations-v3` | Upstream image wired via the `hasura-config` ConfigMap (entrypoint + flattened metadata); GraphQL on :8080 |
| explorer | `ghcr.io/gorbagana-dev/hyperlane-explorer:latest` | Next.js standalone from the `gorbagana-dev/hyperlane-explorer` fork `@v12.0.0-gorbagana.3`; UI + `/api/graphql` proxy on :3000; public ingress |

### How It Works (data flow)
1. The **scraper** reads `agent-config.json` (mailbox addresses, domain ids, IGP,
   `index.from`) from the `agent-config` ConfigMap — the same ConfigMap the
   relayer and validators mount, populated by `state_distribute` from the
   deployer's output. It indexes both the gorchain and solana mailboxes into
   Postgres (messages, deliveries, gas-payments, blocks, txs), and seeds the
   gorchain + solana `domain` rows idempotently (`ON CONFLICT (id) DO NOTHING`)
   in its entrypoint.
2. **Hasura** serves read-only GraphQL over `message_view` (which carries
   `total_gas_payment`) and `domain` to an anonymous, select-only role
   (aggregations enabled).
3. The **explorer** frontend serves the UI plus a same-origin `/api/graphql`
   proxy. The browser only ever calls the frontend — never Hasura or Postgres
   directly. The frontend renders chain metadata (core addresses + domain ids)
   from the mounted `agent-config.json`; gorchain's RPC is public (browser-safe)
   and Solana's is a placeholder (the browser never calls it).

### Config (spec.yml)
- `GORCHAIN_RPC_URL` — used by the scraper (`HYP_CHAINS_GORCHAIN_CUSTOMRPCURLS`)
  and injected into the frontend chain metadata
- `GORCHAIN_DOMAIN_ID`, `SOLANA_DOMAIN_ID`, `GORCHAIN_CHAIN_ID`, `SOLANA_CHAIN_ID`
  — committed per-env literals; consumed by the scraper's domain-seed step
- `GORCHAIN_CHAIN_NAME`, `SOLANA_CHAIN_NAME` (defaults `gorchain`/`solana`)
- `GORCHAIN_IS_TESTNET`, `SOLANA_IS_TESTNET` — sets `domain.is_test_net`
  (prod `false`; staging/local `true`)
- `GORCHAIN_NATIVE_TOKEN_SYMBOL`, `SOLANA_NATIVE_TOKEN_SYMBOL` (defaults `GOR`/`SOL`)

### Compose Environment (hardcoded in compose, not in spec)
- scraper: `HYP_CHAINSTOSCRAPE="gorchain,solana"`, `HYP_METRICSPORT=9090`,
  `CONFIG_FILES=/config/agent-config.json`
- explorer: `HASURA_GRAPHQL_URL=http://hasura:8080/v1/graphql` (server-side only;
  `hasura` resolves to localhost in-pod)

### Secrets (injected separately)
- `HYP_CHAINS_SOLANA_CUSTOMRPCURLS` ← `SOLANA_RPC_URL` (Helius URL embeds an API
  key; scraper-only — the browser never sees it)
- `POSTGRES_PASSWORD` — built into the DSN at runtime by the scraper/hasura
  entrypoints
- `HASURA_GRAPHQL_ADMIN_SECRET` — enforces the anonymous read-only role

`POSTGRES_PASSWORD` and `HASURA_GRAPHQL_ADMIN_SECRET` are generated by the ops
credentials role (like the Grafana admin password).

### ConfigMaps
- `agent-config` — agent-config.json; runtime-populated by `state_distribute`,
  no `data/config/` source dir (same ConfigMap the relayer/validators mount)
- `hasura-config` — Hasura entrypoint + flattened metadata; source dir
  `data/config/hasura-config/`

### Ingress
HTTP proxy routes host → `explorer:3000` via Caddy (SO's ingress controller) with
automatic ACME TLS. Per-env hostnames:

| Environment | Host |
|---|---|
| prod | `explorer.bridge.gorbagana.wtf` |
| staging | `explorer.staging.gorbagana.wtf` |
| local | `explorer.<base_domain>` |

### Image overrides
`image-overrides:` currently pins `explorer` and `scraper` to `:latest` for the
first bring-up; these are to be pinned to release tags once the images are
published and tested.

### Resources (prod)
| Container | CPU (req/limit) | Memory (req/limit) |
|---|---|---|
| db | 0.5 / 2.0 | 1024M / 2048M |
| scraper | 0.5 / 1.0 | 512M / 1024M |
| hasura | 0.25 / 0.5 | 256M / 512M |
| explorer | 0.25 / 0.5 | 256M / 512M |

Volume: `explorer-postgres-data` 20Gi.

### Cross-Stack Dependencies
- Deployer stack must have run (`agent-config.json` carries the mailbox/domain
  metadata the scraper indexes and the frontend renders)

### Known limitations
- **E2E coverage is smoke-level only.** `tests/e2e/test_15_explorer.py` checks
  pods running, the frontend serving HTML, and the `/api/graphql` proxy resolving
  a `domain` query. There is no end-to-end "bridged message appears in
  `message_view`" assertion yet. Tracked in pebble `hyp-277`.
- **Startup relies on crash-loop-until-schema-ready.** Neither the scraper nor
  the hasura entrypoint waits for DB readiness; both restart until Postgres and
  the schema are up.

---

## Ops Directory (Removed — Not a Stack)

**Removed** (formerly at `deployment/ops-archive/`, since deleted). These unsigned-tx k8s Jobs are superseded:
maintenance ops will be rebuilt as operator-layer playbooks (epic `hyp-564`)
signing with the Privy bridge-owner wallet — the bridge owner is a Privy server
wallet (`BRIDGE_OWNER_PUBKEY`), not a hardware wallet.

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
| `gorbagana-dev/hyperlane-svm-deployer` | `gorbagana-dev-hyperlane-svm-deployer` | `github.com/gorbagana-dev/hyperlane-monorepo@v2.2.0-gorbagana.4` | Multi-stage Rust build of `hyperlane-sealevel-client` + `.so` programs (built for both SBPFv0 and SBPFv3) + `solana-verify`. Solana CLI 4.0.3 (Agave 4.x). |
| `gorbagana-dev/hyperlane-kms-proxy` | `gorbagana-dev-hyperlane-kms-proxy` | `github.com/gorbagana-dev/hyperlane-stacks` | Go service, source at `hyperlane-kms-proxy/` |
| `gorbagana-dev/hyperlane-gas-oracle` | `gorbagana-dev-hyperlane-gas-oracle` | `github.com/gorbagana-dev/hyperlane-stacks` | TypeScript, source at `hyperlane-gas-oracle/`, uses `@hyperlane-xyz/sdk` |
| `gorbagana-dev/hyperlane-warp-ui` | `gorbagana-dev-hyperlane-warp-ui` | `github.com/gorbagana-dev/hyperlane-warp-ui-template@v2.0.0-gorbagana.6` | Next.js standalone build of the warp-ui fork; runtime config files + one WalletConnect sentinel |
| `gorbagana-dev/hyperlane-explorer` | `gorbagana-dev-hyperlane-explorer` | `github.com/gorbagana-dev/hyperlane-explorer@v12.0.0-gorbagana.3` | Next.js standalone build of the explorer fork |
| `gorbagana-dev/hyperlane-scraper` | `gorbagana-dev-hyperlane-scraper` | `github.com/gorbagana-dev/hyperlane-monorepo@v2.2.0-gorbagana.1` | Rust build of the Sealevel scraper agent from the monorepo fork |

Each `build.sh` sources `build-base.sh` and runs `docker build` using the SO-cloned repo in `~/cerc/` as build context. Build scripts must NOT use relative paths back to the repo tree — components may move to different repos.

### Build Dir Contents

Each container-build dir is self-contained with its Dockerfile and any supporting files (entrypoint scripts, config templates):

```
container-build/gorbagana-dev-hyperlane-svm-deployer/
  build.sh          # runs: docker build -f Dockerfile -t ...:local ~/cerc/hyperlane-monorepo
  Dockerfile        # COPY from ~/cerc/hyperlane-monorepo (no internal git clone)
  entrypoint.sh     # copied into image at build time

container-build/gorbagana-dev-hyperlane-warp-ui/
  build.sh          # runs: docker build -f Dockerfile -t ...:local ~/cerc/hyperlane-warp-ui-template
  Dockerfile        # COPY from ~/cerc/hyperlane-warp-ui-template (no internal git clone)
  entrypoint.sh     # sentinel substitution + Next.js start
  configs/          # chains.yaml, warpRoutes.yaml, .env.sentinel — placeholder configs

container-build/gorbagana-dev-hyperlane-kms-proxy/
  build.sh          # runs: docker build -t ...:local ~/cerc/hyperlane-stacks/hyperlane-kms-proxy

container-build/gorbagana-dev-hyperlane-gas-oracle/
  build.sh          # runs: docker build -t ...:local ~/cerc/hyperlane-stacks/hyperlane-gas-oracle

container-build/gorbagana-dev-hyperlane-explorer/
  build.sh          # runs: docker build -f Dockerfile -t ...:local ~/cerc/hyperlane-explorer
  Dockerfile        # Next.js standalone build from ~/cerc/hyperlane-explorer
  entrypoint.sh     # renders chain metadata, starts the standalone server

container-build/gorbagana-dev-hyperlane-scraper/
  build.sh          # runs: docker build -f Dockerfile -t ...:local ~/cerc/hyperlane-monorepo
  Dockerfile        # Rust build of the Sealevel scraper from ~/cerc/hyperlane-monorepo
  entrypoint.sh     # builds DSN, seeds domain rows, starts the scraper
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
8. hyperlane-explorer                 (needs agent-config from step 2)
```

Steps 4's two validator deployments use the same `hyperlane-validator` stack with different spec.yml files.

---

## spec.yml Conventions

- **`stack:`** — Full relative path: `stack_orchestrator/data/stacks/hyperlane-*`
- **`deploy-to:`** — `k8s-kind` for all stacks
- **`config:`** — Non-secret environment variables only. Secrets (keys, passwords) are NOT included in spec files — they are injected separately (e.g., via k8s Secrets or at deploy time).
- **`configmaps:`** — Relative to deployment dir: `./configmaps/{name}`. Operator populates these directories after `deploy create`.
- **`volumes:`** — Data volumes map to explicit host paths under `/srv/kind/hyperlane/<stack>/` (see layout below). This makes data survive cluster recreation. Do not leave data volume values empty — that produces a dynamic PVC which is lost on cluster delete.
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
| explorer-postgres-data | 20Gi | Stack 9 |

### Host-Path Layout

All stacks share `kind-mount-root: /srv/kind/hyperlane`. Kind mounts this directory from the host into the cluster node; every data volume is a named subdir:

```
/srv/kind/hyperlane/
  bridge/
    generated/          ← svm-deployer output (program-ids.json, etc.)
    logs/               ← deployer job logs
  minio/
    data/               ← MinIO object store
  validator-gorchain/
    data/               ← gorchain validator state
  validator-solana/
    data/               ← solana validator state
  relayer/
    data/               ← relayer state
  monitoring/
    prometheus/         ← Prometheus TSDB
    grafana/            ← Grafana database
  explorer/
    postgres/           ← Explorer Postgres data
```

Tests use the same layout under `/tmp/hyperlane-bridge-e2e/`.

### Spec Files

| Spec File | Stack |
|-----------|-------|
| `spec-deployer.yml` | `hyperlane-svm-deployer` |
| `spec-warp-deployer.yml` (local: `local/spec-warp-deployer.yml`) | `hyperlane-svm-warp-deployer` |
| `spec-validator-gorchain.yml` | `hyperlane-validator` (gorchain deployment) |
| `spec-validator-solana.yml` | `hyperlane-validator` (solana deployment) |
| `spec-relayer.yml` | `hyperlane-relayer` |
| `spec-minio.yml` | `hyperlane-minio` |
| `spec-gas-oracle.yml` | `hyperlane-gas-oracle` |
| `spec-monitoring.yml` | `hyperlane-monitoring` |
| `spec-warp-ui.yml` | `hyperlane-warp-ui` |
| `spec-explorer.yml` (staging: `staging/spec-explorer.yml`, local: `local/spec-explorer.yml`) | `hyperlane-explorer` |

Both validator spec files reference the same stack (`stack_orchestrator/data/stacks/hyperlane-validator`) with different config values for `ORIGIN_CHAIN_NAME`, `CHECKPOINT_BUCKET`, `PRIVY_WALLET_ID`, etc.

## Compose File Conventions

- **Directory layout**: Long-running services use `compose/docker-compose-{pod-name}.yml` (referenced by `pods:` in stack.yml). One-shot deployers use `compose-jobs/docker-compose-{job-name}.yml` (referenced by `jobs:` in stack.yml).
- **Image tags**: Use `gorbagana-dev/name:local` (what `build-containers` produces). Add comment noting future published version: `# TODO: use ghcr.io/gorbagana-dev/name:tag once CI publish workflows are set up`
- **Inline scripts**: No multi-line inline scripts in compose files. Extract to shell scripts in `stack_orchestrator/data/config/{name}-scripts-config/` dirs. Mount as ConfigMap volumes at `/opt/scripts/`. Reference via `command: ["/bin/bash", "/opt/scripts/script.sh"]`.
- **Environment variables**: All deployment-specific values come from env vars (set via spec.yml config). Compose files use `${VAR}` syntax.
- **Volumes**: Named volumes with `config` in the name → ConfigMaps in k8s. Other named volumes → host-path PVs (path set in the spec file under `volumes:`).

## Cross-Stack Communication

All stacks deploy to the same k8s-kind cluster.

| From | To | Mechanism |
|------|----|-----------|
| Validators → MinIO | `http://hyperlane-minio:9000` | k8s Service created by `deploy/commands.py` |
| Relayer → MinIO | `http://hyperlane-minio:9000` | Same k8s Service |
| Validator → KMS proxy | `localhost:9999` | Same pod (must be same stack) |
| Balance monitor → Slack | `SLACK_WEBHOOK_URL` | Outbound webhook POST (monitoring stack) |
| Prometheus → Agents | k8s service discovery | `kubernetes_sd_configs` with annotations |
| Deployer → k8s API | kubectl in-pod | RBAC grants ConfigMap create/update |
| Grafana → Prometheus | `localhost:9090` | Same pod (monitoring stack) |

### MinIO Service Discovery

SO-generated service names follow the pattern `{deployment-id}-service`, so compose hostnames don't resolve across stacks. To allow static `hyperlane-minio:9000` references in compose files, the `deploy/commands.py` hook in stacks that access MinIO (validator, relayer) creates a k8s Service named `hyperlane-minio` pointing to the MinIO deployment's pod. This decouples compose files from deployment IDs.
