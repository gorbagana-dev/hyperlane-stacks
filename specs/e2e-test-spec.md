# E2E Test Specification — Hyperlane SVM Bridge

End-to-end tests for the full Hyperlane cross-chain bridge deployment on a local kind cluster using `laconic-so`.

## Overview

The test suite deploys all Hyperlane stacks on a kind cluster alongside a Gorchain validator node (laconic-so stack) and a host-local Solana test validator, then progressively validates deployment health, contract state, and cross-chain message flow.

### Test Phases

| Phase | Scope | Validates |
|-------|-------|-----------|
| 1 | Deploy + health | All stacks deploy, pods running, metrics endpoints responding |
| 2 | Contract verification | Program IDs exist on-chain, authorities correct, ConfigMaps populated |
| 3 | Full bridge transfer | Warp route token transfer Gorchain→Solana and Solana→Gorchain |
| 4 | Warp UI | UI deployment, sentinel substitution, browser-driven bridge transfers |

## Infrastructure

### Kind Cluster

A dedicated kind cluster with:
- Caddy ingress controller (SO deploys this automatically)
- cert-manager with self-signed ClusterIssuer (installed as test fixture)
- Ingress resources patched post-deploy to add TLS

```yaml
# kind-config.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    kubeadmConfigPatches:
      - |
        kind: InitConfiguration
        nodeRegistration:
          kubeletExtraArgs:
            node-labels: "ingress-ready=true"
    extraPortMappings:
      - containerPort: 80
        hostPort: 80
        protocol: TCP
      - containerPort: 443
        hostPort: 443
        protocol: TCP
      - containerPort: 9000
        hostPort: 9000
        protocol: TCP
      - containerPort: 9001
        hostPort: 9001
        protocol: TCP
```

### TLS Setup (External)

After kind cluster creation, before stack deployments:

1. Install cert-manager:
   ```bash
   kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml
   kubectl wait --for=condition=Available deployment/cert-manager -n cert-manager --timeout=120s
   ```

2. Create self-signed ClusterIssuer:
   ```yaml
   apiVersion: cert-manager.io/v1
   kind: ClusterIssuer
   metadata:
     name: selfsigned
   spec:
     selfSigned: {}
   ```

3. After each SO stack deploy that uses `http-proxy`, patch the Ingress:
   ```bash
   kubectl patch ingress <ingress-name> --type=merge -p '{
     "metadata": {"annotations": {"cert-manager.io/cluster-issuer": "selfsigned"}},
     "spec": {"tls": [{"hosts": ["<hostname>"], "secretName": "<hostname>-tls"}]}
   }'
   ```

### Gorchain Validator Node

Deployed via laconic-so using the `gorchain` stack from `gorchain-stacks` repo. Runs on the host.

- **Host RPC endpoint**: `http://localhost:8899`
- **In-cluster RPC endpoint**: `http://gorchain-rpc:8899` (via k8s Service, see below)
- **Domain ID**: 99999
- **Chain ID**: 99999

Setup sequence:
```bash
laconic-so --stack gorchain-stacks/stack-orchestrator/stacks/gorchain setup-repositories --git-ssh
laconic-so --stack gorchain-stacks/stack-orchestrator/stacks/gorchain build-containers
laconic-so --stack gorchain-stacks/stack-orchestrator/stacks/gorchain deploy init --output gorchain-spec.yml
laconic-so --stack gorchain-stacks/stack-orchestrator/stacks/gorchain deploy create \
  --spec-file gorchain-spec.yml --deployment-dir gorchain-deployment
laconic-so deployment --dir gorchain-deployment start
```

Health gate: wait for RPC health + slot progression (10+ slots).

### Solana Test Validator (Host Process)

Runs directly on the host machine.

- **Host RPC endpoint**: `http://localhost:18899`
- **In-cluster RPC endpoint**: `http://solana-rpc:18899` (via k8s Service, see below)
- **Domain ID**: 99998
- **Chain ID**: 99998

```bash
solana-test-validator \
  --ledger /tmp/solana-test-ledger \
  --rpc-port 18899 \
  --gossip-port 18001 \
  --dynamic-port-range 19050-19075 \
  --faucet-port 19999 \
  --limit-ledger-size 200000 &
echo $! > /tmp/solana-test-validator.pid
```

Health gate: wait for `getHealth` RPC response.

### In-Cluster Access to Host Chain Nodes

Both chain validators run on the host, outside the kind cluster. To make them accessible from inside the cluster, we create k8s Services with manual Endpoints pointing to the Docker bridge gateway IP.

The host IP is detected at test setup time:
```bash
HOST_IP=$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}')
# Typically 172.18.0.1 for kind networks
```

Declared via the `external-services` spec key in each test spec. The host IP
(`REPLACE_HOST_IP`) is discovered at test time from the Kind network gateway
and patched into the spec before `deploy create`:
```yaml
external-services:
  gorchain-rpc:
    ip: REPLACE_HOST_IP
    port: 8899
  solana-rpc:
    ip: REPLACE_HOST_IP
    port: 18899
config:
  GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
  SOLANA_RPC_URL: "http://solana-rpc:18899"
```

laconic-so creates headless k8s Services + Endpoints pointing at the host IP
during `deploy start`, so the in-cluster DNS names resolve to the host machine.

This keeps the host IP in a single place (the Endpoints manifest) and all other config uses stable DNS names.

## Test Keypair Generation and Funding

All test keypairs are generated during test setup and stored in `/tmp/hyperlane-e2e-keys/`. The keypair generation script (`lib/keygen.sh`) produces all required wallets.

### Required Wallets

| Wallet | Format | Used By | Funding Required |
|--------|--------|---------|-----------------|
| Deployer | Solana keypair JSON (byte array) | `entrypoint.sh` via `DEPLOYER_KEYPAIR` | ~10 SOL on each chain |
| Bridge owner (test) | Just a pubkey (Solana base58) | Receives program ownership | ~0.1 SOL on each chain (for rent) |
| IGP oracle (test) | Solana keypair JSON | Receives IGP account ownership | ~1 SOL on each chain |
| Gorchain validator | secp256k1 private key | Mock KMS proxy signs with this | No SOL needed (signs off-chain) |
| Solana validator | secp256k1 private key | Mock KMS proxy signs with this | No SOL needed |
| Relayer | Solana keypair JSON | Delivers cross-chain messages | ~5 SOL on each chain |

### Keypair Generation

```bash
# Solana keypairs (Ed25519) — JSON byte array format
solana-keygen new --no-bip39-passphrase -o /tmp/hyperlane-e2e-keys/deployer.json
solana-keygen new --no-bip39-passphrase -o /tmp/hyperlane-e2e-keys/owner.json
solana-keygen new --no-bip39-passphrase -o /tmp/hyperlane-e2e-keys/igp-oracle.json
solana-keygen new --no-bip39-passphrase -o /tmp/hyperlane-e2e-keys/relayer.json

# Extract pubkeys
DEPLOYER_PUBKEY=$(solana-keygen pubkey /tmp/hyperlane-e2e-keys/deployer.json)
BRIDGE_OWNER_PUBKEY=$(solana-keygen pubkey /tmp/hyperlane-e2e-keys/owner.json)
IGP_ORACLE_PUBKEY=$(solana-keygen pubkey /tmp/hyperlane-e2e-keys/igp-oracle.json)

# secp256k1 keys (for mock KMS proxy / validator signing)
# Generated as part of mock-kms-proxy build or via openssl:
openssl ecparam -name secp256k1 -genkey -noout -out /tmp/hyperlane-e2e-keys/validator-gorchain.pem
openssl ecparam -name secp256k1 -genkey -noout -out /tmp/hyperlane-e2e-keys/validator-solana.pem
```

### Validator Address Derivation

The deployer needs H160 (Ethereum-format, 20-byte) validator addresses for ISM configuration. These are derived from the secp256k1 public keys:

```
secp256k1 private key → public key (uncompressed, 65 bytes)
  → keccak256(pubkey[1:]) → take last 20 bytes → "0x" prefix
  → H160 address (e.g. "0x1234...abcd")
```

The mock KMS proxy's test keys determine these addresses. The keygen script must:
1. Generate secp256k1 test keys
2. Derive H160 addresses from them
3. Export as `GORCHAIN_VALIDATOR_ADDRESS` and `SOLANA_VALIDATOR_ADDRESS`

### Funding Wallets

Both chains are local test networks with faucets:

```bash
# Fund on Solana test validator (host process, port 18899)
solana airdrop 10 $DEPLOYER_PUBKEY --url http://localhost:18899
solana airdrop 1 $BRIDGE_OWNER_PUBKEY --url http://localhost:18899
solana airdrop 1 $IGP_ORACLE_PUBKEY --url http://localhost:18899

# Fund on Gorchain (host process, port 8899)
solana airdrop 10 $DEPLOYER_PUBKEY --url http://localhost:8899
solana airdrop 1 $BRIDGE_OWNER_PUBKEY --url http://localhost:8899
solana airdrop 1 $IGP_ORACLE_PUBKEY --url http://localhost:8899
```

Note: `solana airdrop` works because both chains are Solana-compatible with built-in faucets. The Solana test validator has `--faucet-port 19999`. Gorchain's faucet behavior should be verified — if it doesn't support airdrop, the genesis config may need pre-funded accounts.

### Test Secret Generation

After keypair generation, create k8s Secret manifests:

```bash
# deployer-secrets.yaml
kubectl create secret generic hyperlane-deployer-secrets \
  --from-file=DEPLOYER_KEYPAIR=/tmp/hyperlane-e2e-keys/deployer.json \
  --from-literal=BRIDGE_OWNER_PUBKEY=$BRIDGE_OWNER_PUBKEY \
  --from-literal=IGP_ORACLE_PUBKEY=$IGP_ORACLE_PUBKEY \
  --from-literal=GORCHAIN_VALIDATOR_ADDRESS=$GORCHAIN_VALIDATOR_H160 \
  --from-literal=SOLANA_VALIDATOR_ADDRESS=$SOLANA_VALIDATOR_H160 \
  --dry-run=client -o yaml > /tmp/hyperlane-e2e-keys/deployer-secrets.yaml

# minio-secrets.yaml
kubectl create secret generic hyperlane-minio-secrets \
  --from-literal=MINIO_ROOT_USER=minioadmin \
  --from-literal=MINIO_ROOT_PASSWORD=minioadmin123 \
  --dry-run=client -o yaml > /tmp/hyperlane-e2e-keys/minio-secrets.yaml

# Apply all secrets
kubectl apply -f /tmp/hyperlane-e2e-keys/
```

## Stack-Orchestrator Workflow

Each hyperlane stack follows this SO lifecycle:

### 1. Setup Repositories (once)

```bash
# Clone hyperlane-monorepo (needed for deployer image build)
laconic-so --stack stack_orchestrator/data/stacks/hyperlane-svm-deployer setup-repositories --git-ssh
```

This clones repos listed in `stack.yml` → `repos:` to `$CERC_REPO_BASE_DIR/`.
For the deployer: `github.com/hyperlane-xyz/hyperlane-monorepo@agents-v2.0.0` → `$CERC_REPO_BASE_DIR/hyperlane-monorepo/`.

### 2. Build Container Images (once, ~30 min for deployer)

```bash
laconic-so --stack stack_orchestrator/data/stacks/hyperlane-svm-deployer build-containers
```

This runs `container-build/gorbagana-dev-hyperlane-svm-deployer/build.sh`, which:
- Copies `entrypoint.sh` into the monorepo build context
- Runs `docker build` with the monorepo as context
- Produces `gorbagana-dev/hyperlane-svm-deployer:local`

Images are cached in Docker's local image store. Subsequent runs skip building if the image exists.

### 3. Deploy Init (generates template spec)

```bash
laconic-so --stack stack_orchestrator/data/stacks/hyperlane-svm-deployer deploy init --output deployer-spec.yml
```

### 4. Deploy Create (creates deployment directory)

```bash
laconic-so --stack stack_orchestrator/data/stacks/hyperlane-svm-deployer deploy create \
  --spec-file deployer-spec.yml --deployment-dir deployer-deployment
```

This:
- Copies compose files to deployment dir
- Copies config files from `data/config/` to deployment dir (for ConfigMap volumes)
- Runs `deploy/commands.py:create()` if present (applies RBAC for kubectl access)
- Generates `config.env` from spec `config:` values

### 5. Start Deployment

```bash
laconic-so deployment --dir deployer-deployment start
```

For k8s-kind: SO generates a k8s Pod spec from the compose file and applies it.

### For E2E Tests

Rather than running the full SO workflow per-stack, use pre-populated test spec files in `fixtures/test-spec-*.yml`. The test runner:

1. Builds all container images once (or reuses cached)
2. For each stack: `deploy init` → merge with test spec → `deploy create` → `start`

## Test Directory Structure

```
tests/
├── e2e/
│   ├── conftest.py                   # Session-scoped fixtures (cluster, chains, keys, deployment)
│   ├── pytest.ini                    # Pytest configuration
│   ├── lib/
│   │   ├── common.py                 # Shared utilities (assertions, waits, kubectl/configmap helpers)
│   │   ├── cluster.py                # Kind cluster create/teardown + TLS setup
│   │   ├── chain.py                  # Gorchain + Solana validator lifecycle
│   │   ├── deploy.py                 # Stack deployment helpers (SO wrappers)
│   │   ├── keygen.py                 # Keypair generation, funding, secret creation
│   │   └── privy_mock.py             # Mock Privy server for validator signing
│   ├── test_01_deployer.py           # Core deployer: Job completion + deep ConfigMap validation
│   ├── test_02_warp_deployer.py      # Warp deployer: token creation, Job completion, on-chain state
│   ├── test_03_minio.py              # MinIO deploy + verify S3 API + buckets
│   ├── test_04_validator.py          # Both validators: deploy, signing, checkpoints, metrics
│   ├── test_05_relayer.py            # Relayer deploy + verify metrics
│   ├── test_06_gas_oracle.py         # Gas oracle deploy + verify on-chain IGP updates
│   ├── test_07_monitoring.py         # Monitoring stack: Prometheus, Grafana, balance monitor
│   ├── test_08_bridge.py             # Cross-chain warp route transfers
│   ├── test_09_fee_claim.py          # IGP fee claim tests
│   ├── test_10_warp_ui.py            # Warp UI deploy + verify TLS ingress
│   ├── test_11_warp_ui_bridge.py     # Warp UI browser bridge tests (Playwright)
│   └── fixtures/
│       ├── kind-config.yaml          # Kind cluster config with port mappings
│       ├── cert-manager-issuer.yaml  # Self-signed ClusterIssuer for TLS
│       ├── test-spec-deployer.yml    # Core deployer test spec
│       ├── test-spec-warp-deployer.yml    # Warp deployer test spec (routes selected via WARP_ROUTES)
│       ├── test-spec-minio.yml       # MinIO test spec
│       ├── test-spec-validator-gorchain.yml  # Validator (Gorchain) test spec
│       ├── test-spec-validator-solana.yml    # Validator (Solana) test spec
│       ├── test-spec-relayer.yml            # Relayer test spec
│       ├── test-spec-gas-oracle.yml         # Gas oracle test spec
│       └── test-spec-monitoring.yml        # Monitoring stack test spec
```

## Phase 1: Deploy + Health

### Prerequisites

- `kind`, `kubectl`, `docker`, `laconic-so` installed
- `solana-test-validator`, `solana`, `solana-keygen` CLI installed (for chain nodes + assertions)
- `jq`, `curl` installed
- Gorchain repos cloned and containers built (can be cached across runs)

### Test Flow

```
 1. Create kind cluster (kind-config.yaml)
 2. Install cert-manager + self-signed ClusterIssuer
 3. Start Gorchain validator (laconic-so on host)
 4. Wait: Gorchain RPC healthy + slot progression (10+ slots)
 5. Start Solana test validator (host process)
 6. Wait: Solana RPC healthy
 7. Detect host IP from Kind network (for external-services spec key)
 8. Generate test keypairs + derive validator H160 addresses
 9. Fund test wallets on both chains (solana airdrop)
10. Create + apply k8s Secrets from generated keypairs
11. Start mock Privy server on host (:19876) with per-chain test keys
12. Build container images (or verify cached):
    - gorbagana-dev/hyperlane-svm-deployer:local (from monorepo, ~30 min first time)
    - gorbagana-dev/hyperlane-kms-proxy:local (real KMS proxy, from hyperlane-kms-proxy/)
    - gorbagana-dev/hyperlane-agent:local (patched agent, from hyperlane-monorepo + patches)
    - gorbagana-dev/hyperlane-gas-oracle:local (from hyperlane-gas-oracle/)
    - gorbagana-dev/hyperlane-warp-ui:local (from hyperlane-warp-ui source)
    - minio/minio (pulled)
13. Load images into kind cluster:
    kind load docker-image <image> --name hyperlane-e2e
14. Deploy stacks in order:
    a. hyperlane-svm-deployer   → verify: deployer Job completed, ConfigMaps created
    b. hyperlane-svm-warp-deployer → verify: warp deployer Job completed, warp ConfigMaps exist
       Warp deployer test flow:
       i.   Create SPL token on Solana test validator (spl-token create-token --decimals 6)
       ii.  Create token account for deployer wallet (spl-token create-account <mint>)
       iii. Mint test supply (1,000,000 USDC = 1,000,000,000,000 base units with 6 decimals)
       iv.  Patch token-config.json in deployment configmap dir with actual mint address,
            name="USDC", symbol="USDC", decimals=6
       v.   Patch warp spec with token mint address
       vi.  Deploy warp stack
       vii. Verify: Job completed, ConfigMaps created (hyperlane-token-config, hyperlane-warp-deploy-outputs)
    c. hyperlane-minio          → verify: minio pods running, S3 API responding, buckets created
    d. hyperlane-validator (gorchain) → verify: pod running, metrics on :9090, checkpoint writes to MinIO
    e. hyperlane-validator (solana)   → verify: same checks
    f. hyperlane-relayer         → verify: pod running, metrics on :9091
    g. hyperlane-gas-oracle      → verify: pod running, first oracle update complete, on-chain IGP updated
    h. hyperlane-monitoring      → verify: prometheus scraping targets, grafana login works
    i. hyperlane-warp-ui         → verify: HTTP 200 on bridge UI, TLS cert valid via ingress
```

### Where Do Assertion Scripts Run?

**On the host**, not inside the cluster. The test machine has `solana` CLI and `kubectl` installed:

- **k8s state assertions**: `kubectl get configmap`, `kubectl get pod`, `kubectl logs` — run on host
- **On-chain assertions**: `solana program show --url http://localhost:8899` — run on host, hitting RPC directly via localhost (host has direct access to both chain nodes)
- **HTTP assertions**: `curl http://localhost:<nodeport>` — via kind NodePort mappings
- **Metrics assertions**: `kubectl port-forward` then `curl localhost:<forwarded-port>/metrics`

This avoids needing `solana` CLI inside the cluster.

### Assertions per Stack

**hyperlane-svm-deployer:**
- Job completes successfully (`kubectl wait --for=condition=complete`)
- `kubectl logs` show "Deployment complete on both chains!"
- ConfigMaps created by deploy.sh:
  - `hyperlane-program-ids` — keys: `gorchain-program-ids.json`, `solana-program-ids.json`
  - `hyperlane-agent-config` — key: `agent-config.json` (valid JSON with both chain configs)
  - `hyperlane-gas-oracle-config` — per-chain gas oracle configs
  - `hyperlane-multisig-config` — per-chain multisig configs

**hyperlane-svm-warp-deployer:**
- Job completes successfully (`kubectl wait --for=condition=complete`)
- `hyperlane-token-config` ConfigMap exists with correct token mint and warp route metadata
- `hyperlane-warp-deploy-outputs` ConfigMap exists with deployment artifacts
- Warp route direction: USDC collateral on Solana (domain 99998) → synthetic USDC on Gorchain (domain 99999)
- `warp-routes/warpRoutes.yaml` exists in state (deployer-produced `WarpCoreConfig`);
  asserted by `test_02_warp_deployer.py::test_warp_deployer_builds_warp_ui_config`

**hyperlane-minio** (`test_03_minio.py`):

Setup:
- Create `hyperlane-minio-secrets` (MINIO_ROOT_USER, MINIO_ROOT_PASSWORD) before deploy
- Deploy using `test-spec-minio.yml` fixture
- MinIO has no dependencies — can deploy in parallel with deployers

Tests:
- `test_minio_pod_running` — Pod reaches Running phase, minio container started
- `test_minio_init_completed` — minio-init container completed (exit 0), bucket creation succeeded. Check via `kubectl get pod` container status or `kubectl logs <pod> -c minio-init`
- `test_minio_s3_api_responds` — Port-forward 9000 to host, verify S3 API responds. Use `boto3` S3 client with MinIO credentials to call `list_buckets()`
- `test_minio_buckets_exist` — Both `hyperlane-validator-gorchain` and `hyperlane-validator-solana` buckets present in list_buckets response
- `test_minio_write_read` — Write a test object to a bucket, read it back, verify content matches (confirms S3 API is fully functional)

Assertion approach: `kubectl port-forward` to expose MinIO S3 API on a local port, then use `boto3` (or `mc` CLI) from the host to verify

**hyperlane-validator (per chain)** (`test_04_validator.py`):

Two separate deployments — one for Gorchain, one for Solana. Each runs a validator
container + KMS proxy sidecar in the same pod. The KMS proxy talks to a **mock Privy
server** running on the host (not a real Privy account).

Prerequisites:
- MinIO deployed and initialized (provides S3 checkpoint storage)
- Core deployer completed (creates `hyperlane-agent-config` ConfigMap)
- Mock Privy server running on host (see [Mock Privy Server](#mock-privy-server-validator-signing))

**Agent-config ConfigMap consumption:**

The deployer job creates a `hyperlane-agent-config` ConfigMap via kubectl at runtime.
The validator needs this ConfigMap mounted as `/config/agent-config.json`. A kubectl
**init container** (labelled `laconic.init-container: "true"` in the compose file)
fetches the real ConfigMap and writes it to a shared PVC (`agent-config`) before the
validator starts.

**Setup (per validator deployment):**

1. Create `hyperlane-validator-{chain}-secrets` k8s Secret:
   - `PRIVY_APP_ID` — test value (e.g. `"test-app-id"`)
   - `PRIVY_APP_SECRET` — test value (e.g. `"test-app-secret"`)
   - `AWS_ACCESS_KEY_ID` — MinIO credentials (same as `MINIO_ROOT_USER`)
   - `AWS_SECRET_ACCESS_KEY` — MinIO credentials (same as `MINIO_ROOT_PASSWORD`)
   - `HYP_DEFAULTSIGNER_KEY` — ed25519 hex key for on-chain announce tx (fund derived address)
2. Deploy using `test-spec-validator-{chain}.yml` fixture
3. Start deployment, wait for pod Running phase

**Tests:**

- `test_validator_pod_running` — Pod reaches Running phase for both containers (validator + kms-proxy)
- `test_kms_proxy_health` — Port-forward kms-proxy :9999, `GET /health` returns 200
- `test_validator_metrics_endpoint` — Port-forward validator :9090, `GET /metrics` returns Prometheus metrics text
- `test_validator_logs_no_errors` — `kubectl logs` for validator container contain no FATAL/PANIC entries within first 30s
- `test_validator_checkpoint_in_minio` — After 60s, MinIO bucket `hyperlane-validator-{chain}` contains at least one checkpoint file. Verify via `mc ls test/hyperlane-validator-{chain}/`
- `test_validator_announcement` — Validator submits `validator_announce` transaction on-chain. Check via `solana program show` or validator logs for announcement confirmation
- `test_checkpoint_after_message` — Dispatch a dummy message via `hyperlane-sealevel-client mailbox send`, wait up to 60s, verify a new checkpoint appears in MinIO with incremented index. This confirms the validator is actively watching the mailbox and signing new merkle roots

**Assertion approach:** `kubectl port-forward` for metrics/health checks, `mc` CLI (via docker) for MinIO bucket inspection, `kubectl logs` for log analysis, `hyperlane-sealevel-client` (via docker + `run_deployer_cli()`) for message dispatch

**hyperlane-relayer** (`test_05_relayer.py`):

Two containers in the pod: the relayer agent and an IGP fee claim sidecar. The relayer
reads validator checkpoints from MinIO, fetches agent-config via an init container
(same pattern as validators), and delivers cross-chain messages.

Prerequisites:
- MinIO deployed and initialized (relayer reads validator checkpoints from S3)
- Core deployer completed (creates `hyperlane-agent-config` ConfigMap)
- Both validators deployed and signing (relayer needs checkpoints to verify messages)

**Agent-config ConfigMap consumption:**

Same init container pattern as the validator: a kubectl init container
(labelled `laconic.init-container: "true"`) fetches the real `hyperlane-agent-config`
ConfigMap and writes it to a shared PVC (`agent-config`) before the relayer starts.

**Setup:**

1. Generate relayer chain signer keys (ed25519 seed as hex, same format as validator
   chain signer HYP_DEFAULTSIGNER_KEY). The relayer needs a funded signer on each chain
   for delivery transactions.
2. Create `hyperlane-relayer-secrets` k8s Secret:
   - `HYP_CHAINS_GORCHAIN_SIGNER_KEY` — hex ed25519 key for Gorchain delivery txs
   - `HYP_CHAINS_SOLANA_SIGNER_KEY` — hex ed25519 key for Solana delivery txs
   - `AWS_ACCESS_KEY_ID` — MinIO credentials (same as `MINIO_ROOT_USER`)
   - `AWS_SECRET_ACCESS_KEY` — MinIO credentials (same as `MINIO_ROOT_PASSWORD`)
   - `RELAYER_KEYPAIR_JSON` — Solana keypair JSON (byte array) for IGP fee claims
3. Read IGP program IDs and account addresses from `hyperlane-program-ids` ConfigMap
4. Deploy using `test-spec-relayer.yml` fixture
5. Start deployment, wait for pod Running phase

**Tests:**

- `test_relayer_pod_running` — Pod reaches Running phase, init container completed,
  relayer container started
- `test_relayer_metrics_endpoint` — Port-forward relayer :9091, `GET /metrics` returns
  Prometheus metrics text containing `hyperlane_` prefixed metrics
- `test_relayer_agent_config_loaded` — `kubectl logs` for relayer container show
  agent-config loaded successfully (both chain configs parsed)
- `test_relayer_checkpoint_syncer_connected` — Logs show relayer connected to S3
  checkpoint syncer (MinIO) and found validator announcements for both chains
- `test_relayer_no_fatal_errors` — `kubectl logs` contain no FATAL/PANIC entries
  within first 60s
- `test_igp_fee_claim_sidecar_running` — IGP fee claim container started,
  `kubectl logs` show "IGP fee claim sidecar starting"

**Assertion approach:** `kubectl port-forward` for metrics, `kubectl logs` for log
analysis. The relayer won't process any messages until Phase 3 (bridge transfer tests)
dispatches them, so Phase 1 tests only validate infrastructure health.

**Test fixture** (`tests/e2e/fixtures/test-spec-relayer.yml`):
```yaml
stack: stack_orchestrator/data/stacks/hyperlane-relayer
deploy-to: k8s-kind
namespace: REPLACE_NAMESPACE
kind-cluster-name: REPLACE_KIND_CLUSTER
config:
  GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
  SOLANA_RPC_URL: "http://solana-rpc:18899"
  GORCHAIN_IGP_PROGRAM_ID: "REPLACE_AT_RUNTIME"
  SOLANA_IGP_PROGRAM_ID: "REPLACE_AT_RUNTIME"
  GORCHAIN_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"
  SOLANA_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"
volumes:
  relayer-data:
  agent-config:
configmaps:
  igp-fee-claim-scripts-config: ./configmaps/igp-fee-claim-scripts-config
secrets:
  hyperlane-relayer-secrets:
    - HYP_CHAINS_GORCHAIN_SIGNER_KEY
    - HYP_CHAINS_SOLANA_SIGNER_KEY
    - AWS_ACCESS_KEY_ID
    - AWS_SECRET_ACCESS_KEY
    - RELAYER_KEYPAIR_JSON
```

**hyperlane-gas-oracle** (`test_06_gas_oracle.py`):

The gas oracle fetches live token prices from CoinGecko, computes exchange rates
using `@hyperlane-xyz/sdk`, and submits `SetGasOracleConfigs` transactions on both
chains. It signs via the mock Privy server using the IGP oracle wallet.

Prerequisites:
- Core deployer completed (creates `hyperlane-program-ids` ConfigMap with IGP program IDs)
- Mock Privy server running on host (oracle signs as `wallet-oracle-001`)
- IGP ownership transferred to oracle wallet (done by deployer's `deploy.sh`)

**Setup:**

1. Read IGP program IDs from `hyperlane-program-ids` ConfigMap (gorchain + solana)
2. Create `hyperlane-gas-oracle-secrets` k8s Secret:
   - `PRIVY_APP_ID` — test value (mock doesn't validate)
   - `PRIVY_APP_SECRET` — test value
   - `PRIVY_ORACLE_WALLET_ID` — `"wallet-oracle-001"` (must match mock)
3. Patch `test-spec-gas-oracle.yml` with actual IGP program IDs
4. Deploy using `test-spec-gas-oracle.yml` fixture
5. Wait for pod Running phase
6. Poll pod logs for `"Gas oracle update complete."` marker (timeout 120s)
7. Parse oracle's logged output to capture computed exchange rates and gas prices

**Tests:**

- `test_oracle_completed_update` — Oracle pod completed at least one update cycle;
  parsed log output contains exchange rates for both directions
- `test_oracle_updated_on_chain` — Query on-chain IGP state via `igp query` CLI
  on both chains. Compare `token_exchange_rate` and `gas_price` against values
  captured from the oracle's own logs (deterministic regardless of live prices)
- `test_oracle_rates_reasonable` — Sanity checks: exchange rates in Sealevel scale
  (1e18–1e22), gas prices > 0, token decimals = 9

**Log parsing:** The oracle logs prices and computed configs in a parseable format:
```
Prices: sGOR=$0.00XX USD, SOL=$XX.XX USD
Gorchain→Solana: exchangeRate=NNNN, gasPrice=MMMM
Solana→Gorchain: exchangeRate=NNNN, gasPrice=MMMM
```
These values are captured by the fixture and used as expected values for on-chain
assertions, avoiding flaky tests from live price fluctuation.

**Race condition mitigation:** The oracle runs in loop mode with a 60s interval.
The fixture waits for the first `"Gas oracle update complete."` log line before
yielding to tests. Subsequent iterations won't fire during the short test window.

**Test fixture** (`tests/e2e/fixtures/test-spec-gas-oracle.yml`):
```yaml
stack: stack_orchestrator/data/stacks/hyperlane-gas-oracle
deploy-to: k8s-kind
namespace: REPLACE_NAMESPACE
kind-cluster-name: REPLACE_KIND_CLUSTER
config:
  GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
  SOLANA_RPC_URL: "http://solana-rpc:18899"
  GORCHAIN_IGP_PROGRAM_ID: "REPLACE_AT_RUNTIME"
  SOLANA_IGP_PROGRAM_ID: "REPLACE_AT_RUNTIME"
  GORCHAIN_DOMAIN_ID: "99999"
  SOLANA_DOMAIN_ID: "99998"
  GAS_ORACLE_INTERVAL_MS: "60000"
  PRIVY_API_URL: "http://privy-mock:19876"
  SIGNER_MODE: "privy"
  GAS_PRICE: "0.000000001"
  GAS_OVERHEAD: "200000"
  EXCHANGE_RATE_MARGIN_PCT: "10"
  MIN_USD_COST: "0.50"
  GORCHAIN_NATIVE_TOKEN_MULTIPLIER: "100"
  PRICE_FEED_URL: "https://api.coingecko.com/api/v3"
secrets:
  hyperlane-gas-oracle-secrets:
    - PRIVY_APP_ID
    - PRIVY_APP_SECRET
    - PRIVY_ORACLE_WALLET_ID
```

**hyperlane-monitoring** (`test_07_monitoring.py`):

The monitoring stack runs Prometheus, Grafana, Pushgateway, and a balance monitor
in a single pod. Prometheus scrapes its own metrics and Pushgateway (static config),
plus validator/relayer pods via k8s service discovery (prometheus.io annotations).
Grafana is provisioned with a Prometheus datasource and three dashboard JSON files.
The balance monitor queries wallet balances via Solana JSON-RPC and pushes metrics
to Pushgateway.

Prerequisites:
- Kind cluster running with namespace created
- Validator and relayer pods already deployed with `prometheus.io/scrape` annotations
  (tests run after `test_05_relayer`)
- Keypairs available (deployer, IGP oracle, IGP beneficiary, relayer-fee-claim pubkeys)

**Setup:**

1. Create `hyperlane-monitoring-secrets` k8s Secret:
   - `GF_SECURITY_ADMIN_PASSWORD` — test value (e.g. `"testadmin"`)
2. Build `MONITORED_WALLETS_GORCHAIN` and `MONITORED_WALLETS_SOLANA` strings from
   keypairs — comma-separated `"label:address"` format:
   - Gorchain: `"deployer:{deployer_pubkey},igp-oracle:{igp_oracle_pubkey},igp-beneficiary:{igp_beneficiary_pubkey},relayer:{relayer_pubkey}"`
   - Solana: same wallet set (all wallets are funded on both chains)
3. Patch `test-spec-monitoring.yml` with wallet strings and RPC URLs
4. Deploy using `test-spec-monitoring.yml` fixture
5. Wait for all containers Running (prometheus, grafana, pushgateway, balance-monitor)
6. Wait for balance monitor to complete at least one check cycle — poll pod logs for
   `"[balance-monitor] Gorchain wallets:"` startup message (timeout 60s)
7. Wait briefly for Pushgateway to receive metrics (~5s after balance-monitor starts)

**Tests:**

- `test_prometheus_healthy` — `GET /prometheus/-/healthy` returns 200 via `kubectl exec`
  (curl/wget in prometheus container)
- `test_prometheus_self_scrape` — Query `up{job="prometheus"}` via PromQL API
  (`/prometheus/api/v1/query`). Assert value is `1` (Prometheus is scraping itself)
- `test_prometheus_pushgateway_target` — Query `up{job="pushgateway"}` via PromQL API.
  Assert value is `1` (Pushgateway scrape target is up)
- `test_prometheus_has_balance_metrics` — Query
  `hyperlane_wallet_balance_sol{chain="gorchain"}` via PromQL API. Assert at least one
  result exists (balance monitor successfully pushed metrics through Pushgateway)
- `test_grafana_healthy` — `GET /grafana/api/health` returns 200 via `kubectl exec`
- `test_grafana_login` — `POST /grafana/api/org` with Basic auth
  (`admin:testadmin`) returns 200, confirming login with injected password
- `test_grafana_datasource_configured` — `GET /grafana/api/datasources` with auth.
  Assert at least one datasource of type `prometheus` exists
- `test_grafana_dashboards_provisioned` — `GET /grafana/api/search` with auth.
  Assert dashboard UIDs include `hyperlane-overview`, `hyperlane-validator`,
  `hyperlane-relayer` (the three provisioned JSON files)
- `test_balance_monitor_wallets_checked` — Parse balance-monitor container logs.
  Confirm log output includes lines for each configured wallet label
  (deployer, igp-oracle, igp-beneficiary, relayer) on both chains
- `test_balance_metrics_have_correct_labels` — Query Prometheus for
  `hyperlane_wallet_balance_sol` and verify `chain`, `wallet`, `address` labels
  are present with expected values. Check both `gorchain` and `solana` chain labels.

**Balance monitor wallet configuration:** The fixture constructs wallet lists from
test keypairs. These are the same wallets funded during test setup, so `getBalance`
calls return real balances. The balance monitor format is `"label:address"` pairs:
```
deployer:ABC123...,igp-oracle:DEF456...,igp-beneficiary:GHI789...,relayer:JKL012...
```

**Grafana API access:** All Grafana API calls use `kubectl exec` against the grafana
container in the monitoring pod, curling `localhost:3000`. This avoids needing
port-forwarding or ingress. Basic auth header: `Authorization: Basic base64(admin:testadmin)`.

**Test fixture** (`tests/e2e/fixtures/test-spec-monitoring.yml`):
```yaml
stack: stack_orchestrator/data/stacks/hyperlane-monitoring
deploy-to: k8s-kind
namespace: REPLACE_NAMESPACE
kind-cluster-name: REPLACE_KIND_CLUSTER
config:
  GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
  SOLANA_RPC_URL: "http://solana-rpc:18899"
  MONITORED_WALLETS_GORCHAIN: "REPLACE_AT_RUNTIME"
  MONITORED_WALLETS_SOLANA: "REPLACE_AT_RUNTIME"
  BALANCE_THRESHOLD_SOL: "1.0"
  BALANCE_CHECK_INTERVAL: "30"
volumes:
  prometheus-data:
  grafana-data:
configmaps:
  prometheus-config: ./configmaps/prometheus-config
  grafana-datasources-config: ./configmaps/grafana-datasources-config
  grafana-dashboard-config: ./configmaps/grafana-dashboard-config
  grafana-dashboards-config: ./configmaps/grafana-dashboards-config
  balance-monitor-scripts-config: ./configmaps/balance-monitor-scripts-config
secrets:
  hyperlane-monitoring-secrets:
    - GF_SECURITY_ADMIN_PASSWORD
```

Note: `BALANCE_CHECK_INTERVAL` is set to 30s in tests (vs 300s in prod) so the
balance monitor completes at least one cycle quickly. The `configmaps:` section is
required because `prepare_spec` overwrites the auto-generated spec — without it,
SO treats config volumes as PVCs.

**hyperlane-warp-ui:**
- Pod running
- HTTP GET on bridge hostname returns 200
- HTTPS GET on bridge hostname returns 200 with valid (self-signed) certificate

## Phase 2: Contract Verification

Runs after Phase 1 completes. Uses `solana` CLI on the host against localhost RPC endpoints.

### Tests

Tests are Python/pytest modules (not shell scripts). See `tests/e2e/` for implementation.

**test_01_deployer.py::test_programs_exist_on_chain:**
- Read `hyperlane-program-ids` ConfigMap via kubectl
- For each program ID (mailbox, validator_announce) on both chains:
  - `solana program show <program-id> --url http://localhost:<port>` succeeds
  - Program is executable and deployed on-chain

**test_01_deployer.py (ConfigMap validation tests) — already implemented:**
- `test_program_ids_configmap` — validates 6 required fields per chain (mailbox, validator_announce, multisig_ism_message_id, igp_program_id, overhead_igp_account, igp_account), all valid base58
- `test_agent_config_configmap` — validates agent-config.json structure, cross-references with program-ids
- `test_gas_oracle_configmap` — validates gas oracle config structure
- `test_multisig_configmap` — validates validator addresses (H160) and threshold
- `test_registry_configmap` — validates chain registry metadata

**test_authorities (future):**
- For each program on both chains:
  - `solana program show <program-id>` → "Authority" field = `BRIDGE_OWNER_PUBKEY`
- IGP account ownership = `IGP_ORACLE_PUBKEY`

**test_01_deployer.py::test_multisig_configmap (ISM config validation):**
- Parse multisig configs for both chains
- Verify validator addresses match test validator H160 addresses
- Verify threshold = 1 (for 1-of-1 test config)

## Phase 3: Full Bridge Transfer

Runs after Phase 2. Executes actual cross-chain warp route transfers using the
deployed warp routes (collateral USDC on Solana ↔ synthetic USDC on Gorchain).

### Prerequisites

All of these must be running/completed before bridge transfer tests:

- Core deployer completed (mailbox, ISM, IGP programs deployed and configured)
- Warp deployer completed (warp route programs deployed, remote routers configured)
- MinIO running (validator checkpoint storage)
- Both validators running and signing checkpoints
- Relayer running and connected to validator checkpoints
- ISM configured with correct validator addresses (so relayer can verify signatures)
- IGP configured with gas oracle (so transfers can pay for gas)

### Fixture: `bridge_setup` (conftest.py)

Session-scoped fixture that depends on `warp_deployment`, `relayer_deployment`,
`validator_gorchain`, and `validator_solana`. Sets up the sender wallet and token
accounts needed for transfers.

**Setup steps:**

1. **Recover warp program addresses** from `hyperlane-warp-deploy-outputs` ConfigMap
   (same helper as `test_02_warp_deployer.py::_get_warp_program_addresses`)
2. **Read the synthetic mint address** from `warpRoute.synthetic.mint` in the
   deployer-emitted `token-config.json` (`_read_synthetic_mint`). The warp deployer
   resolves the mint at deploy time and writes it to state; tests no longer query the
   program themselves.
3. **Use the deployer keypair as sender** — it already has SOL on both chains and
   collateral USDC tokens on Solana (minted during warp deployer setup). No need
   to create a separate sender wallet.
4. **Yield bridge context** to tests:
   ```python
   {
       "namespace": str,
       "token_mint": str,          # Collateral USDC mint on Solana
       "synthetic_mint": str,      # Synthetic USDC mint on Gorchain
       "sender_keypair": str,      # Path to deployer keypair JSON
       "warp_programs": {          # {chain_name: base58_program_id}
           "solana": str,
           "gorchain": str,
       },
   }
   ```

### Token Balance Queries

Use `spl-token balance` CLI (available on the test runner machine):

```bash
# Collateral USDC balance on Solana
spl-token balance <collateral-mint> \
  --owner <sender-keypair> \
  --url http://localhost:18899

# Synthetic USDC balance on Gorchain
spl-token balance <synthetic-mint> \
  --owner <sender-keypair> \
  --url http://localhost:8899
```

Helper function in `lib/common.py`:
```python
def get_spl_token_balance(
    mint: str, owner_keypair: str, rpc_url: str,
) -> float:
    """Query SPL token balance. Returns 0.0 if no token account exists."""
```

### Transfer Commands

Transfers use `hyperlane-sealevel-client token transfer-remote` via `run_deployer_cli()`:

**Solana → Gorchain (collateral → synthetic):**
```bash
hyperlane-sealevel-client \
  --url http://localhost:18899 \
  --keypair <sender-keypair> \
  token transfer-remote <sender-keypair> \
  <amount> 99999 <recipient-pubkey> \
  collateral \
  --program-id <solana-warp-program-id>
```

**Gorchain → Solana (synthetic → collateral):**
```bash
hyperlane-sealevel-client \
  --url http://localhost:8899 \
  --keypair <sender-keypair> \
  token transfer-remote <sender-keypair> \
  <amount> 99998 <recipient-pubkey> \
  synthetic \
  --program-id <gorchain-warp-program-id>
```

Key arguments:
- `<sender-keypair>` appears twice: once as `--keypair` (payer) and once as
  the first positional arg (token sender — can differ, but same wallet in tests)
- `<amount>` is in base units (6 decimals: 1 USDC = 1,000,000)
- `<recipient-pubkey>` is the Solana base58 public key on the destination chain
- The CLI handles ATA creation on the destination automatically (using the
  ATA payer funded during warp deploy)

### Tests (`test_08_bridge.py`)

All tests in `TestBridge` class, marked `@pytest.mark.slow`.

**`test_transfer_solana_to_gorchain`** — Transfer collateral USDC from Solana to synthetic USDC on Gorchain:

1. Record sender's initial collateral USDC balance on Solana
2. Record sender's initial synthetic USDC balance on Gorchain (likely 0)
3. Execute `token transfer-remote` on Solana:
   - Amount: 1,000,000 (1 USDC)
   - Destination domain: 99999 (Gorchain)
   - Recipient: sender's own pubkey (self-transfer for simplicity)
   - Token type: `collateral`
4. Assert transfer tx succeeded (exit code 0)
5. Assert sender's Solana USDC balance decreased by 1,000,000
6. **Poll destination balance** on Gorchain (timeout 120s, poll every 5s):
   - Query `spl-token balance <synthetic-mint> --owner <sender> --url gorchain-rpc`
   - Wait until balance increases by 1,000,000
7. Assert final Gorchain USDC balance = initial + 1,000,000

**`test_transfer_gorchain_to_solana`** — Transfer synthetic USDC back from Gorchain to Solana:

1. Record sender's synthetic USDC balance on Gorchain (should be > 0 from previous test)
2. Record sender's collateral USDC balance on Solana
3. Execute `token transfer-remote` on Gorchain:
   - Amount: 500,000 (0.5 USDC — use half to prove partial transfers work)
   - Destination domain: 99998 (Solana)
   - Recipient: sender's own pubkey
   - Token type: `synthetic`
4. Assert transfer tx succeeded (exit code 0)
5. Assert sender's Gorchain USDC balance decreased by 500,000
6. **Poll destination balance** on Solana (timeout 120s, poll every 5s):
   - Query `spl-token balance <collateral-mint> --owner <sender> --url solana-rpc`
   - Wait until balance increases by 500,000
7. Assert final Solana USDC balance = initial + 500,000

**`test_relayer_processed_messages`** — Verify relayer metrics show successful delivery:

1. Port-forward relayer metrics port (9092)
2. Fetch `/metrics` endpoint
3. Assert `hyperlane_operations_processed_count` > 0 (messages were processed)

### Waiting for Relay Delivery

Bridge transfers are asynchronous — the transfer tx completes on the origin chain
immediately, but delivery on the destination takes time for:
1. Validator to create a checkpoint (< 5s on local chains)
2. Relayer to fetch checkpoint and verify signature (< 5s)
3. Relayer to submit delivery tx on destination (< 5s)

Total expected latency: ~10-15 seconds on local testnets.

Timeout is set to 120s with 5s poll interval to handle edge cases (slow block
production, relayer retry backoff). The polling helper:

```python
def wait_for_token_balance(
    mint: str,
    owner_keypair: str,
    rpc_url: str,
    expected_min: float,
    timeout: int = 120,
    poll_interval: int = 5,
) -> float:
    """Poll SPL token balance until it reaches expected_min or timeout."""
```

### Error Handling

- If `transfer-remote` fails (non-zero exit), the test fails immediately with
  the CLI output in the assertion message
- If balance polling times out, the test fails with the last observed balance
  and hints to check validator/relayer logs
- Tests are ordered: `test_transfer_solana_to_gorchain` runs before
  `test_transfer_gorchain_to_solana` (the reverse transfer needs synthetic
  tokens minted by the first transfer)

## Phase 4: Warp UI

Runs after Phase 3. Deploys the warp-ui stack and validates the bridge UI serves
correctly and can execute actual cross-chain transfers through a browser.

Two test tiers: HTTP smoke tests (`test_10_warp_ui.py`) and browser-driven bridge
transfers (`test_11_warp_ui_bridge.py`).

### Prerequisites

All Phase 3 prerequisites, plus:
- Warp UI container image built (`gorbagana-dev/hyperlane-warp-ui:local`)
- Warp UI image loaded into kind cluster
- Playwright + Chromium installed on test runner (`pip install playwright && playwright install chromium`)

### Fixture: `warp_ui_deployment` (conftest.py)

Session-scoped fixture that depends on `warp_deployment` and `deployer_deployment`.
Deploys the warp-ui stack with addresses resolved from ConfigMaps.

**Setup steps:**

1. **Build and load warp-ui image** into kind cluster (or skip if already loaded,
   following the `--skip-warp-ui-deploy` pattern)
2. **Resolve config values**:
   - Mailbox addresses (gorchain + solana) from deployer state, patched into the spec
     for the container to render into `chains.yaml`
   - `warpRoutes.yaml` is **deployer-produced** (written to `/state/warp-routes/warpRoutes.yaml`
     by `build-warp-ui-config.sh` at the end of `deploy.sh`) and distributed into the
     `warp-ui-config` ConfigMap by `state_distribute`; conftest no longer builds it.
     Its content is asserted by `test_02_warp_deployer.py::test_warp_deployer_builds_warp_ui_config`
     (artifact check) and by `test_10_warp_ui.py` (served by the warp-UI container).
3. **Prepare test spec** from `test-spec-warp-ui.yml`, replacing placeholders:
   ```yaml
   # test-spec-warp-ui.yml
   stack: stack_orchestrator/data/stacks/hyperlane-warp-ui
   deploy-to: k8s-kind
   config:
     GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
     SOLANA_RPC_URL: "http://solana-rpc:18899"
     GORCHAIN_DOMAIN_ID: "99999"
     SOLANA_DOMAIN_ID: "99998"
     GORCHAIN_CHAIN_ID: "99999"
     SOLANA_CHAIN_ID: "99998"
     GORCHAIN_MAILBOX: "REPLACE_AT_RUNTIME"
     SOLANA_MAILBOX: "REPLACE_AT_RUNTIME"
     WARP_COLLATERAL_ADDRESS: "REPLACE_AT_RUNTIME"
     WARP_SYNTHETIC_ADDRESS: "REPLACE_AT_RUNTIME"
     WARP_TOKEN_MINT: "REPLACE_AT_RUNTIME"
   ```
4. **Deploy warp-ui stack** via `deploy_stack()`
5. **Wait for pod healthy** (healthcheck on port 3000)
6. **Yield deployment info** including a `PortForward` context or the local port
   for test access

```python
{
    "deployment": DeploymentInfo,
    "local_port": int,          # Port-forwarded local port (e.g., 13000)
    "gorchain_mailbox": str,
    "solana_mailbox": str,
    "warp_collateral": str,
    "warp_synthetic": str,
    "token_mint": str,
}
```

### Tier 1: HTTP Smoke Tests (`test_10_warp_ui.py`)

No browser needed — uses `subprocess.run(["curl", ...])` or Python `http.client`
via port-forward to the warp-ui pod.

#### Tests

**`test_warp_ui_pod_healthy`**

Verify the warp-ui pod is Running and passes its healthcheck.

```python
def test_warp_ui_pod_healthy(self, warp_ui_deployment):
    ns = warp_ui_deployment["deployment"].namespace
    cluster_id = warp_ui_deployment["deployment"].cluster_id
    result = subprocess.run(
        ["kubectl", "-n", ns, "get", "pods", "-l", f"app={cluster_id}",
         "-o", "jsonpath={.items[0].status.phase}"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "Running"
```

**`test_warp_ui_serves_html`**

GET `/` returns HTTP 200 with HTML content.

```python
def test_warp_ui_serves_html(self, warp_ui_deployment):
    port = warp_ui_deployment["local_port"]
    result = subprocess.run(
        ["curl", "-sf", f"http://localhost:{port}/"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, "Warp UI did not return 200"
    assert "<html" in result.stdout.lower() or "<!doctype" in result.stdout.lower()
```

**`test_warp_ui_sentinels_replaced`**

Fetch JS bundles and verify no sentinel placeholders remain. This proves
`entrypoint.sh` ran successfully.

```python
SENTINELS = [
    "__GORCHAIN_RPC_URL__", "__SOLANA_RPC_URL__",
    "__GORCHAIN_MAILBOX__", "__SOLANA_MAILBOX__",
    "__WARP_COLLATERAL_ADDRESS__", "__WARP_SYNTHETIC_ADDRESS__",
    "__GORCHAIN_CHAIN_NAME__", "__SOLANA_CHAIN_NAME__",
]

def test_warp_ui_sentinels_replaced(self, warp_ui_deployment):
    port = warp_ui_deployment["local_port"]
    # Fetch the HTML page — it loads JS bundles with inlined config
    result = subprocess.run(
        ["curl", "-sf", f"http://localhost:{port}/"],
        capture_output=True, text=True, check=True,
    )
    html = result.stdout

    # Extract JS bundle URLs from <script src="/_next/static/...">
    import re
    js_urls = re.findall(r'src="(/_next/static/[^"]+\.js)"', html)
    assert js_urls, "No JS bundles found in HTML"

    # Fetch each bundle and check for leftover sentinels
    for js_url in js_urls[:5]:  # check first 5 bundles
        js_result = subprocess.run(
            ["curl", "-sf", f"http://localhost:{port}{js_url}"],
            capture_output=True, text=True, check=True,
        )
        for sentinel in SENTINELS:
            assert sentinel not in js_result.stdout, (
                f"Sentinel {sentinel} not replaced in {js_url}"
            )
```

**`test_warp_ui_chain_config_present`**

Verify the served JS contains actual chain names and addresses (not placeholders
and not defaults).

```python
def test_warp_ui_chain_config_present(self, warp_ui_deployment):
    port = warp_ui_deployment["local_port"]
    mailbox = warp_ui_deployment["gorchain_mailbox"]

    # Fetch all JS bundles and concatenate
    result = subprocess.run(
        ["curl", "-sf", f"http://localhost:{port}/"],
        capture_output=True, text=True, check=True,
    )
    # Check that real config values appear somewhere in the served content
    # (they're compiled into the JS bundles)
    all_js = result.stdout
    assert "gorchain" in all_js or mailbox[:8] in all_js, (
        "Chain config not found in served HTML"
    )
```

### Tier 2: Browser Bridge Tests (`test_11_warp_ui_bridge.py`)

Uses **Playwright** to drive the warp-ui in a real browser with the **Backpack
wallet extension** that signs and submits real transactions to the test chains.

#### Backpack Wallet Architecture

The Backpack Chrome extension is downloaded, unpacked, and loaded into a
Chromium persistent browser context via `--load-extension`. The test keypair
is imported into Backpack during setup, and custom RPC URLs are configured
for both chains.

**Key design:** Backpack holds the test keypair and signs real transactions
via its extension popup. This gives true end-to-end coverage through the
full stack: UI → wallet adapter → Backpack popup → on-chain execution →
relay → destination chain.

**Implementation** (`tests/e2e/lib/backpack.py`):

Setup flow:
1. Download and unpack the Backpack CRX (cached in `.backpack-ext/`)
2. Launch Chromium with `--load-extension` pointing to the unpacked dir
3. Import test keypair into Backpack via onboarding flow
4. Set custom RPC URLs for gorchain (`localhost:8899`) and solana (`127.0.0.1:18899`)

Selector notes:
- Backpack uses React Native Web — interactive elements are `<div data-testid>`
- An overlay div intercepts pointer events — all clicks need `force=True` or `dispatch_event("click")`
- React Navigation keeps all screens in DOM — use `.last` to target topmost screen
- Extension opens a separate `popout.html` window for approvals

#### Fixture: `warp_ui_browser` (conftest.py)

Session-scoped fixture that depends on `warp_ui_deployment` and `bridge_setup`.
Launches Chromium with Backpack loaded and configured.

The fixture:
1. Downloads/caches the Backpack CRX
2. Launches a persistent browser context with the extension
3. Imports the test keypair and configures RPC URLs
4. Yields the context, URL, and test data
5. Cleans up on teardown

#### Tests

**`test_warp_ui_loads_in_browser`**

Verify the UI loads in a real browser, renders the transfer form, and shows
the configured chains. Uses URL params to pre-select chains/tokens.
    page.close()
```

**`test_warp_ui_wallet_connects`**

Verify Backpack connects via the wallet modal and the UI reflects connected state.

Connection flow: "Connect wallet" button → protocol modal ("Solana") →
wallet list → "Backpack" → approve popup (if it opens). The test detects
already-connected state by scanning buttons for truncated base58 address patterns.

**`test_warp_ui_bridge_usdc_solana_to_gorchain`**

Execute a real collateral→synthetic USDC transfer through the UI. Self-transfer
mode (recipient auto-filled from connected wallet). Verifies on-chain synthetic
USDC balance increase on Gorchain after relay delivery.

**`test_warp_ui_bridge_usdc_gorchain_to_solana`**

Execute the reverse synthetic→collateral USDC transfer. Switches Backpack RPC to
Gorchain before navigating. Uses a smaller amount (0.05 vs 0.1) to account for
bridge fees.

**`test_warp_ui_bridge_sol_solana_to_gorchain`**

Execute a real native SOL → synthetic SOL transfer through the UI (native route).
Switches Backpack RPC to Solana for the origin, then verifies the on-chain
synthetic SOL balance increase on Gorchain after relay delivery.

**`test_warp_ui_bridge_sol_gorchain_to_solana`**

Execute the reverse synthetic SOL → native SOL transfer. Switches Backpack RPC to
Gorchain, then polls the released native SOL on Solana (destination is native, not
an SPL token). Requires the SOL forward test to have run first.

#### Helper Functions

- `_connect_wallet(page, context)` — Connect Backpack, skip if already connected
- `_fill_amount(page, amount)` — Fill the spinbutton amount input
- `_submit_transfer(page, context, dest_chain, amount)` — Continue → Send → Approve
  with retry on "Plugin Closed" wallet errors (up to 2 attempts)
- `approve_backpack_popup_page(popup)` — Handle optional password unlock, click Approve
- `_screenshot(page, name)` — Save debug screenshot to `/tmp/`

#### Known Issues

- **Single-SVM-chain wallet architecture (upstream):** The warp-ui template's
  `SolanaWalletContext` wraps the entire app in a single `ConnectionProvider`
  with one RPC endpoint. This was designed for one SVM chain (e.g. Solana
  mainnet). With two SVM chains (gorchain + solana), the `ConnectionProvider`
  endpoint and Backpack's active RPC must be managed carefully:
  - **ConnectionProvider** must use a fixed endpoint (solana RPC) so
    `autoConnect` always succeeds regardless of origin chain direction.
  - **Backpack's active RPC** must match the origin chain for transaction
    simulation — Backpack simulates against its own RPC before showing
    the approve dialog. For the reverse bridge (gorchain → solana), Backpack
    must be switched to gorchain RPC *after* wallet connect but *before*
    transaction submission.
  - The actual transaction send uses a per-chain `Connection` created by the
    SDK (`solana.ts: multiProvider.getRpcUrl(chainName)`), independent of
    the `ConnectionProvider` endpoint.
- **Intermittent "Plugin Closed" error:** Backpack popup sometimes closes before
  `sendTransaction` completes. Root cause is a timing issue in the Backpack
  extension. Retry logic handles this — reload and re-submit (up to 5 attempts).
- **Hostname-based chain detection:** The Hyperlane SDK's `findChainByRpcUrl`
  matches chains by RPC hostname only. Both chains on `localhost` are ambiguous.
  Fix: Solana RPC uses `127.0.0.1` while Gorchain uses `localhost`. This is a
  test/dev-only issue — production deployments use distinct hostnames.

#### Error Handling

- Failed wallet connection → screenshot saved, test fails with assertion
- Failed transfer → screenshot saved, retry up to 5 times, then fail
- On-chain balance doesn't change within `RELAY_TIMEOUT` (120s) → fail with
  last observed balance
- Backpack password prompt → auto-filled if detected

#### Dependencies

```
playwright>=1.40
base58
```

Add to test runner setup:
```bash
pip install playwright
playwright install chromium
```

### Test Spec File

```yaml
# test-spec-warp-ui.yml
stack: stack_orchestrator/data/stacks/hyperlane-warp-ui
deploy-to: k8s-kind
config:
  GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
  SOLANA_RPC_URL: "http://solana-rpc:18899"
  GORCHAIN_DOMAIN_ID: "99999"
  SOLANA_DOMAIN_ID: "99998"
  GORCHAIN_CHAIN_ID: "99999"
  SOLANA_CHAIN_ID: "99998"
  GORCHAIN_MAILBOX: "REPLACE_AT_RUNTIME"
  SOLANA_MAILBOX: "REPLACE_AT_RUNTIME"
  WARP_COLLATERAL_ADDRESS: "REPLACE_AT_RUNTIME"
  WARP_SYNTHETIC_ADDRESS: "REPLACE_AT_RUNTIME"
  WARP_TOKEN_MINT: "REPLACE_AT_RUNTIME"
```

## Test Configuration

### Mock Privy Server (Validator Signing)

The Hyperlane validator binary only supports AWS KMS or raw hex key signers. In production,
a **KMS proxy sidecar** (`gorbagana-dev/hyperlane-kms-proxy`) translates AWS KMS API calls to
Privy server wallet RPC requests. The real KMS proxy is used unmodified in e2e tests — we
mock the **Privy API** instead, so the full signing path is exercised.

**Architecture:**

```
┌─ k8s pod ─────────────────────────┐      ┌─ host ──────────────┐
│  validator ──AWS KMS──► kms-proxy ─┼──────┤► mock-privy (:19876)│
│                                    │      │  (Python HTTP)      │
└────────────────────────────────────┘      └─────────────────────┘
```

The KMS proxy has a configurable `PRIVY_API_URL` env var (defaults to `https://api.privy.io`).
In tests, this points to the mock Privy server running on the host via a k8s Service:
`http://privy-mock:19876`.

**How it works:**
1. Validator calls KMS proxy at `http://localhost:9999` (AWS KMS endpoints)
2. KMS proxy translates to Privy RPC: `POST /v1/wallets/{wallet_id}/rpc` with `secp256k1_sign`
3. Mock Privy server looks up `wallet_id` in a dict of test keys, signs with the corresponding
   secp256k1 private key, returns the signature
4. KMS proxy translates back to AWS KMS response format
5. Validator gets a valid ECDSA signature — checkpoint signatures are cryptographically valid

**Per-chain different keys:** Each validator deployment uses a different `PRIVY_WALLET_ID`.
The mock maps each wallet ID to a unique secp256k1 private key:

```python
# tests/e2e/lib/privy_mock.py (simplified)
WALLET_KEYS = {
    "wallet-gorchain-001": "<secp256k1 private key hex>",
    "wallet-solana-001":   "<secp256k1 private key hex>",
}
```

This mirrors production where each validator chain has its own Privy wallet.

**Mock Privy API endpoint:**

```
POST /v1/wallets/{wallet_id}/rpc
Authorization: Basic <app_id>:<app_secret>
Content-Type: application/json

{
  "chain_type": "ethereum",
  "method": "secp256k1_sign",
  "params": {"hash": "0x<hex>", "encoding": "hex"}
}

Response:
{
  "method": "secp256k1_sign",
  "data": {"signature": "0x<r32||s32||v1>", "encoding": "hex"}
}
```

**Implementation:** `tests/e2e/lib/privy_mock.py` — ~50-80 lines of Python using
`http.server`. The mock also handles the `GetPublicKey` flow (KMS proxy calls
`secp256k1_sign` on a known digest at startup to recover the public key).

**Host service exposure:** The `privy-mock` service is declared via
`external-services` in the validator and gas-oracle test specs:
```yaml
external-services:
  privy-mock:
    ip: REPLACE_HOST_IP
    port: 19876
```

**Validator address derivation:** The mock's test keys determine the H160 validator
addresses. During keypair generation (`lib/keygen.py`), secp256k1 keys are generated
via `cast wallet new`, which produces both the private key and H160 address. These
addresses are passed to the deployer as `GORCHAIN_VALIDATOR_ADDRESS` and
`SOLANA_VALIDATOR_ADDRESS` for ISM configuration. Two different keys → two different
H160 addresses, matching the production setup.

**Validator secrets per chain:**

```bash
# Gorchain validator
kubectl create secret generic hyperlane-validator-gorchain-secrets \
  --from-literal=PRIVY_APP_ID=test-app-id \
  --from-literal=PRIVY_APP_SECRET=test-app-secret \
  --from-literal=PRIVY_WALLET_ID=wallet-gorchain-001 \
  --from-literal=AWS_ACCESS_KEY_ID=$MINIO_USER \
  --from-literal=AWS_SECRET_ACCESS_KEY=$MINIO_PASSWORD

# Solana validator
kubectl create secret generic hyperlane-validator-solana-secrets \
  --from-literal=PRIVY_APP_ID=test-app-id \
  --from-literal=PRIVY_APP_SECRET=test-app-secret \
  --from-literal=PRIVY_WALLET_ID=wallet-solana-001 \
  --from-literal=AWS_ACCESS_KEY_ID=$MINIO_USER \
  --from-literal=AWS_SECRET_ACCESS_KEY=$MINIO_PASSWORD
```

### Mock Gas Oracle Signer

The gas oracle supports two signing modes via `SIGNER_MODE` env var:

- **`privy`** (default, production): Signs via Privy server wallet
- **`keypair`** (testing): Signs with a local Solana keypair via `ORACLE_KEYPAIR` env var

For e2e tests, set `SIGNER_MODE=keypair` and inject a funded test Ed25519 keypair as `ORACLE_KEYPAIR` (JSON array format).

> **Note:** If real Privy test credentials become available, the mock KMS proxy and local oracle signing can be swapped for real Privy integration to test the full signing path. The test fixtures are designed to make this a configuration-only change (swap image/env vars, no test logic changes).

### Test Spec Files (fixtures/test-spec-*.yml)

Pre-populated spec files with:
- `config:` values pointing to in-cluster RPC DNS names (`http://gorchain-rpc:8899`)
- `configmaps:` pointing to config directories
- `secrets:` referencing the test Secret names

Example (see `tests/e2e/fixtures/` for canonical versions):
```yaml
# test-spec-deployer.yml
stack: stack_orchestrator/data/stacks/hyperlane-svm-deployer
deploy-to: k8s-kind
configmaps:
  deployer-scripts-config: ./configmaps/deployer-scripts-config
  deployer-gas-oracle-config: ./configmaps/deployer-gas-oracle-config
  deployer-multisig-config: ./configmaps/deployer-multisig-config
  deployer-registry-config: ./configmaps/deployer-registry-config
config:
  GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
  SOLANA_RPC_URL: "http://solana-rpc:18899"
  GORCHAIN_DOMAIN_ID: "99999"
  SOLANA_DOMAIN_ID: "99998"
  GORCHAIN_CHAIN_ID: "99999"
  SOLANA_CHAIN_ID: "99998"
  GORCHAIN_IS_TESTNET: "true"
  SOLANA_IS_TESTNET: "true"
  FORCE_REDEPLOY: "false"
secrets:
  hyperlane-deployer-secrets:
    - DEPLOYER_KEYPAIR
    - BRIDGE_OWNER_PUBKEY
    - IGP_ORACLE_PUBKEY
    - GORCHAIN_VALIDATOR_ADDRESS
    - SOLANA_VALIDATOR_ADDRESS
```

```yaml
# test-spec-warp-deployer.yml — single deployment; routes selected via WARP_ROUTES
# and read from the warp-routes-config ConfigMap (rendered from the e2e menu under
# tests/e2e/fixtures/warp-routes/<stem>.yml; the e2e USDC mint is injected at
# runtime into the route JSON, not the spec).
stack: stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer
deploy-to: k8s-kind
namespace: laconic-hyperlane-warp-deployer
config:
  WARP_ROUTES: "usdc sol"
  GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
  GORCHAIN_DOMAIN_ID: "99999"
  SOLANA_DOMAIN_ID: "99998"
  GORCHAIN_CHAIN_ID: "99999"
  SOLANA_CHAIN_ID: "99998"
  GORCHAIN_IS_TESTNET: "true"
  SOLANA_IS_TESTNET: "true"
  FORCE_REDEPLOY: "false"
configmaps:
  warp-deployer-scripts-config: ./configmaps/warp-deployer-scripts-config
  warp-deployer-registry-config: ./configmaps/warp-deployer-registry-config
  warp-routes-config: ./configmaps/warp-routes-config
secrets:
  hyperlane-warp-deployer-secrets:
    - DEPLOYER_KEYPAIR
    - BRIDGE_OWNER_PUBKEY
    - SOLANA_RPC_URL
```

```yaml
# test-spec-minio.yml
stack: stack_orchestrator/data/stacks/hyperlane-minio
deploy-to: k8s-kind
network:
  ports:
    minio:
      - "9000"
      - "9001"
volumes:
  minio-data: 1Gi
secrets:
  hyperlane-minio-secrets:
    - MINIO_ROOT_USER
    - MINIO_ROOT_PASSWORD
```

```yaml
# test-spec-validator-gorchain.yml
stack: stack_orchestrator/data/stacks/hyperlane-validator
deploy-to: k8s-kind
namespace: REPLACE_NAMESPACE
kind-cluster-name: REPLACE_KIND_CLUSTER
config:
  ORIGIN_CHAIN_NAME: gorchain
  CHECKPOINT_BUCKET: hyperlane-validator-gorchain
  PRIVY_API_URL: "http://privy-mock:19876"
  PRIVY_WALLET_ID: REPLACE_PRIVY_WALLET_ID
volumes:
  validator-data:
  agent-config:
secrets:
  hyperlane-validator-gorchain-secrets:
    - PRIVY_APP_ID
    - PRIVY_APP_SECRET
    - AWS_ACCESS_KEY_ID
    - AWS_SECRET_ACCESS_KEY
    - HYP_DEFAULTSIGNER_KEY
```

```yaml
# test-spec-validator-solana.yml
stack: stack_orchestrator/data/stacks/hyperlane-validator
deploy-to: k8s-kind
namespace: REPLACE_NAMESPACE
kind-cluster-name: REPLACE_KIND_CLUSTER
config:
  ORIGIN_CHAIN_NAME: solana
  CHECKPOINT_BUCKET: hyperlane-validator-solana
  PRIVY_API_URL: "http://privy-mock:19876"
  PRIVY_WALLET_ID: REPLACE_PRIVY_WALLET_ID
volumes:
  validator-data:
  agent-config:
secrets:
  hyperlane-validator-solana-secrets:
    - PRIVY_APP_ID
    - PRIVY_APP_SECRET
    - AWS_ACCESS_KEY_ID
    - AWS_SECRET_ACCESS_KEY
    - HYP_DEFAULTSIGNER_KEY
```

## Test Runner

Tests use Python pytest. See `tests/e2e/conftest.py` for session-scoped fixtures and CLI options.

### Usage

```bash
cd tests/e2e

# Run all tests (full setup + teardown)
pytest -v

# Run specific test module
pytest test_01_deployer.py -v
pytest test_02_warp_deployer.py -v

# Skip infrastructure setup (reuse existing cluster + chains)
pytest -v --skip-cluster-setup --skip-chain-setup

# Reuse existing deployments from a previous --skip-cleanup run
pytest -v --skip-cluster-setup --skip-chain-setup --skip-core-deploy --skip-warp-deploy --skip-cleanup

# Skip teardown (keep everything running for debugging or re-runs)
pytest -v --skip-cleanup

# Build deployer image from source instead of pulling published image
pytest -v --build-from-source
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All tests passed |
| 1 | Test failure (assertion failed) |
| 2 | Infrastructure failure (cluster/chain setup) |
| 3 | Timeout (stack didn't become healthy) |

### Timeouts

| Operation | Timeout |
|-----------|---------|
| Kind cluster creation | 120s |
| cert-manager ready | 120s |
| Gorchain RPC healthy | 900s (container build may be slow) |
| Solana RPC healthy | 30s |
| Stack deploy (long-running) | 300s per stack |
| Stack deploy (one-time jobs) | 600s (deployer builds can be slow) |
| Cross-chain relay | 120s |
| Checkpoint write | 60s |

## Teardown

```bash
# Stop all SO deployments
for dir in *-deployment; do
  laconic-so deployment --dir "$dir" stop --delete-volumes
done

# Kill Solana test validator
kill $(cat /tmp/solana-test-validator.pid)

# Delete kind cluster
kind delete cluster --name hyperlane-e2e

# Clean up keypairs
rm -rf /tmp/hyperlane-e2e-keys /tmp/solana-test-ledger
```

## Design Decisions

1. **Privy in tests:** Using the **real KMS proxy** with a **mock Privy server** running on the host. The mock implements Privy's `secp256k1_sign` RPC with local test keys (different key per validator chain). This exercises the full signing path (validator → KMS proxy → Privy API → signature). If real Privy test credentials become available, just stop the mock and point `PRIVY_API_URL` at the real API — no code changes needed.

2. **Deploy script strategy:** The deployer uses a ConfigMap-mounted `deploy.sh` at `/opt/scripts/deploy.sh` (env-var-driven). The deployer scripts are mounted via ConfigMap volumes, not baked into the image. Config templates (multisig, gas-oracle, registry) are also mounted via ConfigMap volumes and rendered at runtime via `envsubst`. The deploy script creates output k8s ConfigMaps (program-ids, agent-config, etc.) directly via kubectl.

3. **Assertion scripts run on host:** All test assertions use `solana` CLI and `kubectl` from the host machine. On-chain queries hit chain RPC directly via localhost. k8s queries use kubectl. No need for solana CLI inside the cluster.

## Resolved Questions

1. **Container image caching:** Use Docker's local image store for now (only rebuild if missing or `--force-rebuild`). Once CI publish workflows are set up, switch to pre-built images from `ghcr.io/gorbagana-dev` registry.

2. **CI integration:** Shell scripts that work standalone + a thin GitHub Actions wrapper (`.github/workflows/e2e.yml`).

3. **Gorchain faucet:** Gorchain is a Solana fork — `solana airdrop` works against its RPC endpoint.

4. **Test token mint:** The test setup creates the SPL token mint before the warp deployer runs, then passes the mint address to the warp deployer via env/config.
