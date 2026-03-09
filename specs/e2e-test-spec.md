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

Applied as a test fixture (`fixtures/host-chain-services.yaml`):
```yaml
# Gorchain RPC
apiVersion: v1
kind: Service
metadata:
  name: gorchain-rpc
spec:
  ports:
    - port: 8899
      targetPort: 8899
---
apiVersion: v1
kind: Endpoints
metadata:
  name: gorchain-rpc
subsets:
  - addresses:
      - ip: "${HOST_IP}"
    ports:
      - port: 8899
---
# Solana RPC
apiVersion: v1
kind: Service
metadata:
  name: solana-rpc
spec:
  ports:
    - port: 18899
      targetPort: 18899
---
apiVersion: v1
kind: Endpoints
metadata:
  name: solana-rpc
subsets:
  - addresses:
      - ip: "${HOST_IP}"
    ports:
      - port: 18899
```

The test setup script templates `${HOST_IP}` into the manifest and applies it:
```bash
HOST_IP=$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}')
sed "s/\${HOST_IP}/$HOST_IP/g" fixtures/host-chain-services.yaml | kubectl apply -f -
```

All stack spec files use the in-cluster DNS names:
```yaml
config:
  GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
  SOLANA_RPC_URL: "http://solana-rpc:18899"
```

This keeps the host IP in a single place (the Endpoints manifest) and all other config uses stable DNS names.

## Test Keypair Generation and Funding

All test keypairs are generated during test setup and stored in `/tmp/hyperlane-e2e-keys/`. The keypair generation script (`lib/keygen.sh`) produces all required wallets.

### Required Wallets

| Wallet | Format | Used By | Funding Required |
|--------|--------|---------|-----------------|
| Deployer | Solana keypair JSON (byte array) | `entrypoint.sh` via `DEPLOYER_KEYPAIR` | ~10 SOL on each chain |
| Hardware wallet (test) | Just a pubkey (Solana base58) | Receives program ownership | ~0.1 SOL on each chain (for rent) |
| IGP oracle (test) | Solana keypair JSON | Receives IGP account ownership | ~1 SOL on each chain |
| Gorchain validator | secp256k1 private key | Mock KMS proxy signs with this | No SOL needed (signs off-chain) |
| Solana validator | secp256k1 private key | Mock KMS proxy signs with this | No SOL needed |
| Relayer | Solana keypair JSON | Delivers cross-chain messages | ~5 SOL on each chain |

### Keypair Generation

```bash
# Solana keypairs (Ed25519) — JSON byte array format
solana-keygen new --no-bip39-passphrase -o /tmp/hyperlane-e2e-keys/deployer.json
solana-keygen new --no-bip39-passphrase -o /tmp/hyperlane-e2e-keys/hardware-wallet.json
solana-keygen new --no-bip39-passphrase -o /tmp/hyperlane-e2e-keys/igp-oracle.json
solana-keygen new --no-bip39-passphrase -o /tmp/hyperlane-e2e-keys/relayer.json

# Extract pubkeys
DEPLOYER_PUBKEY=$(solana-keygen pubkey /tmp/hyperlane-e2e-keys/deployer.json)
HARDWARE_WALLET_PUBKEY=$(solana-keygen pubkey /tmp/hyperlane-e2e-keys/hardware-wallet.json)
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
solana airdrop 1 $HARDWARE_WALLET_PUBKEY --url http://localhost:18899
solana airdrop 1 $IGP_ORACLE_PUBKEY --url http://localhost:18899

# Fund on Gorchain (host process, port 8899)
solana airdrop 10 $DEPLOYER_PUBKEY --url http://localhost:8899
solana airdrop 1 $HARDWARE_WALLET_PUBKEY --url http://localhost:8899
solana airdrop 1 $IGP_ORACLE_PUBKEY --url http://localhost:8899
```

Note: `solana airdrop` works because both chains are Solana-compatible with built-in faucets. The Solana test validator has `--faucet-port 19999`. Gorchain's faucet behavior should be verified — if it doesn't support airdrop, the genesis config may need pre-funded accounts.

### Test Secret Generation

After keypair generation, create k8s Secret manifests:

```bash
# deployer-secrets.yaml
kubectl create secret generic hyperlane-deployer-secrets \
  --from-file=DEPLOYER_KEYPAIR=/tmp/hyperlane-e2e-keys/deployer.json \
  --from-literal=HARDWARE_WALLET_PUBKEY=$HARDWARE_WALLET_PUBKEY \
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

This runs `container-build/laconic-hyperlane-svm-deployer/build.sh`, which:
- Copies `entrypoint.sh` into the monorepo build context
- Runs `docker build` with the monorepo as context
- Produces `laconic/hyperlane-svm-deployer:local`

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
│   │   └── keygen.py                 # Keypair generation, funding, secret creation
│   ├── test_deployer.py              # Core deployer: Job completion + deep ConfigMap validation
│   ├── test_warp_deployer.py         # Warp deployer: token creation, Job completion, on-chain state
│   ├── test_minio.py                 # MinIO deploy + verify S3 API + buckets (planned)
│   ├── test_validators.py            # Both validators deploy + verify metrics (planned)
│   ├── test_relayer.py               # Relayer deploy + verify metrics (planned)
│   ├── test_gas_oracle.py            # Gas oracle deploy + verify (planned)
│   ├── test_monitoring.py            # Monitoring deploy + verify Grafana/Prometheus (planned)
│   ├── test_warp_ui.py               # Warp UI deploy + verify TLS ingress (planned)
│   ├── test_transfer.py              # Cross-chain warp route transfers (planned)
│   └── fixtures/
│       ├── kind-config.yaml          # Kind cluster config with port mappings
│       ├── cert-manager-issuer.yaml  # Self-signed ClusterIssuer for TLS
│       ├── host-chain-services.yaml  # k8s Services pointing to host chain nodes
│       ├── test-spec-deployer.yml    # Core deployer test spec
│       ├── test-spec-warp-deployer.yml  # Warp deployer test spec
│       └── test-spec-minio.yml       # MinIO test spec (planned)
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
 7. Apply host-chain-services (k8s Services pointing to host IPs)
 8. Generate test keypairs + derive validator H160 addresses
 9. Fund test wallets on both chains (solana airdrop)
10. Create + apply k8s Secrets from generated keypairs
11. Build container images (or verify cached):
    - laconic/hyperlane-svm-deployer:local (from monorepo, ~30 min first time)
    - laconic/hyperlane-kms-proxy:local (mock, from tests/e2e/mock-kms-proxy/)
    - laconic/hyperlane-gas-oracle:local (from hyperlane-gas-oracle/)
    - laconic/hyperlane-warp-ui:local (from hyperlane-warp-ui source)
    - minio/minio, gcr.io/abacus-labs-dev/hyperlane-agent:agents-v2.0.0 (pulled)
12. Load images into kind cluster:
    kind load docker-image <image> --name hyperlane-e2e
13. Deploy stacks in order:
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
    g. hyperlane-gas-oracle      → verify: pod running, at least one oracle update tx submitted
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

**hyperlane-minio** (`test_minio.py`):

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

**hyperlane-validator (per chain):**
- Pod phase = Running
- Both containers (validator + mock-kms-proxy) in Running state
- Metrics endpoint responds on :9090
- MinIO bucket has at least one checkpoint file after 60s

**hyperlane-relayer:**
- Pod phase = Running
- Relayer container running
- Metrics endpoint responds on :9091

**hyperlane-gas-oracle:**
- Pod phase = Running (or completed one cycle if run-once mode)
- Logs contain "Gas oracle update" or equivalent success message

**hyperlane-monitoring:**
- Prometheus pod running, scrape targets include validator/relayer endpoints
- Grafana responds on :3000, login with test admin password succeeds
- Pushgateway responds on :9091

**hyperlane-warp-ui:**
- Pod running
- HTTP GET on bridge hostname returns 200
- HTTPS GET on bridge hostname returns 200 with valid (self-signed) certificate

## Phase 2: Contract Verification

Runs after Phase 1 completes. Uses `solana` CLI on the host against localhost RPC endpoints.

### Tests

Tests are Python/pytest modules (not shell scripts). See `tests/e2e/` for implementation.

**test_deployer.py::test_programs_exist_on_chain:**
- Read `hyperlane-program-ids` ConfigMap via kubectl
- For each program ID (mailbox, validator_announce) on both chains:
  - `solana program show <program-id> --url http://localhost:<port>` succeeds
  - Program is executable and deployed on-chain

**test_deployer.py (ConfigMap validation tests) — already implemented:**
- `test_program_ids_configmap` — validates 6 required fields per chain (mailbox, validator_announce, multisig_ism_message_id, igp_program_id, overhead_igp_account, igp_account), all valid base58
- `test_agent_config_configmap` — validates agent-config.json structure, cross-references with program-ids
- `test_gas_oracle_configmap` — validates gas oracle config structure
- `test_multisig_configmap` — validates validator addresses (H160) and threshold
- `test_registry_configmap` — validates chain registry metadata

**test_authorities (future):**
- For each program on both chains:
  - `solana program show <program-id>` → "Authority" field = `HARDWARE_WALLET_PUBKEY`
- IGP account ownership = `IGP_ORACLE_PUBKEY`

**test_deployer.py::test_multisig_configmap (ISM config validation):**
- Parse multisig configs for both chains
- Verify validator addresses match test validator H160 addresses
- Verify threshold = 1 (for 1-of-1 test config)

## Phase 3: Full Bridge Transfer

Runs after Phase 2. Executes actual cross-chain warp route transfers.

### Setup
- The warp deployer has already deployed the warp route with the USDC token mint (created during Phase 1, step 13b)
- Create and fund test sender wallets on both chains (`solana airdrop`)
- Create token account for the TEST SENDER wallet (not the deployer) and mint test supply:
  ```bash
  # Create token account for the test sender wallet
  spl-token create-account <mint-address> --url http://localhost:18899 --owner <sender-keypair>
  # Mint test USDC to the sender (1,000,000 USDC = 1,000,000,000,000 base units with 6 decimals)
  spl-token mint <mint-address> 1000000 --url http://localhost:18899 -- <sender-token-account>
  ```
- Transfer directions:
  - **Solana -> Gorchain**: Locks collateral USDC on Solana, mints synthetic USDC on Gorchain
  - **Gorchain -> Solana**: Burns synthetic USDC on Gorchain, unlocks collateral USDC on Solana

### Tests

**test_transfer.py::test_transfer_sol2gor (Solana → Gorchain):**
1. Record initial balances:
   - Source wallet USDC balance on Solana
   - Destination wallet gUSDC balance on Gorchain
2. Execute warp transfer:
   - Call collateral warp route `transfer_remote` on Solana
   - Amount: 1,000,000 (1 USDC, 6 decimals)
   - Pay IGP fee
3. Wait for relay (poll destination balance, timeout 120s)
4. Assert:
   - Source USDC balance decreased by 1,000,000
   - Destination gUSDC balance increased by 1,000,000
   - Relayer metrics show 1 message processed

**test_transfer.py::test_transfer_gor2sol (Gorchain → Solana):**
1. Same pattern in reverse direction
2. Burn gUSDC on Gorchain, receive USDC on Solana
3. Assert balances updated correctly

**test_transfer.py::test_relay_metrics:**
- Query relayer prometheus metrics
- `hyperlane_messages_processed_total` > 0
- No `hyperlane_messages_failed_total` (or = 0)
- Validator checkpoint index advanced

## Test Configuration

### Mock KMS Proxy (Validator Signing)

The Hyperlane validator binary only supports AWS KMS or raw hex key signers. In production, a KMS proxy sidecar translates AWS KMS API calls to Privy server wallet requests. For e2e tests, we replace the real KMS proxy with a **mock KMS proxy** that signs with a local test key.

**How it works:**
- The mock implements the same three AWS KMS endpoints (`Sign`, `GetPublicKey`, `DescribeKey`)
- Uses a hardcoded secp256k1 test private key to produce real ECDSA signatures
- The validator binary is unmodified — it calls `http://localhost:9999` and gets valid responses
- Checkpoint signatures are cryptographically valid, so the relayer accepts them

**Implementation:** A minimal Go or Python HTTP server (~100 lines) in `tests/e2e/mock-kms-proxy/`:

```
tests/e2e/mock-kms-proxy/
├── Dockerfile
├── main.go          # (or main.py)
└── test-key.json    # secp256k1 test private key (NOT a real key)
```

The mock handles:
- `TrentService.Sign` → sign digest with test key, return DER-encoded signature
- `TrentService.GetPublicKey` → return test key's public key in SubjectPublicKeyInfo DER
- `TrentService.DescribeKey` → return static metadata (`ECC_SECG_P256K1`)

**Test image swap:** SO doesn't support per-service image overrides. The mock is built with the same image name as the real KMS proxy:
```bash
docker build -t laconic/hyperlane-kms-proxy:local tests/e2e/mock-kms-proxy/
```
This makes it a drop-in replacement — the validator compose file and stack definition are used as-is. The mock image just needs to be built *after* (or instead of) the real KMS proxy image during test setup.

**Validator address derivation:** The mock's test key determines the H160 validator address. The keygen script derives H160 from the test key and passes it to the deployer as `GORCHAIN_VALIDATOR_ADDRESS` / `SOLANA_VALIDATOR_ADDRESS`. If two separate test keys are used (one per chain), two different H160 addresses result.

### Mock Gas Oracle Signer

The gas oracle uses Privy's Ed25519 wallet to sign and submit `SetGasOracleConfigs` transactions. For e2e tests:

- Inject a test Ed25519 keypair as `ORACLE_KEYPAIR_JSON` (Solana keypair format)
- Modify the gas oracle to detect test mode: if `ORACLE_KEYPAIR_JSON` is set, sign locally instead of calling Privy
- Fund the test oracle wallet via faucet/airdrop on both chains

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
    - HARDWARE_WALLET_PUBKEY
    - IGP_ORACLE_PUBKEY
    - GORCHAIN_VALIDATOR_ADDRESS
    - SOLANA_VALIDATOR_ADDRESS
```

```yaml
# test-spec-warp-deployer.yml
stack: stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer
deploy-to: k8s-kind
config:
  WARP_TOKEN_MINT: "REPLACE_AT_RUNTIME"
  WARP_ROUTE_NAME: "USDC-solana-gorchain"
  COLLATERAL_CHAIN: solana
  SYNTHETIC_CHAIN: gorchain
  GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
  SOLANA_RPC_URL: "http://solana-rpc:18899"
  GORCHAIN_DOMAIN_ID: "99999"
  SOLANA_DOMAIN_ID: "99998"
  GORCHAIN_CHAIN_ID: "99999"
  SOLANA_CHAIN_ID: "99998"
  GORCHAIN_IS_TESTNET: "true"
  SOLANA_IS_TESTNET: "true"
  COLLATERAL_CHAIN_RPC_URL: "http://solana-rpc:18899"
  COLLATERAL_DOMAIN_ID: "99998"
  SYNTHETIC_CHAIN_RPC_URL: "http://gorchain-rpc:8899"
  SYNTHETIC_DOMAIN_ID: "99999"
  WARP_TOKEN_METADATA_URI: ""
  FORCE_REDEPLOY: "false"
configmaps:
  warp-deployer-scripts-config: ./configmaps/warp-deployer-scripts-config
  warp-deployer-token-config: ./configmaps/warp-deployer-token-config
  warp-deployer-registry-config: ./configmaps/warp-deployer-registry-config
secrets:
  hyperlane-warp-deployer-secrets:
    - DEPLOYER_KEYPAIR
    - HARDWARE_WALLET_PUBKEY
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

## Test Runner

Tests use Python pytest. See `tests/e2e/conftest.py` for session-scoped fixtures and CLI options.

### Usage

```bash
cd tests/e2e

# Run all tests (full setup + teardown)
pytest -v

# Run specific test module
pytest test_deployer.py -v
pytest test_warp_deployer.py -v

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

1. **Privy in tests:** Using mock KMS proxy + local oracle signing for e2e tests. If real Privy test credentials become available, swap to real Privy integration (configuration-only change — replace mock image with real KMS proxy, inject Privy env vars).

2. **Deploy script strategy:** The deployer uses a ConfigMap-mounted `deploy.sh` at `/opt/scripts/deploy.sh` (env-var-driven). The deployer scripts are mounted via ConfigMap volumes, not baked into the image. Config templates (multisig, gas-oracle, registry) are also mounted via ConfigMap volumes and rendered at runtime via `envsubst`. The deploy script creates output k8s ConfigMaps (program-ids, agent-config, etc.) directly via kubectl.

3. **Assertion scripts run on host:** All test assertions use `solana` CLI and `kubectl` from the host machine. On-chain queries hit chain RPC directly via localhost. k8s queries use kubectl. No need for solana CLI inside the cluster.

## Resolved Questions

1. **Container image caching:** Use Docker's local image store for now (only rebuild if missing or `--force-rebuild`). Once CI publish workflows are set up, switch to pre-built images from `git.vdb.to` registry.

2. **CI integration:** Shell scripts that work standalone + a thin GitHub Actions wrapper (`.github/workflows/e2e.yml`).

3. **Gorchain faucet:** Gorchain is a Solana fork — `solana airdrop` works against its RPC endpoint.

4. **Test token mint:** The test setup creates the SPL token mint before the warp deployer runs, then passes the mint address to the warp deployer via env/config.
