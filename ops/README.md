# Hyperlane SVM Ops — Raw Kubernetes Job Manifests

Operational jobs that don't fit the Stack Orchestrator model (they're one-time k8s Jobs requiring hardware wallet signing, not long-running services). Apply directly with `kubectl`.

## Prerequisites

- Core deployer has run and `hyperlane-program-ids` ConfigMap exists
- Hardware wallet (Ledger) connected with Solana app
- `solana` CLI installed locally
- Edit the `env` section in each job YAML to set your values before applying

## Jobs

### kill-switch-job.yaml — Emergency Kill Switch

Reconfigures Multisig ISM to null validator (`0x000...000`) on both chains, making message delivery impossible even by third-party relayers. Scales agents to 0.

### restore-job.yaml — Restore After Kill Switch

Reconfigures ISM back to real validator addresses. Operator must scale agents back up manually after signing and submitting.

### teardown-job.yaml — Full Bridge Teardown

Closes all programs, recovers rent deposits (~10-30 SOL per program), transfers funds to treasury. **DRY_RUN=true by default** — runs in read-only mode first.

### verify-ownership-job.yaml — Ownership Verification

Read-only check that all programs have correct upgrade authority (hardware wallet) and IGP account ownership (oracle wallet). No signing required.

## Operator Workflow

All jobs that modify on-chain state (kill, restore, teardown) follow the same pattern:

### 1. Edit and apply the job

```bash
# Edit env vars in the YAML first, then:
kubectl create -f ops/kill-switch-job.yaml
```

### 2. Wait for completion

```bash
kubectl wait --for=condition=complete job/kill-switch --timeout=120s
```

### 3. Copy unsigned transactions

```bash
# Find the pod name
POD=$(kubectl get pods -l job-name=kill-switch -o jsonpath='{.items[0].metadata.name}')

# Copy unsigned txs to local directory
kubectl cp ${POD}:/output/ ./unsigned-txs/
```

### 4. Review each transaction

```bash
cat unsigned-txs/*.summary.txt
```

Each `.summary.txt` describes exactly what the transaction does, which program it targets, and who must sign it.

### 5. Sign with hardware wallet

```bash
solana sign-offloaded-transaction unsigned-txs/kill-gorchain-01.json \
  --signer usb://ledger
```

The Ledger will display the transaction details for review before signing.

### 6. Submit signed transaction

```bash
solana send-signed-transaction unsigned-txs/kill-gorchain-01.json \
  --url https://your-gorchain-rpc.example.com
```

### 7. Repeat for all transaction files

Process each `.json` file in order. The `.summary.txt` files indicate the correct RPC URL for each chain.

### 8. Clean up

```bash
# Delete completed job
kubectl delete job kill-switch

# Or let it auto-clean (ttlSecondsAfterFinished: 86400 = 24h)
```

## Configuration

All jobs read program IDs from the `hyperlane-program-ids` ConfigMap created by the core deployer. Env vars are set directly in the YAML — edit before applying:

| Variable | Description | Used by |
|----------|-------------|---------|
| `GORCHAIN_RPC_URL` | Gorchain RPC endpoint | All jobs |
| `SOLANA_RPC_URL` | Solana RPC endpoint | All jobs |
| `HARDWARE_WALLET_PUBKEY` | Hardware wallet Solana pubkey | All jobs |
| `GORCHAIN_DOMAIN_ID` | Gorchain Hyperlane domain ID | kill, restore |
| `SOLANA_DOMAIN_ID` | Solana Hyperlane domain ID | kill, restore |
| `GORCHAIN_VALIDATOR_ADDRESS` | Gorchain validator H160 address | restore |
| `SOLANA_VALIDATOR_ADDRESS` | Solana validator H160 address | restore |
| `TREASURY_ADDRESS` | Address to receive recovered funds | teardown |
| `IGP_ORACLE_PUBKEY` | Privy oracle wallet pubkey | verify |
| `DRY_RUN` | `true`/`false` (default: `true`) | teardown |
| `CONFIRM_TEARDOWN` | Must be `yes` when DRY_RUN=false | teardown |
