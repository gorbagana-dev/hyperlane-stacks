# Hyperlane SVM Bridge: Operations Decisions

Decisions on how the stacks should accommodate the maintenance operations from the ops-runbook.

---

## Ops Architecture: Operator-Layer Ops + Built-in Ledger Signing

**Decision (2026-05-29, supersedes the 2026-05-28 "atomic ops as suspended SO
jobs" plan):** All operator-attended on-chain operations run from the
**ansible/operator layer**, not from an in-cluster SO stack. The forked
`hyperlane-sealevel-client` gains **built-in Ledger support**, so each operation
**signs on the Ledger and broadcasts in one step**, run natively on the
operator's machine. There is **no `hyperlane-ops` SO stack**, no unsigned-tx
artifact, no scp round-trip, and no `submit-tx`.

Full rationale, investigation findings (file:line), and the locked decisions live
in **`docs/superpowers/specs/2026-05-29-ops-layer-redesign-and-ledger-signing-design.md`**.
This section is the summary; that spec is authoritative.

### Why the pivot

The old model didn't actually work as drawn. For the ops commands that matter
(`set-validators-and-threshold`, `transfer-ownership`, IGP, close-program), the
client's `--write-instructions` flag is wired only for the warp-route-deploy path
(`router.rs:311`); everything else falls into a `wait_for_user_confirmation()`
that reads stdin (`context.rs:393`) and **hangs/panics in a headless k8s Job**.
Meanwhile the entire Ledger signing stack (`solana-clap-utils` →
`solana-remote-wallet` → `hidapi`, all @ 3.0.x) is **already in the client's
dependency tree**, and the signer abstraction already returns `Box<dyn Signer>`.
So built-in Ledger support is a small, contained change that deletes the whole
unsigned-tx-artifact + custom-signer workstream.

### Signing model

- The operator runs the forked client natively with `--keypair usb://ledger…`.
  It builds the tx, fetches a fresh blockhash, prompts the operator **on the
  Ledger screen** (the review gate), signs, and broadcasts to the public RPC —
  one step.
- **No secrets on the operator machine.** The Ledger holds the only key; state
  and config come from the repo; both RPCs are public.
- Fee payer == authority in the client (single `owner_payer`), so no hot
  fee-payer key is needed anywhere.
- Distributed as **prebuilt binaries on GitHub Releases**. See sub-project 1 in
  the design spec.

### Composite playbooks (inventory + ordering unchanged; signing mechanism updated)

Each playbook orchestrates lifecycle changes + on-chain operations. Where a step
needs an on-chain transaction, the playbook invokes the Ledger client on
`localhost` (which signs + broadcasts in one step) — replacing the old
"`run-job` emits unsigned tx → scp → operator signs → `submit-tx`" sequence.

| Composite playbook | Steps |
|---|---|
| `kill-switch.yml` | `deployment stop` agents → set ISM validators to null per chain (Ledger client) → done. |
| `restore.yml` | Set ISM validators to the real set per chain (Ledger client) → `deployment start` agents. |
| `ism-update.yml` | Set ISM `(validators, threshold)` for one chain (Ledger client). Used for add-validator-to-ISM, remove-validator-from-ISM, threshold change. |
| `teardown.yml` | `deployment stop` agents → loop {claim IGP fees per chain} → loop {close program} → close orphan buffers → loop {transfer SOL per wallet} → optional key disposal. Each on-chain step signs on the Ledger; operator-attended throughout. |
| `add-validator.yml` | Interactive: generates `spec-validator-<label>.yml` + updates `validators.yaml`. Human gate (commit + PR + merge). Then: distribute-credentials → configure-dns → minio-resync → deploy-validator (`stack_deploy`). On-chain ISM add is a separate `ism-update.yml` run. |
| `remove-validator.yml` | Pre-flight: `ism-update.yml` must have detached the validator from ISM. Then: `deployment stop` → interactive spec deletion + `validators.yaml` edit + commit gate → MinIO IAM cleanup (deletes user, keeps bucket) → remove-dns. |
| `verify-ownership.yml` | Read-only: confirms all programs are owned by the hardware-wallet address. No signing. |

### Why operator-layer (not in-cluster SO jobs)

- **It's where the Ledger is.** Signing is USB-HID; the device is on the
  operator's machine, never in a remote k8s node. In-cluster jobs can't reach it.
- **One auditable command, on-device review.** No artifact passing, no parsing a
  base58 blob — the operator confirms the actual tx on the Ledger.
- **Teardown is a multi-step attended flow** — sequencing, pauses, and per-step
  retry belong in a playbook, not a shell script inside a k8s Job.
- The `laconic.suspend` + `run-job` SO feature (merged) is now **latent
  infrastructure** with no v1 consumer; kept for future use.

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

**Decision:** Composite `kill-switch.yml` playbook orchestrating agent scale-down + ISM reconfiguration per chain via the Ledger client.

The kill switch makes the bridge un-deliverable by reconfiguring the on-chain Multisig ISM to the null validator address (`0x0000000000000000000000000000000000000000`, H160 format) on both destination chains. Stopping the relayer alone is insufficient — a third-party relayer could still deliver messages using cached validator signatures. The on-chain ISM reconfiguration is what actually blocks delivery.

**Flow:**

1. `laconic-so deployment stop` on the relayer + all validator stacks (scale agents to 0).
2. On the controller (`localhost`), once per destination chain, the playbook runs the Ledger client `set-validators-and-threshold` with `VALIDATORS=null_address` and `THRESHOLD=1`. The operator confirms each tx on the Ledger; the client signs and broadcasts.

The kill-switch is in effect once step 2 completes for both chains. Validators and relayer stay stopped until `restore.yml` is run.

### Restore

**Decision:** Composite `restore.yml` playbook — symmetric to kill-switch.

1. On the controller, per destination chain, the playbook runs the Ledger client `set-validators-and-threshold` with the real `VALIDATORS` list (from `validators.yaml`) and operator-supplied `THRESHOLD`. Operator confirms on the Ledger; client signs and broadcasts.
2. `laconic-so deployment start` on validator stacks + relayer.

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
3. On the controller (`localhost`), runs the Ledger client `set-validators-and-threshold` with the resolved `(validators, threshold)` and `--keypair usb://ledger…`.
4. Operator reviews the tx on the Ledger screen and confirms; the client signs and broadcasts in one step.

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

The `verify-ownership.yml` playbook (read-only, runs the client on the controller) confirms all programs are owned by the hardware wallet address before the deployer key is destroyed.

**Implication:** The hardware wallet is the long-lived authority for post-deployment operations:
- Kill switch (ISM reconfiguration)
- Program upgrades (including IGP — recovery path if oracle key is compromised)
- Bridge teardown (program closure)

Gas oracle updates are handled by the Privy oracle wallet (see `architecture-decisions.md` Tier 2).

Operator-attended on-chain operations are signed directly on the Ledger by the forked client (see below) — no unsigned-tx artifacts.

**Current implementation status (as of `hyp-d9c`):**

The deploy scripts now realize this decision:

- **Multisig-ISM ownership (`hyp-d9c.1`) is transferred** to the hardware wallet by `deployer-scripts-config/deploy.sh` via `multisig-ism-message-id transfer-ownership`. A failed transfer aborts the deploy (fail-closed).
- **Warp-route app-level ownership (`hyp-d9c.2`) is transferred** to the hardware wallet by `warp-deployer-scripts-config/deploy.sh` via `token transfer-ownership` as the final step of each route deployment. A failed transfer aborts the deploy (fail-closed).
- **The hot deployer key is a long-lived cluster secret** (`spec-warp-deployer.yml` → `~/.credentials/hyperlane/deployer-keypair.json`), re-injected each run. Minimizing it (JIT/ephemeral or Ledger-attended) is **deliberately not tracked**: once `.1`–`.3` have landed, a leaked deploy key can neither drain funds nor get rogue routes relayed, so the simpler hot-key workflow is kept.

**Route relay authorization (`hyp-d9c.3`):** The relayer is gated by a menu-derived `HYP_WHITELIST` built from the deployed routes' program addresses by `warp-deployer-scripts-config/build-relayer-whitelist.sh` (written to `relayer-whitelist.json` in state). An empty `[]` would relay everything, so the default is a deny-all sentinel — a single rule whose recipient is 32 zero bytes (`[{"recipientaddress":"0x000…000"}]`), which no real message matches. The whitelist is injected by conftest (e2e) and `publish-bridge-state.yml` (prod). Adding a new route to the relayer requires a git-reviewed config change.

### Operator-Attended Signing UX

**Decision (2026-05-29):** The forked `hyperlane-sealevel-client` has **built-in
Ledger support**. Composite playbooks run it on the controller (`localhost`) with
`--keypair usb://ledger…`; it builds the tx, prompts the operator on the Ledger
screen, signs, and broadcasts — one step, no artifact, no `submit-tx`. See
`docs/superpowers/specs/2026-05-29-ops-layer-redesign-and-ledger-signing-design.md`
for the full design and the investigation findings behind it.

**Per-op flow:**

1. Composite playbook on the controller runs the client locally, e.g.
   `hyperlane-sealevel-client … multisig-ism-message-id set-validators-and-threshold --keypair usb://ledger?key=0/0 …`.
2. The client fetches a fresh blockhash and presents the transaction to the
   Ledger.
3. **Operator reviews the transaction details on the Ledger device screen** and
   approves with the device button — this *is* the review gate.
4. The client attaches the signature and broadcasts to the public RPC, reporting
   the tx hash.

**Hardware wallet:** Ledger with the Solana app, attached to the operator's
machine; udev rules on Linux. Same prerequisites as standard `solana` CLI Ledger
use.

**Why operator-layer (not in-cluster SO jobs):** the Ledger is USB-HID on the
operator's machine and never reachable from a remote k8s node; on-device review
beats parsing a base58 blob; no artifact-passing or duplicate broadcast plumbing.
The client runs as a **native prebuilt binary** (GitHub Releases) — `docker run`
USB passthrough is Linux-only/flaky on macOS, and operators already run a native
`solana` CLI for Ledger work.

### IGP Beneficiary

**Decision:** Set at deploy time via env var, not changed afterward.

The deployer configures the IGP beneficiary address during initial deployment. The automated CronJob claims fees to this address.

---

## Bridge Teardown

**Decision:** Composite `teardown.yml` playbook orchestrating on-chain operations in a fixed sequence with operator-attended signing pauses between steps.

Teardown is fundamentally a multi-step playbook — each step requires the hardware wallet for one or more transactions, ordering matters (you can't transfer SOL out of a wallet after disposing the key), and operators may want to pause mid-flow.

**Sequence:**

| # | Step | Signing |
|---|---|---|
| 1 | Stop all agents (`deployment stop`: relayer, validators, gas-oracle) | None |
| 2 | Claim remaining IGP fees per chain (Ledger client) | Per-chain tx — operator confirms on Ledger |
| 3 | Close all Solana programs per chain (~7 programs × 2 chains, Ledger client) | Per-tx — operator confirms each |
| 4 | Close orphan deploy buffer accounts (Ledger client) | Per-tx — operator confirms each |
| 5 | Transfer remaining SOL from agent wallets (Ledger client) | Per-tx — operator confirms each |
| 6 | Dispose of key material (operator-local, optional) | n/a |
| 7 | Stop remaining stacks (`deployment stop`: minio, monitoring, warp-ui) | None |

**Safety mechanisms:**

- **Dry-run by default.** `teardown.yml` runs with `DRY_RUN=true` unless explicitly set to false. In dry-run mode, the playbook prints each intended operation and its parameters but does not invoke the Ledger client to broadcast.
- **Per-step confirmation.** Between each signing step, the playbook pauses for operator confirmation before proceeding to the next.
- **Per-step retry.** If a step fails, the playbook can be resumed from that step (idempotent: on-chain ops are no-ops if state already matches).
- **`CONFIRM_TEARDOWN=yes` extra-var required** to actually execute.

**Inputs:**
- Hardware wallet (owner/upgrade authority) — operator confirms each tx on the Ledger
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

### Operator-attended (ansible playbooks invoking the Ledger client on the controller)

| Composite playbook | On-chain operations | Hardware-wallet signing |
|---|---|---|
| `kill-switch.yml` | Set ISM to null per chain | Yes (on Ledger) |
| `restore.yml` | Set ISM to real set per chain | Yes (on Ledger) |
| `ism-update.yml` (general) | Set ISM `(validators, threshold)` | Yes (on Ledger) |
| `teardown.yml` | Claim IGP fees, close programs, close buffers, transfer SOL | Yes (per step, on Ledger) |
| `add-validator.yml` | None (deployment-side only); on-chain ISM addition runs as separate `ism-update.yml` | No |
| `remove-validator.yml` | ISM detach (via `ism-update.yml`) first → `deployment stop` → MinIO IAM cleanup → DNS removal | Yes (for ISM detach) |
| `verify-ownership.yml` | Read-only ownership check | No |

### Signing mechanism

All on-chain operations run the forked `hyperlane-sealevel-client` on the
controller (`localhost`) with `--keypair usb://ledger…`. The client builds the
tx, the operator confirms it on the Ledger screen, and the client signs and
broadcasts in one step. There is no `hyperlane-ops` SO stack, no unsigned-tx
artifact, and no `submit-tx`. Built-in Ledger support, the binary-release
distribution, and the full rationale are specified in
`docs/superpowers/specs/2026-05-29-ops-layer-redesign-and-ledger-signing-design.md`
(sub-project 1). The `laconic.suspend` + `run-job` SO feature remains available
as latent infrastructure with no v1 consumer.
