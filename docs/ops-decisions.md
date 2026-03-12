# Hyperlane SVM Bridge: Operations Decisions

Decisions on how the stacks should accommodate the maintenance operations from the ops-runbook.

---

## Routine Operations

### IGP Fee Claiming

**Decision:** Automated long-running sidecar.

An IGP fee claim sidecar in the `hyperlane-relayer` stack periodically claims accumulated IGP fees to the beneficiary address on both chains. It runs as a long-running container with a 6-hour sleep loop (not a CronJob). The `claim` instruction is permissionless — anyone can call it, but funds always go to the pre-configured beneficiary. The sidecar only needs a funded account to pay transaction fees (the relayer key works for this).

### Gas Oracle + Destination Gas Overhead

**Decision:** Automated long-running service using Privy oracle wallet.

The Sealevel IGP program requires the IGP account owner's signature for `set_gas_oracle_configs` — there is no separate oracle role. IGP account ownership is transferred to a dedicated Privy server wallet at deploy time (see `architecture-decisions.md` Tier 2), enabling fully automated updates.

The gas oracle is a long-running service in the `hyperlane-gas-oracle` stack that:
1. Fetches current token prices and computes updated gas oracle configs
2. Signs and submits `set_gas_oracle_configs` transactions via Privy API on both chains
3. Loops with a configurable interval (default 15 min via `GAS_ORACLE_INTERVAL_MS`)

The Privy policy engine restricts the oracle wallet to `SetGasOracleConfigs` only — `SetIgpBeneficiary` and `TransferIgpOwnership` are blocked. If the oracle key is compromised, the hardware wallet retains program upgrade authority as a recovery path.

### Agent Wallet Balance Monitoring

**Decision:** Balance monitoring with alerting.

Include a sidecar or CronJob that:
- Checks validator and relayer wallet balances on both chains
- Emits Prometheus metrics for wallet balances
- Fires alerts when balances drop below a configurable threshold

Top-ups remain manual — the operator transfers funds when alerted. No auto-funding service.

### IGP State Verification

**Decision:** Covered by monitoring.

The gas oracle service and balance monitor provide ongoing visibility into IGP state. No separate verification tooling needed.

---

## Emergency Operations

### Kill Switch

**Decision:** Full kill switch + restore jobs.

**Previous decision (superseded):** Architecture-decisions.md said "relayer kill switch (stop pod)." The ops runbook shows this is insufficient — a third-party relayer could still deliver messages using cached validator signatures.

**Updated decision:** Include two k8s Job templates:

1. **Kill job:** Scales agent deployments to 0, then reconfigures Multisig ISM on both destination chains to the null validator address (`0x0000000000000000000000000000000000000000`). Validator addresses in the Multisig ISM use H160 (20-byte Ethereum-style) format, even on Sealevel chains. This makes it impossible for any relayer to deliver messages, even with valid signatures.

2. **Restore job:** Reconfigures ISM back to the real validator addresses and scales agents back up. Messages dispatched during the pause will be delivered.

Both jobs require the ISM owner key (hardware wallet). Jobs generate unsigned transactions; the operator signs on the hardware wallet.

### Validator Key Rotation

**Decision:** Deferred to v2.

Using pre-generated 1-of-1 keys for v1. Key rotation is a manual process documented in the ops runbook but not automated in the stack.

### Mailbox ISM Swap / Debug Tools

**Decision:** Not in v1 stack scope.

These are advanced administrative operations. Operators use the CLI directly.

---

## Warp Route Management

### Enroll New Chain / Update Destination Gas / Swap ISM / Swap IGP

**Decision:** All deferred.

Only relevant when adding new chains or tokens. Out of scope for v1 single-pair bridge.

---

## Ownership & Key Management

### Ownership Transfer

**Decision:** Deployer transfers ownership to hardware wallet after deployment.

As the final step of the deployer job, ownership is transferred as follows:
- **Mailbox, ISM, Validator Announce, Token Collateral, Token Native/Synthetic** on both chains → `HARDWARE_WALLET_PUBKEY`
- **IGP account** on both chains → Privy oracle wallet (to enable automated gas oracle updates)
- **Program upgrade authority** for ALL programs (including IGP) → `HARDWARE_WALLET_PUBKEY`

The hot deployer keypair is then discarded.

A verification job in the `hyperlane-svm-ops` stack confirms all programs are owned by the hardware wallet address before the deployer key is destroyed.

**Implication:** The hardware wallet is the long-lived authority for post-deployment operations:
- Kill switch (ISM reconfiguration)
- Program upgrades (including IGP — recovery path if oracle key is compromised)
- Bridge teardown (program closure)

Gas oracle updates are handled by the Privy oracle wallet (see `architecture-decisions.md` Tier 2).

All ops jobs generate unsigned transactions; the operator signs on the hardware wallet (operator-attended).

### Operator-Attended Signing UX

**Decision:** Ops jobs output serialized unsigned transactions to files; operator signs offline with Ledger.

**Workflow:**

1. Operator runs the ops job (e.g., kill switch). The job builds the required transactions and writes serialized unsigned transactions to an output directory (k8s volume or ConfigMap).
2. Operator retrieves the unsigned transaction files from the pod/volume.
3. Operator reviews the transaction details (the job also outputs a human-readable summary of each transaction).
4. Operator signs each transaction locally using the `solana` CLI with the Ledger hardware wallet:
   ```bash
   solana sign-offloaded-transaction <unsigned-tx-file> --signer usb://ledger
   ```
5. Operator submits the signed transaction:
   ```bash
   solana send-signed-transaction <signed-tx-file> --url <RPC_URL>
   ```

**Output format per transaction:**
- `<operation>-<chain>-<seq>.json` — serialized unsigned transaction (base64)
- `<operation>-<chain>-<seq>.summary.txt` — human-readable description (program, instruction, accounts, expected effect)

**Example (kill switch):**
```
kill-gorchain-01.json          # Unsigned: scale agents to 0
kill-gorchain-01.summary.txt   # "Set ISM validators to null address on Gorchain"
kill-solana-01.json
kill-solana-01.summary.txt
```

**Hardware wallet:** Ledger with Solana app. The `solana` CLI supports `usb://ledger` as a signer for offline transaction signing.

### IGP Beneficiary

**Decision:** Set at deploy time via env var, not changed afterward.

The deployer configures the IGP beneficiary address during initial deployment. The automated CronJob claims fees to this address.

---

## Bridge Teardown

**Decision:** Full teardown job with confirmation + dry-run.

Include a k8s Job template that executes the complete ordered teardown:

1. Scale agents to 0
2. Claim remaining IGP fees on both chains
3. Close all Solana programs on both chains (recovers rent, ~10-30 SOL per program)
4. Close any orphaned deploy buffer accounts
5. Transfer remaining SOL from agent wallets to a treasury address
6. Dispose of key material (optional, operator can skip)

**Safety mechanisms:**
- **Confirmation required:** Job only proceeds if `CONFIRM_TEARDOWN=yes` is set as an environment variable
- **Dry-run mode:** Job supports `DRY_RUN=true` (default) which shows what would be done without executing. Set `DRY_RUN=false` to actually execute.

**Inputs:**
- Hardware wallet (owner/upgrade authority) — operator signs generated unsigned transactions
- Treasury address (where to send recovered funds)
- Both chain RPC URLs
- Deployment artifact paths (program-ids.json locations)

---

## Summary: Components from Ops Decisions

| Component | Stack / Location | Type |
|-----------|-----------------|------|
| IGP fee claim | `hyperlane-relayer` | Sidecar (loops every 6h) |
| Gas oracle updater | `hyperlane-gas-oracle` | Long-running pod (automated via Privy oracle wallet) |
| Wallet balance monitor | `hyperlane-monitoring` | Sidecar + Prometheus metrics |
| Kill switch job | `ops/` directory | k8s Job template (manual, operator-attended) |
| Restore job | `ops/` directory | k8s Job template (manual, operator-attended) |
| Teardown job | `ops/` directory | k8s Job template (manual, operator-attended) |

Kill/restore/teardown jobs live in `ops/` as standalone k8s Job manifests, not an SO-managed stack. Rationale:
- Different security profile: ops jobs require the hardware wallet (operator-attended signing), agents do not
- Different lifecycle: ops jobs are triggered on-demand, agents are long-running
- Clean separation of concerns

Routine operations (IGP fee claiming, wallet balance monitoring) are sidecars in their respective agent stacks. The gas oracle is its own stack with a dedicated Privy oracle wallet (IGP account owner).
