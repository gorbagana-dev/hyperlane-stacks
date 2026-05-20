# Hyperlane SVM Bridge: Architecture Decisions

Decisions made during planning for the v1 laconic-so stacks. These inform the implementation spec.

---

## Stack Decomposition

**8 stacks** (each stack = one k8s Pod or Job, see `specs/stack-specifications.md` for detailed per-stack specs):

| Stack | Type | Purpose |
|-------|------|---------|
| `hyperlane-svm-deployer` | One-time job | Deploys Hyperlane core contracts, configures IGP + Multisig ISM. Outputs deployment artifacts as k8s ConfigMaps. |
| `hyperlane-svm-warp-deployer` | One-time job | Deploys warp route contracts for a specific token pair. Separate from core deployer to allow multiple warp routes against the same core deployment. |
| `hyperlane-validator` | Long-running | Runs a Hyperlane validator for one chain with KMS proxy sidecar for Privy signing. One deployment per chain. |
| `hyperlane-relayer` | Long-running | Delivers cross-chain messages. Includes IGP fee claim sidecar. |
| `hyperlane-minio` | Long-running | S3-compatible checkpoint storage (MinIO) for validators and relayer. |
| `hyperlane-gas-oracle` | Long-running | Automated IGP gas oracle updates via Privy. |
| `hyperlane-monitoring` | Long-running | Prometheus + Grafana + Pushgateway + balance monitor. |
| `hyperlane-warp-ui` | Long-running (optional) | Browser-based bridge UI for token transfers. |

Operational jobs (kill switch, restore, teardown) live in `ops/` as standalone k8s Job manifests — not an SO-managed stack. They require the hardware wallet for operator-attended signing.

**Rationale:** Stack-orchestrator maps all services in a stack to a single k8s Pod, so services needing independent lifecycles or restart must be separate stacks. Separating deployment from runtime allows re-running deployers independently, deploying multiple warp routes, and upgrading agents without redeploying contracts.

---

## Networking

**Decision:** Public internet, standard k8s egress.

Both chains (Gorchain and Solana) are SVM (Solana Virtual Machine) chains accessed via external RPC endpoints over the public internet. No VPN, host networking, or special network configuration required. Pods use standard Kubernetes egress.

---

## Build Strategy

### Container Images

Three categories of images based on what's available upstream:

#### 1. Agents (validator, relayer) — Custom patched build

**Image:** `ghcr.io/gorbagana-dev/hyperlane-agent:latest`

Custom build from `hyperlane-monorepo` at `agents-v2.2.0` (commit `4da9c44`) with two patches applied at build time:
- **`kms-endpoint.patch`**: Adds `AWS_ENDPOINT_URL_KMS` support so the validator's AWS KMS signer can be redirected to the local KMS proxy sidecar
- **`s3-path-style.patch`**: Forces S3 path-style addressing for MinIO compatibility

The patched agent image is used by both the validator and relayer stacks. Published to the Gitea registry via CI workflow.

#### 2. Sealevel tools (deployer, warp-deployer, ops) — Custom build required

**No existing image.** Must build from hyperlane-monorepo at `@hyperlane-xyz/core@10.2.0` (commit `16c056a09af862b3ce9e14bd3b5b8034750af9d0`).

- Base image: Ubuntu 22.04
- Source: hyperlane-monorepo at commit `16c056a` (`@hyperlane-xyz/core@10.2.0`)
- Multi-stage Docker build: builder stage compiles, runtime stage copies binaries
- Produces: `hyperlane-sealevel-client` binary + `.so` program files (mailbox, IGP, ISM, validator announce, token, token-native, token-collateral)
- Also includes: `solana-verify` CLI for post-deploy program hash verification (see `supply-chain-security.md`), Solana CLI 3.0.14
- **Solana CLI 3.x note:** Program deployment ownership transfer requires the `--skip-new-upgrade-authority-signer-check` flag (added in Solana CLI 2.x+), which bypasses the requirement for the new authority to co-sign the transfer
- **No patches applied at build time.** The `localnet5.patch` from hyperlane-demo only contains runtime configuration files (agent-config.json, gas-oracle-configs.json, multisig-config.json, metadata.yaml, token-config.json) — all with placeholder values. These are injected at runtime via k8s ConfigMaps.

**Rationale:** Build-time compilation produces a self-contained image with fast startup. Runtime compilation would add 20+ minutes to every container start.

#### 3. Warp UI — Custom build required (build with placeholders, inject at runtime)

**No existing image.** Built from https://github.com/hyperlane-xyz/hyperlane-warp-ui-template at tag `v2.0.0` (not the monorepo). Uses `@hyperlane-xyz/sdk@28.0.0`.

Next.js inlines `NEXT_PUBLIC_*` environment variables and YAML configs at `pnpm build` time. To avoid a slow runtime build, we build the full app at image build time using **placeholder sentinel values**, then substitute them with real values at container start.

**Docker image build time (full build):**
- Clone `hyperlane-warp-ui-template` repo at tag `v2.0.0`
- Apply `SolanaWalletContext.tsx` patch: the default wallet context uses `clusterApiUrl(WalletAdapterNetwork.Mainnet)` which hardcodes the connection to Solana mainnet RPC. The patch replaces this with sentinel RPC URLs (`__GORCHAIN_RPC_URL__`, `__SOLANA_RPC_URL__`) so the wallet adapter connects to our custom chains. The sentinels are compiled into the JS bundle and replaced at container start by `entrypoint.sh`, same as all other config values. See `hyperlane-demo/patches/warp-ui.patch` for the original localhost version.
- Install dependencies (`pnpm install`)
- Create placeholder config files with sentinel values:
  - `chains.yaml` — chain metadata including `mailbox` addresses (required for SVM token adapters)
  - `warpRoutes.yaml` — warp route token connections with `mailbox` per token
  - `.env` with sentinel `NEXT_PUBLIC_*` variables
- Run `pnpm build` — produces the full Next.js build with sentinels baked into the JS bundles

**Container runtime (entrypoint.sh):**
- Read actual values from environment variables
- String-replace all sentinel placeholders in the built `.next/` output and `/app/public/` static assets
- Start the Next.js server

**No ConfigMap mount.** Config is fully baked into the JS bundles at build time and substituted at container start via `sed` on the compiled output. There is no runtime config file mount — environment variables are the sole configuration interface.

**Sentinel variables:**

| Sentinel | Replaced with | Source |
|----------|--------------|--------|
| `__GORCHAIN_RPC_URL__` | Gorchain RPC endpoint | Env var |
| `__SOLANA_RPC_URL__` | Solana RPC endpoint | Env var |
| `__GORCHAIN_DOMAIN_ID__` | Gorchain domain ID | Env var |
| `__SOLANA_DOMAIN_ID__` | Solana domain ID | Env var |
| `__GORCHAIN_CHAIN_ID__` | Gorchain chain ID | Env var |
| `__SOLANA_CHAIN_ID__` | Solana chain ID | Env var |
| `__GORCHAIN_CHAIN_NAME__` | Gorchain chain name | Env var |
| `__SOLANA_CHAIN_NAME__` | Solana chain name | Env var |
| `__GORCHAIN_MAILBOX__` | Gorchain mailbox program address | Env var (from deployer ConfigMap) |
| `__SOLANA_MAILBOX__` | Solana mailbox program address | Env var (from deployer ConfigMap) |
| `__WARP_COLLATERAL_ADDRESS__` | Collateral token program ID | Env var (from warp deployer) |
| `__WARP_SYNTHETIC_ADDRESS__` | Synthetic token program ID | Env var (from warp deployer) |
| `__NEXT_PUBLIC_WALLET_CONNECT_ID__` | WalletConnect project ID | Env var / Secret |
| `__GORCHAIN_NATIVE_TOKEN_*__` | Native token name/symbol/decimals | Env var (defaults: GOR/GOR/9) |
| `__SOLANA_NATIVE_TOKEN_*__` | Native token name/symbol/decimals | Env var (defaults: SOL/SOL/9) |

**Rationale:** Full build at image time means instant startup. The `sed` replacement in entrypoint.sh takes seconds. This is a standard pattern for deploying Next.js apps in Docker with environment-specific config.

### Version Pinning

Deployer image uses **`@hyperlane-xyz/core@10.2.0`** (commit `16c056a09af862b3ce9e14bd3b5b8034750af9d0`) with Solana CLI **3.0.14**. Agent images (validator, relayer) use a **custom patched build** from `agents-v2.2.0` (commit `4da9c44`) with KMS endpoint and S3 path-style patches.

**Registry:** `ghcr.io/gorbagana-dev` (GitHub Container Registry)

| Component | Image | Source |
|-----------|-------|--------|
| Validator | `ghcr.io/gorbagana-dev/hyperlane-agent:latest` | Custom patched build from `agents-v2.2.0` (commit `4da9c44`) |
| Relayer | `ghcr.io/gorbagana-dev/hyperlane-agent:latest` | Same image as validator |
| Deployer | `ghcr.io/gorbagana-dev/hyperlane-svm-deployer:local` | Custom build from `@hyperlane-xyz/core@10.2.0` (commit `16c056a`), Solana CLI 3.0.14 |
| Warp Deployer | `ghcr.io/gorbagana-dev/hyperlane-svm-deployer:local` | Same image as deployer |
| Ops jobs | `ghcr.io/gorbagana-dev/hyperlane-svm-deployer:local` | Same image as deployer (has sealevel-client) |
| KMS Proxy | `ghcr.io/gorbagana-dev/hyperlane-kms-proxy:local` | Custom build — Privy-to-AWS-KMS shim for validator signing |
| Gas Oracle | `ghcr.io/gorbagana-dev/hyperlane-gas-oracle:local` | Custom build — fetches prices, signs via Privy Solana wallet |
| Warp UI | `ghcr.io/gorbagana-dev/hyperlane-warp-ui:local` | Custom build from `hyperlane-warp-ui-template` @ `v2.0.0` (`@hyperlane-xyz/sdk@28.0.0`) |
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

- **Hot deployer keypair:** Must be pre-funded externally on both chains before deployment. Needs **~50+ SOL per chain** upfront. Each program deploy requires ~1.3 SOL rent for the program account plus a ~1.3 SOL buffer account, and the deployer deploys 7 programs per chain (mailbox, validator_announce, IGP, multisig_ism, token, token_collateral, token_native). The CLI retries failed deploys with increasing compute unit prices, which adds fees. Most rent is recoverable if programs are later closed (see teardown). Net non-recoverable cost is ~5-6 SOL per chain in transaction fees.
  - **TODO (production):** On real chains, the deployer must be funded manually before running the deployer stack. The deploy spec should document the exact funding amount needed and include a pre-flight balance check in the deploy script that fails early with a clear message if the deployer balance is insufficient. Consider splitting deploys across multiple transactions with balance checks between programs.
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

The gas oracle is a standalone TypeScript service in the `hyperlane-gas-oracle` stack that:
1. Fetches sGOR and SOL token prices from CoinGecko (or configurable endpoint)
2. Converts sGOR price to gGOR (Gorchain native token) via configurable multiplier (default ×100, since 1 gGOR = 100 sGOR)
3. Computes `GasOracleConfig` values using `@hyperlane-xyz/sdk` `getLocalStorageGasOracleConfig()` — handles 1e19 Sealevel exchange rate scaling, margin, and gas price conversion
4. Builds `SetGasOracleConfigs` instructions using SDK Borsh serialization (correct discriminator, account ordering, and Option/enum wrappers)
5. Signs via Privy `signTransaction` (sign-only), then submits to the configured chain RPC — giving the oracle full control over which RPC receives the transaction
6. Skips updates when `PRICE_FEED_URL` is not set (empty string)

**No proxy needed** — the oracle is our own service, so it calls Privy directly. Uses the sign-only `signTransaction` endpoint (not `signAndSendTransaction`), so the oracle controls which RPC the tx is submitted to — critical for custom chains like Gorchain that aren't in Privy's CAIP-2 registry.

**Two signer modes:**
- `SIGNER_MODE=privy` (default): Privy server wallet for production. Uses `PRIVY_API_URL` (default: `https://auth.privy.io/api/v1`, overridable for mock server in E2E tests).
- `SIGNER_MODE=keypair`: Local Solana keypair for lightweight testing

**Configuration:**
- Privy wallet ID and API credentials (k8s Secret) — or keypair JSON for testing
- Price feed URL (`PRICE_FEED_URL`) and update interval — empty URL skips oracle updates entirely
- Gas price (default: 0.000000001 SOL), overhead (default: 200000 CU), margin (default: 10%), min USD cost floor (default: $0.50)
- Sanity check thresholds (reject updates deviating >50% from previous value)
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

**Decision:** Automated gas oracle update service in the `hyperlane-gas-oracle` stack.

The Sealevel IGP's `set_gas_oracle_configs` instruction requires the IGP account owner's signature (no separate oracle role exists). IGP account ownership is transferred to a dedicated Privy oracle wallet (Tier 2) at deploy time, enabling fully automated updates without operator attendance.

- The `hyperlane-gas-oracle` stack runs a long-running TypeScript service that fetches current token prices and submits `SetGasOracleConfigs` transactions (Privy sign-only or local keypair signing)
- Configurable update frequency (default: 15 min)
- If `PRICE_FEED_URL` is empty, the oracle skips updates — the deployer's initial `gas-oracle-configs.json` values remain in effect
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

**Decision:** Two process management modes based on workload type.

**Long-running agents (validators, relayer, gas oracle, monitoring):** Standard Kubernetes Deployments with `restartPolicy: Always`.

- Liveness probes: HTTP health endpoint or TCP check on metrics port
- Readiness probes: Same mechanism
- Resource limits: TBD during implementation (CPU, memory, disk)
- Automatic restart on crash with k8s default backoff

**One-shot deployers (svm-deployer, warp-deployer):** Kubernetes Jobs with `restartPolicy: Never` and `backoffLimit: 0`.

- Deployers are run-once containers that exit on completion — using Deployments caused CrashLoopBackOff as k8s tried to restart completed containers
- stack.yml uses `jobs:` instead of `pods:` for deployer stacks; compose files live in `compose-jobs/` instead of `compose/`
- laconic-so supports k8s Jobs in the k8s-kind path via BatchV1Api (`_create_jobs()`)
- Deploy scripts are mounted via ConfigMap volumes at `/opt/scripts/` rather than baked into the Docker image, allowing script iteration without image rebuilds
- E2E tests use `kubectl wait --for=condition=complete job/<name>` instead of polling container exit codes

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

**Decision:** S3-compatible object storage (MinIO) deployed as its own `hyperlane-minio` stack.

Validators write checkpoint signatures to S3, and the relayer reads from the same bucket. This uses Hyperlane's native `s3` checkpoint syncer type, avoiding the need for shared PVCs (ReadWriteMany) which are not supported on Kind's default StorageClass.

**Architecture:**

```
┌──────────────────┐    S3 API     ┌─────────┐    S3 API     ┌──────────────────┐
│  Validator        │ ───────────→ │  MinIO  │ ←─────────── │  Relayer          │
│  (writes)         │              │  (pod)  │              │  (reads)          │
└──────────────────┘              └─────────┘              └──────────────────┘
```

**MinIO deployment:**
- Single-node MinIO pod in the `hyperlane-minio` stack with a RWO PVC for data
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
AWS_ENDPOINT_URL_S3=http://hyperlane-minio:9000
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
- MinIO PVC size: 10 Gi (see `deployment/spec-minio.yml`); checkpoint data is small so this is sufficient for extended operation

**Endpoint routing:** The validator pod uses per-service AWS endpoint overrides (`AWS_ENDPOINT_URL_KMS=http://localhost:9999` for the Privy KMS proxy, `AWS_ENDPOINT_URL_S3=http://minio:9000` for MinIO). The AWS SDK for Rust (v0.56+) supports these per-service overrides natively. See the "Privy Integration Architecture" section for the full environment configuration.

---

## Artifact Passing (Deployer → Agents)

**Decision (2026-05-20, supersedes earlier "ConfigMaps and Secrets" decision):** Deployer Jobs write JSON state files to a host-path bind mount; an out-of-cluster loader (pytest fixture in dev, ansible in prod) copies the relevant files into each consumer stack's `{deploy_dir}/configmaps/<cm-name>/` before `deployment start`; SO creates plain ConfigMaps in each consumer's own namespace and pods mount them as normal volumes.

**Earlier model (deprecated):** Deployer Jobs `kubectl create configmap` directly; consumer pods `kubectl get configmap` at startup via an init container. That coupled consumers to the deployer's k8s cluster at runtime and required all stacks to share one namespace. It also bypassed SO's spec-driven ConfigMap mechanism.

**Why this changed:**
- SO now enforces per-deployment namespace ownership; the shared-namespace pattern fails the check.
- Multi-machine deployments (validators on separate hosts) need consumers to bootstrap without k8s API access to the deployer's cluster.
- Git-tracked state files give an audited, reviewable artifact set that ansible can fan out across hosts.

**State files (committed under `deployment/bridges/<bridge>/generated/`):**

| State file | Contents | Produced by | Consumed by |
|---|---|---|---|
| `agent-config.json` | chain definitions, RPC URLs, deployed contract addresses | hyperlane-svm-deployer | validator (CM mount), relayer (CM mount) |
| `gas-oracle-config.json` | token exchange rates, gas prices, overhead | hyperlane-svm-deployer | gas-oracle (env-var injection) |
| `multisig-config.json` | per-chain validator pubkeys + threshold | hyperlane-svm-deployer | conftest/ansible (env-var injection for monitoring, deployer ISM re-config) |
| `program-ids.json` | per-chain deployed program addresses | hyperlane-svm-deployer | warp-deployer (direct disk read at runtime), all stacks (env-var injection) |
| `registry/metadata.yaml` | chain registry metadata | hyperlane-svm-deployer | warp-ui (env-var injection) |
| `token-config.json` | warp route token config with contract addresses | hyperlane-svm-warp-deployer | warp-ui (env-var injection) |
| `warp-deploy-outputs/<file>` | per-warp-route program IDs (hex+base58) | hyperlane-svm-warp-deployer | bridge_setup, warp-ui |

Distribution: `BridgeStateLoader.populate(stack, deploy_dir)` (in `tests/e2e/lib/state_loader.py`) for dev; ansible task (PR3) for prod. Both copy state files into each consumer's `{deploy_dir}/configmaps/`, where SO turns them into k8s ConfigMaps in the consumer's own namespace.

Full design: `docs/superpowers/specs/2026-05-20-bridge-state-extract-and-distribution-design.md`.

---

## Kind Cluster Management

**Decision (2026-05-20):** We bypass SO's built-in Kind cluster management today (`--skip-cluster-management`, now the SO default) and pre-create the cluster ourselves — in tests via a pytest fixture, in prod via ansible. We plan to revisit this in a focused follow-up PR.

**Original reason for bypassing:** Early SO versions tied cluster lifecycle to a single deployment — every `deployment start` tried to create the cluster, every `deployment stop` tried to destroy it. With 8 stacks sharing one cluster that pattern was unworkable, so we lifted cluster lifecycle out of SO.

**Why this can be revisited:** SO's `create_cluster()` (`stack_orchestrator/deploy/k8s/helpers.py:385-409`) is now explicitly designed for the shared-cluster case: *"There is only one kind cluster per host by design. Multiple deployments share this cluster. If a cluster already exists, it is reused."* It also runs `check_mounts_compatible()` against the running cluster to catch deployments declaring incompatible `extraMounts`. The original failure mode is gone.

**Cost of carrying the workaround:**

- **No Caddy ingress controller in tests.** SO's `install_ingress_for_kind()` runs only on the `not skip_cluster_management` branch of `_setup_cluster()` (`deploy_k8s.py:887-894`). So Ingress objects emitted by any stack's `http-proxy:` config exist in our test cluster but are inert (no controller picks them up).
- **Duplicated kind-config.** `tests/e2e/fixtures/kind-config.yaml` re-implements what SO's `generate_kind_config()` already produces (`helpers.py:1295-1341`): `ingress-ready=true` node label, hostPort 80/443 mappings for Caddy, and per-stack `extraMounts` derived from `kind-mount-root`.
- **Manual cluster lifecycle code.** `tests/e2e/lib/cluster.py` mirrors logic SO would handle automatically.

**Switch plan (follow-up PR):**

1. Delete `tests/e2e/fixtures/kind-config.yaml`.
2. Slim `tests/e2e/lib/cluster.py` — drop `create_kind_cluster`; keep `destroy_kind_cluster` for session teardown only.
3. `deploy_start` passes `--perform-cluster-management`; the first stack creates the cluster, the rest reuse it.
4. `deploy_stop` keeps the default `--skip-cluster-management`; the test fixture handles final `kind delete cluster` at session teardown (so an early stop doesn't kill the cluster mid-test).
5. Prod ansible: same model — first `deployment start` (the SVM deployer) creates the cluster + Caddy; subsequent stacks reuse.

The first stack to start must declare the umbrella mount (`kind-mount-root: /srv/kind/hyperlane-bridge`) so the `/mnt` bind is active for everything else. The SVM deployer already does. All specs set `kind-cluster-name: hyperlane`, so all stacks resolve to the same kube context (`kind-hyperlane`).

**Why this is its own PR:** The cluster-management switch is independent of state distribution. Bundling it would broaden this PR's scope and make any test regressions harder to isolate. The current PR retains the existing cluster-management posture verbatim.

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

**Components (in the `hyperlane-monitoring` stack):**

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
