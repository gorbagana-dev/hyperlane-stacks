# Hyperlane SVM Bridge: Operations Decisions

Decisions on how the stacks should accommodate the maintenance operations from the ops-runbook.

---

## Ops Architecture: Atomic Ops + Composite Playbooks

**Decision (2026-05-28, replaces the "k8s Job templates under `ops/`" plan):** All operator-attended operations are split into two layers — small **atomic ops** that produce one type of unsigned transaction each, and **composite ansible playbooks** that orchestrate sequences of atomic ops plus SO lifecycle ops plus operator-attended signing pauses.

### Atomic ops

All atomic ops live in a single `hyperlane-ops` SO stack. Each action is declared as a separate Job in the stack's `jobs:` list with `suspend: true` so it does not auto-run at `deployment start`. The operator triggers individual actions with `laconic-so deployment run-job <name>`. Each `run-job` invocation creates a fresh k8s Job (timestamped name), allowing repeated runs.

| Atomic op | Purpose |
|---|---|
| `ism-update` | Generate unsigned `set_validators` tx for one chain (params: chain, validators list, threshold). Used by kill-switch, restore, add-validator-to-ISM, remove-validator-from-ISM, threshold change. |
| `claim-igp-fees` | Claim IGP fees on one chain. |
| `close-program` | Close one Solana program (recovers rent). Params: program ID, chain. |
| `close-buffer` | Close one orphan deploy buffer account. |
| `transfer-sol` | Transfer SOL out of one account to another. |
| `verify-ownership` | Read-only: confirms all programs are owned by `HARDWARE_WALLET_PUBKEY`. No signing. |
| `submit-tx` | Broadcast a signed transaction (params: signed-tx file path, chain). |

All share the deployer image (has `hyperlane-sealevel-client` + Solana CLI), the same secrets block (deployer keypair, hardware wallet pubkey), and the same state-file mounts (via `state_distribute` role / `BridgeStateLoader`). Each writes outputs to `/srv/kind/hyperlane/ops/<action>-<timestamp>/` on the host. Composite playbooks scp those outputs back to the controller.

### Composite playbooks

Composite playbooks orchestrate the operator-attended flows. Each is a thin ansible playbook that:
1. Invokes one or more atomic ops via `laconic-so deployment run-job` on the appropriate host.
2. Retrieves the unsigned-tx outputs via scp.
3. Pauses for operator to sign with the Ledger hardware wallet.
4. Invokes `submit-tx` to broadcast signed transactions.
5. Performs any pod-lifecycle changes (`laconic-so deployment stop`, `start`, etc.).

| Composite playbook | Atomic ops + lifecycle ops |
|---|---|
| `kill-switch.yml` | `deployment stop` agents → `ism-update` with null addresses (per chain) → operator signs → `submit-tx`. |
| `restore.yml` | `ism-update` with real validator addresses (per chain) → operator signs → `submit-tx` → `deployment start` agents. |
| `ism-update.yml` | `ism-update` atomic op with (validators, threshold) inputs → operator signs → `submit-tx`. Used for add-validator-to-ISM, remove-validator-from-ISM, threshold change. |
| `teardown.yml` | `deployment stop` agents → loop {`claim-igp-fees` per chain} → loop {`close-program` per program} → `close-buffer` for orphans → loop {`transfer-sol` per wallet} → optional key disposal. Pauses for operator signing between each unsigned-tx-producing step. |
| `add-validator.yml` | Interactive: generates `spec-validator-<label>.yml` + updates `validators.yaml`. Human gate (commit + PR + merge). Then: distribute-credentials → configure-dns → minio-resync → deploy-validator (`stack_deploy`). |
| `remove-validator.yml` | Pre-flight: `ism-update` must have detached the validator from ISM. Then: `deployment stop` → interactive spec deletion + `validators.yaml` edit + commit gate → MinIO IAM cleanup (deletes user, keeps bucket) → remove-dns. |
| `submit-signed-tx.yml` | Thin wrapper around `submit-tx` for one-off signed-tx broadcasts. |

### SO enhancement

The `suspend: true` + `run-job` mechanism is being added to stack-orchestrator as part of this work (see `architecture-decisions.md` §Stack Decomposition). Until that lands, the `hyperlane-ops` stack cannot exist as a single SO stack; the v1 architecture above is the target end-state. No intermediate model is shipped.

### Why this split

- **Atomic ops are testable.** Each is a single Job with one well-defined output. Easy to unit-test the on-chain logic.
- **Composite playbooks are operator-facing.** Sequencing, pauses, and operator review live at the playbook level, not buried in a shell script inside a k8s Job.
- **Teardown is fundamentally a multi-step playbook.** It's a sequence of conditional signing operations with ordering constraints — not a single Job that emits a giant batch of unsigned txs. Splitting it into atomic ops orchestrated by a playbook matches what the operation actually is.

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

**Decision:** Composite `kill-switch.yml` playbook orchestrating agent scale-down + atomic `ism-update` per chain.

The kill switch makes the bridge un-deliverable by reconfiguring the on-chain Multisig ISM to the null validator address (`0x0000000000000000000000000000000000000000`, H160 format) on both destination chains. Stopping the relayer alone is insufficient — a third-party relayer could still deliver messages using cached validator signatures. The on-chain ISM reconfiguration is what actually blocks delivery.

**Flow:**

1. `laconic-so deployment stop` on the relayer + all validator stacks (scale agents to 0).
2. `laconic-so deployment run-job ism-update` on the deployer host, once per destination chain, with `VALIDATORS=null_address` and `THRESHOLD=1`. Each invocation writes an unsigned tx to `/srv/kind/hyperlane/ops/ism-update-<chain>-<timestamp>/`.
3. Ansible scp's the unsigned txs back to the controller.
4. Operator signs each tx with the Ledger hardware wallet:
   ```
   solana sign-offloaded-transaction <unsigned-tx>.json --signer usb://ledger
   ```
5. `laconic-so deployment run-job submit-tx` on the deployer host, once per signed tx, to broadcast.

The kill-switch is in effect once step 5 completes. Validators and relayer stay stopped until `restore.yml` is run.

### Restore

**Decision:** Composite `restore.yml` playbook — symmetric to kill-switch.

1. `laconic-so deployment run-job ism-update` per destination chain with the real `VALIDATORS` list (from `validators.yaml`) and operator-supplied `THRESHOLD`. Writes unsigned txs.
2. Operator signs.
3. `submit-tx` broadcasts.
4. `laconic-so deployment start` on validator stacks + relayer.

Messages dispatched during the pause are processed once agents are back online (Hyperlane delivers from on-chain history, not from in-memory queues).

### Validator Key Rotation

**Decision:** Use the add-validator + remove-validator flows.

The semantically clean rotation = "add new validator with new key → ISM update to include both old and new → wait for confirmations to clear → ISM update to remove old → stop old validator". Each step uses the existing playbooks. Key rotation is not a special operation in v1.

For 1-of-1 deployments, this is a controlled split-second window where threshold needs to be temporarily 2 (both validators required to agree); operators can also accept the simpler "stop old → add new → ISM swap" with a brief delivery pause.

### Mailbox ISM Swap / Debug Tools

**Decision:** Not in v1 stack scope.

These are advanced administrative operations. Operators use the CLI directly.

---

## Validator Lifecycle

The "add a validator" and "remove a validator" flows are operator-facing GitOps workflows. Each splits into two phases: a deployment-side change (handled by ansible + git) and an on-chain ISM update (operator-attended hardware-wallet signing). The two phases are deliberately separate playbooks: the deployment side is config-only and reversible; the on-chain side is the hard step.

### Add Validator

**Decision:** Two playbooks with a human gate (commit + PR + merge) between them.

**Phase 1 — generate the spec (`generate-validator-spec.yml`, interactive, runs on controller):**

Operator prompts:
- `label?` — operator-assigned identifier (e.g. `gorchain-backup`)
- `chain?` — `gorchain` or `solana`
- `host?` — inventory host alias
- `privy_wallet_id?` — operator must have already created the Privy server wallet manually in the Privy UI
- `hostname?` — default `validator-<label>.bridge.<zone>`; operator can override; playbook checks for conflicts against existing `http-proxy.host-name:` entries

Outputs (all in the working tree, no git operations):
- **NEW:** `deployment/spec-validator-<label>.yml` — rendered from the validator spec template
- **MODIFIED:** `deployment/bridges/<bridge>/operator/validators.yaml` — append entry
- **Implicit:** the `MINIO_USERS` env-var value (derived from `validators.yaml` at spec-render time) gains the new label

Playbook displays the diff, prints "review, commit, push, open PR, merge", and exits.

**Human gate.** Operator reviews on their fork, commits, opens a PR, merges to main. Pulls main on the controller.

**Phase 2 — deploy the validator. Five sub-steps, idempotent and safe to re-run:**

1. `distribute-validator-credentials.yml -e validator_label=<label> -e generate=true`
   - Generates a MinIO IAM cred pair (`openssl rand -hex 24`) on the controller.
   - Drops `~/.credentials/hyperlane/<label>-minio.{key_id,secret}` on the validator host.
   - Patches `minio-validator-secrets` Secret on the MinIO host (adds `<LABEL>_KEY_ID` / `<LABEL>_SECRET`).
   - Idempotent: if files already exist with sensible contents, no-op.
2. `configure-dns.yml` — adds the new `validator-<label>.bridge.<zone>` A record (additive reconciliation).
3. `minio-resync.yml` — triggers the existing `minio-provision` CronJob on the MinIO host (`kubectl create job --from=cronjob/minio-provision`). New bucket + IAM user + policy appear for the label. MinIO pod does not restart.
4. `deploy-validator.yml -e validator_label=<label>` — invokes `stack_deploy` role with `spec_file: deployment/spec-validator-<label>.yml`. Pre-flight: DNS resolves, host has docker + laconic-so + cluster. Then `laconic-so deploy create && deployment start`. SO brings up the pod; it announces its address on-chain via `validatorAnnounce`.
5. Validator is now running, signing checkpoints to its MinIO bucket. **But its checkpoints are not counted toward the on-chain ISM yet** — that's the next phase, which is operator-attended and run when the operator is ready.

**Phase 3 — add to on-chain ISM (`ism-update.yml`, operator-attended, hardware wallet):**

Run separately from phase 2. Operator decides when and at what threshold.

```
$ ansible-playbook playbooks/ism-update.yml -e chain=gorchain -e threshold=2
```

Reads `validators.yaml` for the current set, generates unsigned `set_validators` tx, operator signs with Ledger, broadcasts. See §ISM Update below.

### Remove Validator

**Decision:** Strict ordering — ISM update first (detach on-chain), then stop the pod, then bookkeeping.

**Why this ordering:** If you stop the pod before detaching from ISM, the validator's address still appears in the on-chain ISM but no longer publishes signatures, degrading signature aggregation (relayer can't reach the m-of-n threshold). The on-chain detach must happen first.

**Sequence:**

1. **Detach from ISM:** `ism-update.yml -e chain=<chain> -e remove_label=<label> -e threshold=<new>`. Generates unsigned `set_validators` tx without the removed validator, operator signs, broadcasts.
2. **Stop the pod:** `laconic-so deployment stop --delete-volumes` on the validator stack. Volume deletion is destructive; ansible prompts for confirmation. RocksDB state is wiped.
3. **Bookkeeping (interactive `remove-validator.yml`, runs on controller):**
   - Removes entry from `validators.yaml`.
   - Deletes `deployment/spec-validator-<label>.yml`.
   - Pre-flight: checks that the label is not in the current on-chain ISM (queries chain). Refuses to proceed if still present.
   - Human gate: commit + PR + merge.
4. **MinIO IAM cleanup:** patches `minio-validator-secrets` to remove `<LABEL>_KEY_ID` / `<LABEL>_SECRET`, triggers `minio-provision` CronJob to delete the user + policy.
   - **The bucket is kept** for audit trail. Operator can delete it manually if desired.
5. **DNS removal:** `remove-dns.yml -e hostname=<host>`. Explicit playbook, additive removal of the validator-specific A record.

### ISM Update (general)

**Decision:** Single composite playbook for all "update on-chain ISM" operations — add, remove, threshold change, kill-switch, restore. They all use the same `set_validators` instruction with different `(validators, threshold)` arguments.

```
$ ansible-playbook playbooks/ism-update.yml \
    -e chain=gorchain \
    -e threshold=2
```

Optional extra-vars:
- `add_label=<label>` — include this label's address in addition to the current set
- `remove_label=<label>` — exclude this label's address from the current set
- `validators_override=[…]` — bypass `validators.yaml`; supply explicit addresses (used by kill-switch with null address)

**Flow:**

1. Reads `validators.yaml` for the current set (or `validators_override` if supplied).
2. Resolves each label's secp256k1 address by querying Privy via `privy_wallet_id`.
3. Triggers `laconic-so deployment run-job ism-update` on the deployer host with the resolved `(validators, threshold)`.
4. scp's the unsigned tx + summary back to the controller.
5. Operator reviews the summary, signs with Ledger.
6. Triggers `laconic-so deployment run-job submit-tx` to broadcast.

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

**Decision:** Atomic ops jobs in the `hyperlane-ops` SO stack write serialized unsigned transactions to a host-path output directory. Composite ansible playbooks scp the outputs back to the controller; the operator signs locally with Ledger; the playbook broadcasts via the `submit-tx` atomic op.

**Per-op flow:**

1. Composite playbook on controller invokes `laconic-so deployment run-job <op>` on the deployer host with the relevant inputs (chain, validators list, threshold, etc.).
2. SO creates a fresh k8s Job (timestamped name) from the suspended job template. The Job runs the atomic op, which writes outputs to `/srv/kind/hyperlane/ops/<op>-<timestamp>/` on the host.
3. Playbook scp's the output directory back to the controller's `.ops/<bridge>/<op>-<timestamp>/`.
4. Playbook prints a summary and pauses for operator review.
5. Operator signs each `.json` with Ledger:
   ```bash
   solana sign-offloaded-transaction <unsigned-tx-file>.json --signer usb://ledger
   ```
6. Operator confirms; playbook scp's signed files back to the deployer host and invokes `laconic-so deployment run-job submit-tx` once per signed file.

**Output format per transaction (same as before):**
- `<operation>-<chain>-<seq>.json` — serialized unsigned transaction (base64)
- `<operation>-<chain>-<seq>.summary.txt` — human-readable description (program, instruction, accounts, expected effect)

**Example (kill switch, gorchain):**
```
.ops/hyperlane-main/ism-update-2026-06-01T12-34-56/
  ism-update-gorchain-01.json          # Unsigned: set ISM validators to null
  ism-update-gorchain-01.summary.txt
```

**Hardware wallet:** Ledger with Solana app. The `solana` CLI supports `usb://ledger` as a signer for offline transaction signing.

**Why SO Jobs (not raw `kubectl apply`):** Atomic ops share the same image, secrets, state-file mounts, and namespace as every other stack. Putting them in the SO `hyperlane-ops` stack means they use the same `secrets:` mechanism (file/env credential injection), the same state-file distribution (via `state_distribute` role), and the same Caddy-fronted ingress rules as everything else. No duplicate plumbing.

### IGP Beneficiary

**Decision:** Set at deploy time via env var, not changed afterward.

The deployer configures the IGP beneficiary address during initial deployment. The automated CronJob claims fees to this address.

---

## Bridge Teardown

**Decision:** Composite `teardown.yml` playbook orchestrating atomic ops in a fixed sequence with operator-attended signing pauses between steps.

Teardown is fundamentally a multi-step playbook — each step requires the hardware wallet for one or more transactions, ordering matters (you can't transfer SOL out of a wallet after disposing the key), and operators may want to pause mid-flow. Bundling it into one k8s Job that emits a giant batch of unsigned txs is the wrong shape: operators couldn't pause, couldn't selectively retry one step, and couldn't sanity-check intermediate state.

**Sequence:**

| # | Step | Atomic op | Signing |
|---|---|---|---|
| 1 | Stop all agents | `deployment stop` (relayer, validators, gas-oracle) | None |
| 2 | Claim remaining IGP fees | `claim-igp-fees` per chain | Per-chain tx — operator signs |
| 3 | Broadcast claims | `submit-tx` per signed file | None (signed already) |
| 4 | Close all Solana programs | `close-program` per program per chain (~7 programs × 2 chains) | Per-tx — operator signs each |
| 5 | Broadcast program closures | `submit-tx` per signed file | None |
| 6 | Close orphan deploy buffer accounts | `close-buffer` per buffer | Per-tx — operator signs each |
| 7 | Broadcast buffer closures | `submit-tx` per signed file | None |
| 8 | Transfer remaining SOL from agent wallets | `transfer-sol` per wallet | Per-tx — operator signs each |
| 9 | Broadcast SOL transfers | `submit-tx` per signed file | None |
| 10 | Dispose of key material | Operator-local (optional) | n/a |
| 11 | Stop remaining stacks | `deployment stop` (minio, monitoring, warp-ui) | None |

**Safety mechanisms:**

- **Dry-run by default.** `teardown.yml` runs with `DRY_RUN=true` unless explicitly set to false. In dry-run mode, atomic ops emit unsigned txs and summaries but the playbook skips the `submit-tx` step.
- **Per-step confirmation.** Between each signing step, the playbook prompts for operator confirmation before scp'ing the next unsigned tx batch back.
- **Per-step retry.** If a signing or broadcast step fails, the playbook can be resumed from that step (idempotent: atomic ops re-emit the same unsigned tx if state already matches).
- **`CONFIRM_TEARDOWN=yes` extra-var required** to actually execute (matches the previous design's gate).

**Inputs:**
- Hardware wallet (owner/upgrade authority) — operator signs each unsigned tx via Ledger
- Treasury address (where to send recovered funds)
- Both chain RPC URLs (from spec / `validators.yaml`)
- `program-ids.json` (from `deployment/bridges/<bridge>/generated/`)

---

## Summary: Components from Ops Decisions

### Routine (automated)

| Component | Stack / Location | Type |
|---|---|---|
| IGP fee claim | `hyperlane-relayer` | Sidecar (loops every 6h) |
| Gas oracle updater | `hyperlane-gas-oracle` | Long-running pod (automated via Privy oracle wallet) |
| Wallet balance monitor | `hyperlane-monitoring` | Sidecar + Prometheus metrics |

### Operator-attended (composite ansible playbooks + atomic SO Jobs)

| Composite playbook | Atomic ops invoked | Hardware-wallet signing |
|---|---|---|
| `kill-switch.yml` | `ism-update` per chain → `submit-tx` | Yes |
| `restore.yml` | `ism-update` per chain → `submit-tx` | Yes |
| `ism-update.yml` (general) | `ism-update` → `submit-tx` | Yes |
| `teardown.yml` | `claim-igp-fees`, `close-program`, `close-buffer`, `transfer-sol` (each → `submit-tx`) | Yes (per step) |
| `add-validator.yml` | None (deployment-side only); on-chain ISM addition runs as separate `ism-update.yml` | No |
| `remove-validator.yml` | `ism-update` (detach first) → `deployment stop` → MinIO IAM cleanup → DNS removal | Yes (for ISM detach) |
| `submit-signed-tx.yml` | `submit-tx` | No (already signed) |
| `verify-ownership.yml` | `verify-ownership` (read-only) | No |

### Atomic ops (all in the `hyperlane-ops` SO stack, suspended jobs, triggered via `laconic-so deployment run-job`)

`ism-update`, `claim-igp-fees`, `close-program`, `close-buffer`, `transfer-sol`, `verify-ownership`, `submit-tx`.

**Rationale for the SO-managed atomic ops + ansible-orchestrated composite pattern:**
- Atomic ops share image, secrets, state-file mounts, and namespace with the rest of the deployment. Same `secrets:` mechanism, same `state_distribute` role.
- Composite playbooks own the operator-attended flow logic (sequencing, signing pauses, retries). That logic doesn't belong in a shell script inside a k8s Job.
- Each atomic op is small and testable. Each composite playbook is auditable as a sequence of atomic ops.
