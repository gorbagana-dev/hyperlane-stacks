# Hyperlane SVM Bridge: Architecture Decisions

Decisions made during planning for the v1 laconic-so stacks. These inform the implementation spec.

---

## Stack Decomposition

**5 stacks:**

| Stack | Type | Purpose |
|-------|------|---------|
| `hyperlane-svm-deployer` | One-time job | Deploys Hyperlane core contracts, configures IGP + Multisig ISM. Outputs deployment artifacts as k8s ConfigMaps/Secrets. |
| `hyperlane-svm-warp-deployer` | One-time job | Deploys warp route contracts for a specific token pair. Separate from core deployer to allow multiple warp routes against the same core deployment. |
| `hyperlane-svm-agents` | Long-running | Runs Hyperlane validators (one per chain) with KMS proxy sidecars for Privy signing, relayer, MinIO (S3-compatible checkpoint storage), gas oracle CronJob, Prometheus, and Grafana (with Hyperlane's pre-built dashboards). Consumes ConfigMaps/Secrets from deployer. |
| `hyperlane-svm-ops` | On-demand jobs | Operational jobs: kill switch, restore, teardown. Requires hardware wallet (operator-attended signing). |
| `hyperlane-warp-ui` | Long-running (optional) | Browser-based bridge UI for token transfers. |

**Rationale:** Separating deployment from runtime allows re-running deployers independently, deploying multiple warp routes, and upgrading agents without redeploying contracts. Ops is separate because it requires the hardware wallet (which agents don't) and has a different lifecycle.

---

## Networking

**Decision:** Public internet, standard k8s egress.

Both chains (Gorchain and Solana) are SVM (Solana Virtual Machine) chains accessed via external RPC endpoints over the public internet. No VPN, host networking, or special network configuration required. Pods use standard Kubernetes egress.

---

## Build Strategy

### Container Images

Three categories of images based on what's available upstream:

#### 1. Agents (validator, relayer) — Use existing upstream image

**Image:** `gcr.io/abacus-labs-dev/hyperlane-agent:agents-v2.0.0`

The Hyperlane team publishes pre-built agent images. No custom build needed for validators and relayer. Pin to tag `agents-v2.0.0`.

#### 2. Sealevel tools (deployer, warp-deployer, ops) — Custom build required

**No existing image.** Must build from hyperlane-monorepo at tag `agents-v2.0.0`.

- Base image: Ubuntu 22.04 (newer Hyperlane releases use a Solana CLI version that doesn't have the libssl1.1 dependency)
- Source: hyperlane-monorepo at tag `agents-v2.0.0`
- Multi-stage Docker build: builder stage compiles, runtime stage copies binaries
- Produces: `hyperlane-sealevel-client` binary + `.so` program files (mailbox, IGP, ISM, validator announce, token, token-native, token-collateral)
- Also includes: `solana-verify` CLI for post-deploy program hash verification (see `supply-chain-security.md`)
- **No patches applied at build time.** The `localnet5.patch` from hyperlane-demo only contains runtime configuration files (agent-config.json, gas-oracle-configs.json, multisig-config.json, metadata.yaml, token-config.json) — all with placeholder values. These are injected at runtime via k8s ConfigMaps.

**Rationale:** Build-time compilation produces a self-contained image with fast startup. Runtime compilation would add 20+ minutes to every container start.

#### 3. Warp UI — Custom build required (build with placeholders, inject at runtime)

**No existing image.** Built from https://github.com/hyperlane-xyz/hyperlane-warp-ui-template (not the monorepo).

Next.js inlines `NEXT_PUBLIC_*` environment variables and YAML configs at `pnpm build` time. To avoid a slow runtime build, we build the full app at image build time using **placeholder sentinel values**, then substitute them with real values at container start.

**Docker image build time (full build):**
- Clone `hyperlane-warp-ui-template` repo at commit `6227c04350c27c208c5512ef40776f8181ab022a` (HEAD as of planning). **Note:** Compatibility with `agents-v2.0.0` contracts and exact commit selection to be verified during implementation.
- Apply `SolanaWalletContext.tsx` code change (exact patch to be determined during implementation):
  - Modify wallet context to read RPC URLs from environment variables instead of hardcoded localhost URLs
  - Remove dependency on `@solana/web3.js` default cluster endpoints
- Install dependencies (`pnpm install`)
- Create placeholder config files:
  - `chains.yaml` with sentinel values (e.g., `__GORCHAIN_RPC_URL__`, `__SOLANA_RPC_URL__`)
  - `warpRoutes.yaml` with sentinel values (e.g., `__WARP_COLLATERAL_ADDRESS__`, `__WARP_SYNTHETIC_ADDRESS__`)
  - `.env` with sentinel `NEXT_PUBLIC_*` variables
- Run `pnpm build` — produces the full Next.js build with sentinels baked into the JS bundles

**Container runtime (entrypoint.sh):**
- Read actual values from environment variables and mounted ConfigMaps
- String-replace all sentinel placeholders in the built `.next/` output:
  ```bash
  # Example: replace sentinels in all JS bundles
  find /app/.next -type f -name '*.js' -exec sed -i \
    -e "s|__GORCHAIN_RPC_URL__|${GORCHAIN_RPC_URL}|g" \
    -e "s|__SOLANA_RPC_URL__|${SOLANA_RPC_URL}|g" \
    -e "s|__GORCHAIN_DOMAIN_ID__|${GORCHAIN_DOMAIN_ID}|g" \
    -e "s|__SOLANA_DOMAIN_ID__|${SOLANA_DOMAIN_ID}|g" \
    -e "s|__WARP_COLLATERAL_ADDRESS__|${WARP_COLLATERAL_ADDRESS}|g" \
    -e "s|__WARP_SYNTHETIC_ADDRESS__|${WARP_SYNTHETIC_ADDRESS}|g" \
    {} +
  ```
- Also replace sentinels in `chains.yaml` and `warpRoutes.yaml` if served as static assets
- Start the Next.js server

**Sentinel variables (to be finalized during implementation):**

| Sentinel | Replaced with | Source |
|----------|--------------|--------|
| `__GORCHAIN_RPC_URL__` | Gorchain RPC endpoint | Env var |
| `__SOLANA_RPC_URL__` | Solana RPC endpoint | Env var |
| `__GORCHAIN_DOMAIN_ID__` | Gorchain domain ID | Env var |
| `__SOLANA_DOMAIN_ID__` | Solana domain ID | Env var |
| `__GORCHAIN_CHAIN_NAME__` | Gorchain chain name | Env var |
| `__SOLANA_CHAIN_NAME__` | Solana chain name | Env var |
| `__WARP_COLLATERAL_ADDRESS__` | Collateral token program ID | ConfigMap (from warp deployer) |
| `__WARP_SYNTHETIC_ADDRESS__` | Synthetic token program ID | ConfigMap (from warp deployer) |
| `__WALLETCONNECT_PROJECT_ID__` | WalletConnect project ID | Env var / Secret |

**Rationale:** Full build at image time means instant startup. The `sed` replacement in entrypoint.sh takes seconds. This is a standard pattern for deploying Next.js apps in Docker with environment-specific config.

### Version Pinning

All images use the same Hyperlane version: **`agents-v2.0.0`**

**Registry:** `git.vdb.to/laconicnetwork` (private Gitea registry)

| Component | Image | Source |
|-----------|-------|--------|
| Validator | `gcr.io/abacus-labs-dev/hyperlane-agent@sha256:<digest>` (tag: `agents-v2.0.0`) | Upstream pre-built, pinned by digest (see `versions.json`) |
| Relayer | `gcr.io/abacus-labs-dev/hyperlane-agent@sha256:<digest>` (tag: `agents-v2.0.0`) | Upstream pre-built, pinned by digest (see `versions.json`) |
| Deployer | `git.vdb.to/laconic/hyperlane-svm-deployer:local` | Custom build from `agents-v2.0.0` tag |
| Warp Deployer | `git.vdb.to/laconic/hyperlane-svm-deployer:local` | Same image as deployer |
| Ops jobs | `git.vdb.to/laconic/hyperlane-svm-deployer:local` | Same image as deployer (has sealevel-client) |
| KMS Proxy | `git.vdb.to/laconicnetwork/hyperlane-kms-proxy:local` | Custom build — Privy-to-AWS-KMS shim for validator signing |
| Gas Oracle | `git.vdb.to/laconicnetwork/hyperlane-gas-oracle:local` | Custom build — fetches prices, signs via Privy Solana wallet |
| Warp UI | `git.vdb.to/laconicnetwork/hyperlane-warp-ui:local` | Custom build from `hyperlane-warp-ui-template` @ `6227c04` |
| MinIO | `minio/minio` (upstream) | S3-compatible checkpoint storage |
| Prometheus | `prom/prometheus` (upstream) | Metrics collection |
| Grafana | `grafana/grafana` (upstream) | Dashboards and alerting |

---

## Key Management

**Decision:** Three-tier key management with escalating security.

### Tier 1: Hardware Wallet — Program Owner (highest security)

The **program owner key** is held on a hardware wallet and controls all post-deployment administrative operations. It is the ultimate authority over the bridge.

- **Format:** Solana pubkey (the hardware wallet address)
- **Provided as:** `HARDWARE_WALLET_PUBKEY` env var (public key only — private key never leaves the hardware wallet)
- **Used for:** Kill switch (ISM reconfiguration), program upgrades, ownership transfers, teardown
- **Signing:** Operator-attended. The `hyperlane-svm-ops` stack generates unsigned transactions; operator signs on the hardware wallet.

**Ownership transfer flow:**
1. Deployer job deploys all contracts using the hot deployer key (Tier 3)
2. As its final step, deployer transfers ownership as follows:
   - **Mailbox, ISM, Validator Announce, Token Collateral, Token Native/Synthetic** on both chains → `HARDWARE_WALLET_PUBKEY`
   - **IGP account** on both chains → Privy oracle wallet (Tier 2) to enable automated gas oracle updates
3. The `hyperlane-svm-ops` stack includes a verification job that confirms all program ownerships are set correctly
4. Hot deployer key is discarded

**Note:** The Solana program upgrade authority for ALL programs (including IGP) is transferred to the hardware wallet. The IGP account-level `owner` field (which controls `SetGasOracleConfigs`, `SetIgpBeneficiary`, `TransferIgpOwnership`) is separate from the program upgrade authority. If the oracle key is compromised, the hardware wallet can upgrade the IGP program to forcibly reset the account owner.

### Tier 2: Privy Server Wallets — Validator & Oracle Keys (medium security)

**Validator keys** and the **IGP oracle key** are managed by [Privy server wallets](https://docs.privy.io/guide/server-wallets/usage/solana).

#### Validator Keys

- **Format:** secp256k1 keys (Ethereum-style, used for Hyperlane checkpoint signing)
- **Managed by:** Privy server wallet API
- **Used for:** Signing merkle root checkpoints (continuous, every few seconds)
- **Signing:** Automated via Privy API. ~175ms latency per signature. **TODO: Benchmark under checkpoint workload to verify acceptable throughput.**

#### IGP Oracle Key

- **Format:** Solana keypair (Ed25519)
- **Managed by:** Privy server wallet API
- **Used for:** `SetGasOracleConfigs` on both chains (periodic, automated)
- **Signing:** Automated via Privy API
- **Risk:** Oracle key is the IGP account owner, so it also has access to `SetIgpBeneficiary` and `TransferIgpOwnership`. Privy policy engine mitigates this (see below).

**Privy policy engine configuration:**
- **Validator wallets:** Allowlist specific program IDs (only Hyperlane contracts), cap SOL transfer amounts, restrict to known contract interactions
- **Oracle wallet:** Restrict to `SetGasOracleConfigs` instruction only — block `SetIgpBeneficiary` and `TransferIgpOwnership` calls via Privy policy rules. This prevents a compromised oracle adapter from escalating access.
- **Approval quorum:** m-of-n approval required for high-risk operations

**Why Privy for validators and oracle:**
- Validator key compromise in 1-of-1 multisig = bridge security broken in one direction
- Oracle key compromise = can redirect IGP fees and lock out operator (mitigated by Privy policies and program upgrade authority on hardware wallet)
- Privy provides TEE-backed key custody without exposing raw keys
- Policy engine adds defense-in-depth (restrict what each key can sign)
- Keys are never in pod memory — signing happens remotely

**Integration:** See "Privy Integration Architecture" section below for the KMS proxy (validators) and oracle service designs.

### Tier 3: Environment Variable — Hot Keys (lowest security)

**Relayer key** and **hot deployer key** are injected as env vars / k8s Secrets.

| Key | Format | Used by | Lifecycle |
|-----|--------|---------|-----------|
| Hot deployer keypair | Solana keypair JSON | Deployer job | **Ephemeral** — used for initial deployment only, discarded after ownership transfer to hardware wallet |
| Relayer key | Hex private key (0x...) | Relayer | Long-lived, in pod as k8s Secret |

**Why env vars for relayer:** Relayer key compromise is medium-impact (can't forge messages, only disrupt delivery and drain relayer wallet). The operational simplicity of env var injection outweighs the security benefit of Privy for this key.

**Why env var for hot deployer:** The deployer signs ~2000+ transactions during initial deployment (program buffer writes, deploys, configuration). Hardware wallet signing at that volume is impractical. The hot key is only alive during the deploy window, then ownership is transferred and the hot key is destroyed.

### Key Inventory

| Key | Security Tier | Storage | Signing Method | Compromise Impact |
|-----|--------------|---------|---------------|-------------------|
| Program owner | Tier 1 (HW wallet) | Hardware wallet | Operator-attended | **CRITICAL** — full bridge control (program upgrades, kill switch, teardown) |
| Gorchain validator | Tier 2 (Privy) | Privy server wallet | Privy API (automated) | **HIGH** — forge checkpoints in one direction |
| Solana validator | Tier 2 (Privy) | Privy server wallet | Privy API (automated) | **HIGH** — forge checkpoints in one direction |
| IGP oracle | Tier 2 (Privy) | Privy server wallet | Privy API (automated) | **MEDIUM** — bad gas prices, redirect fees (mitigated by Privy policy engine; recoverable via program upgrade) |
| Hot deployer | Tier 3 (env var) | k8s Secret | In-process (automated) | **CRITICAL but ephemeral** — only during deploy window |
| Relayer | Tier 3 (env var) | k8s Secret | In-process (automated) | **MEDIUM** — disrupt delivery, drain wallet |

### Funding

- **Hot deployer keypair:** Must be pre-funded externally on both chains before deployment. Needs ~30+ SOL per chain upfront (includes recoverable rent deposits for program accounts; net non-recoverable cost is ~5-6 SOL per chain).
- **Hardware wallet:** Does not need funding (only signs authority transactions, fees paid by other keys or can be funded minimally for post-deploy ops).
- **Validator keys (Privy):** Must be funded on their respective chains for validator announce transactions. Fund the Privy wallet's Solana address (derived from the secp256k1 key via Ed25519 conversion).
- **IGP oracle key (Privy):** Must be funded on both chains for `SetGasOracleConfigs` transaction fees (minimal — a few transactions per day).
- **Relayer key:** Must be pre-funded on both chains for message delivery transaction fees.

### Privy Integration Architecture

Two custom components are needed: a KMS proxy for validators and a standalone oracle service.

#### Validator: AWS KMS API Proxy

The Hyperlane validator at `agents-v2.0.0` has no signer plugin interface. The `Signers` enum supports only two backends: local hex key (`LocalWallet`) and AWS KMS (`AwsSigner`). Checkpoint signing is exclusively **secp256k1 (Ethereum ECDSA)** using EIP-191 message hashing.

**Approach:** Exploit the existing AWS KMS signer path with a local KMS API proxy that redirects to Privy.

```
┌─────────────────┐  AWS KMS API   ┌──────────────┐  Privy API   ┌─────────┐
│  Validator       │ ────────────→ │  KMS Proxy   │ ───────────→ │  Privy  │
│  (unmodified)    │               │  (sidecar)   │              │  (TEE)  │
│  type: "aws"     │               │  port 9999   │              │         │
└─────────────────┘                └──────────────┘              └─────────┘
```

**KMS proxy sidecar** — a lightweight service (~200 lines) that:
1. Implements the AWS KMS API endpoints used by the Hyperlane `AwsSigner`:
   - `Sign` — receives signing request, forwards to Privy's `signMessage` (secp256k1/EVM wallet), returns signature
   - `GetPublicKey` — returns the Privy wallet's secp256k1 public key in AWS KMS response format
   - `DescribeKey` — returns key metadata (key type: ECC_SECG_P256K1)
2. Authenticates to Privy using API credentials from a k8s Secret
3. Maps AWS KMS key IDs to Privy wallet IDs via configuration

**Validator configuration:**

Note: `type: "aws"` applies only to the **signer** (checkpoint signing key). Checkpoint storage and all other data use `localStorage` on PVCs as defined in the "Checkpoint Storage" section. These are independent configuration paths.

```json
{
  "validator": {
    "type": "aws",
    "id": "<privy-wallet-id-mapped-as-kms-key-id>",
    "region": "us-east-1"
  },
  "checkpointSyncer": {
    "type": "s3",
    "bucket": "hyperlane-validator-gorchain",
    "region": "us-east-1"
  }
}
```

**Environment:**
```
AWS_ENDPOINT_URL_KMS=http://localhost:9999   # KMS proxy sidecar → Privy
AWS_ENDPOINT_URL_S3=http://minio:9000        # MinIO checkpoint storage
AWS_ACCESS_KEY_ID=<minio-access-key>
AWS_SECRET_ACCESS_KEY=<minio-secret-key>
```

The AWS SDK for Rust (v0.56+) supports per-service endpoint overrides via `AWS_ENDPOINT_URL_<SERVICE>` environment variables. These take priority over the global `AWS_ENDPOINT_URL`. The KMS proxy sidecar authenticates to Privy independently — the AWS credentials are used by MinIO only.

**Advantages:**
- Validator binary is completely unmodified — no fork to maintain across Hyperlane upgrades
- The KMS proxy is stateless and independently testable
- Same proxy can serve both validator pods (one per chain)

**Risks and mitigations:**
- **AWS SDK compatibility:** The AWS SDK for Rust supports per-service endpoint overrides (`AWS_ENDPOINT_URL_KMS`) since v0.56+. Verify the SDK version bundled in the `agents-v2.0.0` tag is recent enough, or that the Hyperlane `AwsSigner` uses standard SDK configuration loading.
- **Response format fidelity:** The proxy must return signatures in the exact DER-encoded format AWS KMS uses. The Hyperlane `AwsSigner` likely parses the DER and extracts `(r, s)` — the proxy must match this.
- **Latency:** Each checkpoint signing adds a network hop (proxy → Privy). At ~175ms Privy latency + local proxy overhead, total should be <200ms — well within the checkpoint interval.

#### Oracle: Standalone Gas Oracle Service

The gas oracle is a new standalone service (CronJob in agents stack) that:
1. Fetches token prices from configured price feeds (e.g., CoinGecko, on-chain oracles)
2. Computes `GasOracleConfig` values (token exchange rate, gas price per destination domain)
3. Builds `SetGasOracleConfigs` transactions for both chains
4. Signs and submits via Privy's **Solana wallet API** (Ed25519 — this is a Solana transaction, not a checkpoint)

**No proxy needed** — the oracle is our own service, so it calls Privy directly.

**Configuration:**
- Privy wallet ID and API credentials (k8s Secret)
- Price feed URLs and update interval
- Sanity check thresholds (reject updates deviating >X% from previous value)
- Both chain RPC URLs and IGP program IDs

---

## Multisig ISM Configuration

**Decision:** Single validator (1-of-1) per chain for v1.

- Each chain has one validator whose address is configured in the other chain's Multisig ISM
- Threshold = 1
- Configurable via env vars (validator addresses)

**Deferred:** m-of-n validator sets, geographic distribution, validator rotation procedures.

---

## Gas Economics

### Gas Enforcement

**Decision:** `None` enforcement policy on the relayer.

The Sealevel `process_estimate_costs()` function returns hardcoded zeros (upstream TODO in `mailbox.rs:539`), making `OnChainFeeQuoting` non-functional. Using `None` policy accepts this and does not require gas payment for message delivery.

**Risk:** Without enforcement, the relayer subsidizes all message delivery. This is a known DoS vector in production. Acceptable for v1 given controlled deployment scope.

### Gas Oracle

**Decision:** Automated gas oracle update CronJob in the `hyperlane-svm-agents` stack.

The Sealevel IGP's `set_gas_oracle_configs` instruction requires the IGP account owner's signature (no separate oracle role exists). IGP account ownership is transferred to a dedicated Privy oracle wallet (Tier 2) at deploy time, enabling fully automated updates without operator attendance.

- A CronJob in the agents stack fetches current token prices and submits `SetGasOracleConfigs` transactions signed via Privy API
- Configurable update frequency
- Static fallback values configured at deploy time if price feed is unavailable
- Privy policy engine restricts the oracle wallet to `SetGasOracleConfigs` only — blocks `SetIgpBeneficiary` and `TransferIgpOwnership`

**Note:** Despite using `None` enforcement, the gas oracle should still be configured correctly for accurate fee quoting to users.

---

## Emergency Controls

**Decision:** Full on-chain kill switch + restore (via `hyperlane-svm-ops` stack).

Two k8s Job templates in the ops stack:

1. **Kill job:** Scales agent deployments to 0, then reconfigures Multisig ISM on both destination chains to the null validator address (`0x0000000000000000000000000000000000000000`). Validator addresses in the Multisig ISM use H160 (20-byte Ethereum-style) format, even on Sealevel chains. This makes it impossible for any relayer (including third-party) to deliver messages.

2. **Restore job:** Reconfigures ISM back to the real validator addresses and scales agents back up. Messages dispatched during the pause will be delivered.

Both jobs require the ISM owner key (hardware wallet). Jobs generate unsigned transactions; the operator signs on the hardware wallet. See `docs/ops-decisions.md` for full details.

**Supersedes** the earlier "relayer kill switch" approach — stopping the relayer alone is insufficient because a third-party relayer could still deliver messages using cached validator signatures.

---

## Process Management

**Decision:** Standard Kubernetes liveness/readiness probes, `restartPolicy: Always`.

- Liveness probes: HTTP health endpoint or TCP check on metrics port
- Readiness probes: Same mechanism
- Resource limits: TBD during implementation (CPU, memory, disk)
- Automatic restart on crash with k8s default backoff

---

## High Availability

**Decision:** Single instance per agent, with RPC failover support.

- One validator per chain, one relayer
- Support comma-separated RPC URLs for failover (if supported by Hyperlane agents)
- No relayer redundancy, no validator replication in v1

**Deferred:** Active-passive relayer, multiple validators per chain, geographic distribution.

---

## Network Security

### Consumed RPCs (Gorchain/Solana endpoints)

**Decision:** No validation. Accept whatever URLs the user provides.

User is responsible for the security of their RPC endpoints (TLS, authentication, rate limiting).

### Served Endpoints (metrics, health checks)

**Decision:** Expose via k8s Ingress with TLS termination.

- Validator metrics (port 9090) and relayer metrics (port 9091) exposed via Ingress
- TLS termination via cert-manager or similar (part of the Kind cluster config)
- Access control via Ingress annotations or network policies

---

## Backup & Recovery

**Decision:** PVC snapshots, user-managed.

- Validator and relayer state (RocksDB) stored on per-pod RWO PersistentVolumeClaims
- Checkpoint signatures stored in MinIO (S3-compatible) — already durable via MinIO's own PVC
- Backup via Kubernetes VolumeSnapshot (user configures snapshot schedule via StorageClass)
- No in-stack backup jobs or CronJobs

**Deferred:** External S3 backup replication, automated recovery procedures, RTO/RPO definitions.

---

## Token Supply Management

**Decision:** Configurable per-transaction and per-day caps.

- Environment variables for maximum bridge amount per transaction and per time window
- Enforcement mechanism TBD (relayer-level or requires custom contract logic)

**Deferred:** Proof-of-reserves, collateral verification, allowlist/blocklist.

---

## Chain Configuration

**Decision:** Resolved. Full schema documented.

The `agent-config.json` schema for Sealevel chains requires the following fields per chain:

**Required fields:**

| Field | Description | Example |
|-------|-------------|---------|
| `name` | Chain name (must match key) | `"gorchain"` |
| `chainId` | Unique chain identifier | `99999` |
| `domainId` | Hyperlane domain ID | `99999` |
| `protocol` | Must be `"sealevel"` | `"sealevel"` |
| `mailbox` | Mailbox program address (base58) | Populated by deployer |
| `interchainGasPaymaster` | IGP program address (base58) | Populated by deployer |
| `validatorAnnounce` | Validator announce program address (base58) | Populated by deployer |
| `merkleTreeHook` | Merkle tree hook program address (base58) | Populated by deployer |
| `rpcUrls[].http` | HTTP RPC endpoint URL | `"https://gorchain.example.com"` |

**Optional fields (sensible defaults):**

| Field | Default | Notes |
|-------|---------|-------|
| `blocks.estimateBlockTime` | `0.4` | Seconds per block/slot |
| `blocks.reorgPeriod` | `0` | Sealevel has deterministic finality |
| `index.from` | `0` | Starting slot for indexing |
| `index.chunk` | `10000` | Slots per query batch |
| `index.mode` | `"sequence"` | Slot-based indexing (auto for Sealevel) |
| `nativeToken.decimals` | `9` | SOL/GOR decimals |
| `priorityFeeOracle` | `Constant(0)` | Can use `"helius"` type for dynamic fees |
| `transactionSubmitter` | `Rpc` | Can use `"jito"` for MEV-protected submission |

**Not configurable:**
- **Commitment level:** hardcoded to `finalized` in the Sealevel agent — not a config option
- **WebSocket URL:** not needed — agent uses HTTP RPC only

---

## Checkpoint Storage

**Decision:** S3-compatible object storage (MinIO) deployed as part of the agents stack.

Validators write checkpoint signatures to S3, and the relayer reads from the same bucket. This uses Hyperlane's native `s3` checkpoint syncer type, avoiding the need for shared PVCs (ReadWriteMany) which are not supported on Kind's default StorageClass.

**Architecture:**

```
┌──────────────────┐    S3 API     ┌─────────┐    S3 API     ┌──────────────────┐
│  Validator        │ ───────────→ │  MinIO  │ ←─────────── │  Relayer          │
│  (writes)         │              │  (pod)  │              │  (reads)          │
└──────────────────┘              └─────────┘              └──────────────────┘
```

**MinIO deployment:**
- Single-node MinIO pod in the agents stack with a RWO PVC for data
- Bucket per validator: `hyperlane-validator-gorchain`, `hyperlane-validator-solana`
- Credentials injected via k8s Secret
- Internal ClusterIP service (not exposed externally)

**Validator checkpoint syncer config:**
```json
{
  "checkpointSyncer": {
    "type": "s3",
    "bucket": "hyperlane-validator-gorchain",
    "region": "us-east-1"
  }
}
```

**Environment (validator and relayer pods):**
```
AWS_ENDPOINT_URL=http://minio:9000
AWS_ACCESS_KEY_ID=<minio-access-key>
AWS_SECRET_ACCESS_KEY=<minio-secret-key>
```

**Advantages:**
- No RWX PVC required — MinIO uses a single RWO PVC, validators and relayer connect via S3 API
- Works identically on Kind, cloud k8s, and bare metal
- Native Hyperlane support — `s3` syncer type is first-class, no `--allowLocalCheckpointSyncers` flag needed
- Decouples validator and relayer pod lifecycles completely
- MinIO can be swapped for AWS S3, GCS, or any S3-compatible service in production by changing the endpoint URL

**Storage requirements:**
- Checkpoint data is small (~1 KB per checkpoint) — minimal disk footprint
- MinIO PVC size: 1 Gi is sufficient for extended operation

**Endpoint routing:** The validator pod uses per-service AWS endpoint overrides (`AWS_ENDPOINT_URL_KMS=http://localhost:9999` for the Privy KMS proxy, `AWS_ENDPOINT_URL_S3=http://minio:9000` for MinIO). The AWS SDK for Rust (v0.56+) supports these per-service overrides natively. See the "Privy Integration Architecture" section for the full environment configuration.

---

## Artifact Passing (Deployer → Agents)

**Decision:** Kubernetes ConfigMaps and Secrets.

- Deployer job outputs deployment artifacts as k8s ConfigMaps and Secrets
- Agents stack references these by name
- Enables independent lifecycle management of deployer and agents

**Runtime configuration files (all via ConfigMaps, none baked into images):**

| ConfigMap | Contents | Created by | Consumed by |
|-----------|----------|-----------|-------------|
| `hyperlane-agent-config` | `agent-config.json` — chain definitions, RPC URLs, deployed contract addresses | Deployer (populates after deploy) | Validators, Relayer |
| `hyperlane-gas-oracle-config` | `gas-oracle-configs.json` — token exchange rates, gas prices, overhead | Deployer (initial), Gas oracle service (updates) | Deployer (IGP configure) |
| `hyperlane-multisig-config` | `multisig-config.json` per chain — validator addresses, threshold | Deployer | Deployer (ISM configure) |
| `hyperlane-registry` | `metadata.yaml` — chain registry metadata | Pre-configured | Deployer, CLI tools |
| `hyperlane-token-config` | `token-config.json` — warp route token configuration with contract addresses | Warp deployer (populates after deploy) | Warp deployer |
| `hyperlane-program-ids` | `program-ids.json` per chain — deployed program addresses | Deployer (output) | Agents, Warp deployer, Ops |

These correspond to the files in the `localnet5.patch` from hyperlane-demo, but with actual values populated at deploy time rather than hardcoded localhost URLs.

---

## Warp Route Token

**Decision:** Pre-existing token mint only.

- User provides `WARP_TOKEN_MINT` address of an already-deployed SPL token
- The warp deployer only deploys the warp route contracts (collateral + synthetic)
- No in-stack token creation

---

## Monitoring

**Decision:** In-stack Prometheus + Grafana with Hyperlane's pre-built dashboards.

Hyperlane agents (validators and relayer) natively export Prometheus metrics. The Hyperlane team provides pre-built Grafana dashboards for validator and relayer monitoring.

**Components (in the agents stack):**

| Component | Purpose |
|-----------|---------|
| Prometheus | Scrapes metrics from validator and relayer pods |
| Grafana | Visualization using Hyperlane's pre-built dashboards |

**Metrics sources:**
- Validator metrics endpoints (one per chain)
- Relayer metrics endpoint
- Wallet balance monitor (custom CronJob, emits Prometheus metrics)

**Grafana dashboards:**
- Import Hyperlane's pre-built validator dashboard
- Import Hyperlane's pre-built relayer dashboard
- Custom dashboard for wallet balances and gas oracle status

**Alerting** (via Prometheus Alertmanager or Grafana alerts):
- Validator not signing checkpoints for > N minutes
- Relayer delivery failures
- Wallet balance below threshold
- Agent pod restarts

---

## Logging

**Decision:** JSON to stdout, user brings log collector.

- Agents output structured JSON logs to stdout
- Kubernetes log collection (Fluentd, Loki, CloudWatch, etc.) is the user's responsibility
- No in-stack log aggregation infrastructure

---

## Upgrades

**Decision:** Version pinning only.

- Container image versions pinned in stack configuration
- Upgrade procedure: change version tag → redeploy stack
- No automated rolling upgrades or upgrade scripts

**Deferred:** Rolling upgrade automation, compatibility matrix, rollback procedures.

---

## Deferred Items (v2+)

| Item | Priority | Reason |
|------|----------|--------|
| m-of-n multisig | P0 | Requires multi-validator infrastructure |
| On-chain emergency pause | P0 | Requires contract modifications |
| Upstream gas enforcement fix | P0 | Depends on Hyperlane core team |
| Relayer redundancy | P1 | Adds significant complexity |
| S3 checkpoint storage | P1 | Requires S3 credentials management |
| Automated backup jobs | P1 | PVC snapshots sufficient for v1 |
| Rate limiting (per-address) | P2 | Requires custom contract logic |
| Automated testing suite | P2 | High effort |
| Load/chaos testing | P3 | Not blocking v1 |
| Full supply chain security (reproducible builds, SBOM, sigstore) | P3 | Baseline mitigations (digest pinning, `--locked` builds, `cargo-audit`, post-deploy hash verification) are in v1 scope; see `supply-chain-security.md` |
| KMS key management | P1 | Pre-generated keys sufficient for v1 |
