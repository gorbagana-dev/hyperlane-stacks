# ISM Update Playbook Design (hyp-564.1)

**Status:** design captured — **implementation DEFERRED** (see below).
**Epic:** hyp-564 (operator-attended bridge maintenance ops, Ledger-signed)
**Depends on:** hyp-d9c (Ledger owns the ISM, so it can authorize `set-validators-and-threshold`); the merged client fork with built-in Ledger signing (`--keypair usb://ledger?key=0/0`).

> **Deferral note (2026-06-10).** The whole hyp-564 maintenance-ops effort — this
> playbook **and** additional-validator support (564.5/.6) — is **deferred**.
> Priority order now:
> 1. **WebSocket-based indexing** — it changes how the bridge indexes the chains,
>    so it must land *before* the bridge is deployed (not after a deployment).
> 2. **Bridge deployment**.
> 3. Maintenance ops (this work) — after both of the above.
>
> This document is the validated design only; **no code has been written** for it.
> The branch carrying this spec is safe to merge as design-only. When the work
> resumes, this spec is the starting point — pick up at the writing-plans step.
> The hyp-564 epic and its children stay **open**.

## Goal

An operator-attended, Ledger-signed Ansible playbook that sets the multisig-ISM
validator set + threshold for **one chain per run**, so validators can be added,
removed, or the threshold changed on-chain. Designed **correct-by-construction**:
the dominant silent failure (an ISM holding an H160 that no running validator
actually signs with → that validator's signatures are ignored → the bridge can
stall with no error) is made impossible to ship.

## Tech stack

Ansible (localhost/controller execution); the native `hyperlane-sealevel-client`
binary with built-in Ledger support; the committed `generated/` bridge state
(program-ids, registry); `validators.yaml` as the ISM source of truth.

---

## Background: what makes an ISM update correct (first principles)

A multisig-ISM on chain **X** verifies inbound messages whose **origin is the
other chain**. So:

- **gorchain's ISM** holds the H160s of validators that **watch solana** (they
  produce solana-origin checkpoints).
- **solana's ISM** holds the H160s of validators that **watch gorchain**.

A validator's identity in the ISM is the **secp256k1 H160 of its
checkpoint-signing key** — not its Solana pubkey, not its Privy wallet id. The
validator records that H160 on-chain when it **announces** (the
`validator-announce` program maps `H160 → storage location`) on the chain it
watches.

Two correctness hazards, both silent if mishandled:

1. **Wrong/stale/typo'd H160, or a validator not yet running.** The ISM accepts
   it structurally, but no checkpoint ever matches it, so it contributes nothing
   toward threshold. If threshold then can't be met, delivery halts — with no
   error at configure time.
2. **Cross-chain inversion.** Putting gorchain-watching validators into
   gorchain's ISM (instead of solana's) silently rejects all real traffic.

The design eliminates both rather than relying on operator care.

## What the client actually provides (verified in source, not docs)

From `rust/sealevel/client/src/multisig_ism.rs` and `main.rs`:

- `multisig-ism-message-id configure --program-id <ISM> --multisig-config-file
  <json> --registry <dir>` reads `HashMap<chain_name, {validators: Vec<H160>,
  threshold: u8}>`, resolves each `chain_name`'s domain via the registry, and
  **writes only when the on-chain set differs** (HashSet equality on validators +
  threshold equality). It is **idempotent and absolute-set**: re-running when
  already-correct sends **no transaction** (so no Ledger prompt). `Vec<H160>`
  means **malformed addresses fail to parse loudly** at the client.
- `set-validators-and-threshold` is owner-gated → the signer (the Ledger, post
  hyp-d9c the ISM owner) **must** be the owner or the tx fails on-chain.
- `validator-announce query --validator <H160> [--program-id <VA>]` is a **point
  lookup**: prints the validator's announced storage locations, or
  `"Validator not yet announced"`. This is the ground-truth oracle.
- `multisig-ism-message-id query --program-id <ISM> --domains <domain>` prints
  current owner + per-domain validators/threshold (human-readable; used only for
  the preflight display, never parsed for control flow).

---

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Source of truth = `validators.yaml`** (+ a required per-validator `address` and a top-level `ism_thresholds`). | Membership is a trust decision and must be explicit + git-reviewed; chain announcements can't be enumerated (point-lookup only), so the set can't be auto-discovered. |
| D2 | **One chain per run** via `-e target_chain=gorchain\|solana`. | One ISM = one program = one transaction = one on-device confirmation. kill-switch/restore (564.3/.4) invoke it twice. |
| D3 | **Full correctness gating** (structural inversion-proofing + on-chain announce cross-check + post-condition verify). | The user's brief: correct-by-construction. Converts both silent hazards into loud pre-flight failures and proves the result on-chain. |
| D4 | **`address` is required and on-chain-verified.** | The announce cross-check makes a typo'd value unshippable, so an explicit value is safe; required keeps the model simple. |
| D5 | **Reuse `configure`** (not `set-validators-and-threshold`). | Mirrors the proven deploy-time call exactly; one code path; gives idempotency + the no-op re-verify for free. |
| D6 | Maintenance playbooks live under **`ops/playbooks/maintenance/`**. | Separates operator-attended maintenance from bring-up/lifecycle playbooks. |

### Out of scope (noted, not built here)

- **Deploy-side** still uses single-scalar `{GORCHAIN,SOLANA}_VALIDATOR_ADDRESS`
  (hyp-564.2 unifies the deployer onto `validators.yaml`; until then, ism-update
  is the canonical multi-validator path, run post-deploy to establish the real
  set).
- **kill-switch / restore** (564.3/.4) consume this playbook; not built here.
  ism-update itself **never nulls a set** (empty remote set → hard failure); only
  kill-switch nulls.
- **Deriving H160 from Privy** — rejected: needs signing secrets on the operator
  box (violates the "no secrets on the operator machine" model). The on-chain
  announcement is the ground truth instead.

---

## Data model: `validators.yaml`

```yaml
ism_thresholds:        # keyed by the chain a validator WATCHES (the message origin)
  gorchain: 1          # M-of-N over gorchain-watching validators → consumed by SOLANA's ISM
  solana: 1            # M-of-N over solana-watching validators   → consumed by GORCHAIN's ISM
validators:
  - label: solana-primary
    chain: solana            # watches solana → its H160 belongs in gorchain's ISM
    address: "0x<40 hex>"    # secp256k1 H160 of its checkpoint-signing key (verified on-chain)
    host: bridge-host-1
    privy_wallet_id: priv_bbb
    hostname: validator-solana.bridge.gorbagana.wtf
  - label: gorchain-primary
    chain: gorchain          # watches gorchain → its H160 belongs in solana's ISM
    address: "0x<40 hex>"
    host: bridge-host-1
    privy_wallet_id: priv_aaa
    hostname: validator-gorchain.bridge.gorbagana.wtf
```

`ism_thresholds` is keyed by the **watched/origin** chain, matching how
validators group by `chain` — so for a given `remote`, both the validator set and
its threshold come from the **same** key. Pre-existing `validators.yaml` consumers
(pods, DNS, MinIO via `load_validators.yml`) ignore the new fields.

---

## Components

Each unit has one responsibility and a clear interface.

### 1. `ops/roles/common/tasks/load_validators.yml` (extend)

Add one `set_fact` exposing `ism_thresholds` from the loaded file
(`(… | from_yaml).ism_thresholds | default({})`). No other change; existing
derivations untouched.

### 2. `ops/roles/ism_update/tasks/build_desired.yml` (new — **pure, no I/O**)

The unit under test. Encodes the inversion-proof construction + all static
validation.

**Inputs:** `validators`, `ism_thresholds`, `target_chain`.
**Outputs (facts):**
- `ism_remote_chain` — the other chain.
- `ism_desired_validators` — `[{label, address}, …]` for display.
- `ism_desired_config` — `{ <ism_remote_chain>: { validators: [<addr>…], threshold: <int> } }`.

**Construction (inversion impossible by design):** one variable
`ism_remote_chain` is used as BOTH the membership filter and the JSON key:

```yaml
- set_fact:
    ism_remote_chain: "{{ 'solana' if target_chain == 'gorchain' else 'gorchain' }}"
- set_fact:
    _remote_vals: "{{ validators | selectattr('chain','equalto', ism_remote_chain) | list }}"
- set_fact:
    ism_desired_config:
      "{{ {ism_remote_chain: {'validators': _addrs, 'threshold': _thr}} }}"
  vars:
    _addrs: "{{ _remote_vals | map(attribute='address') | list }}"
    _thr: "{{ ism_thresholds[ism_remote_chain] }}"
```

**Static validation (all fail-closed `assert`s, before any signing):**
- `target_chain in ['gorchain','solana']`.
- every entry in `_remote_vals` has an `address` key (else: which label is missing).
- `_remote_vals | length >= 1` (ism-update never nulls — that's kill-switch).
- each address matches `^0x[0-9a-fA-F]{40}$`.
- addresses are unique (`_addrs | unique | length == _addrs | length`).
- `ism_thresholds[ism_remote_chain] is defined`.
- `1 <= threshold <= (_addrs | length)`.

### 3. `ops/playbooks/maintenance/ism-update.yml` (new — orchestration)

`hosts: localhost`, `connection: local`. Because it sits one level deeper than
existing playbooks, it resolves controller paths with **three** `dirname`s and
includes roles with an extra `../`:

- `validators_file` (passed to `load_validators`): `{{ playbook_dir | dirname |
  dirname | dirname }}/{{ deployment_subdir }}/bridges/{{ bridge_name
  }}/operator/validators.yaml`.
- `generated_dir`: `…/{{ deployment_subdir }}/bridges/{{ bridge_name }}/generated`.
- role include: `../../roles/common/tasks/load_validators.yml` and
  `../../roles/ism_update/tasks/build_desired.yml`.

Wrong paths surface as a loud file-not-found, never silent corruption.

Per-chain inputs via `lookup('ansible.builtin.vars', …)` over existing group_vars
(`GORCHAIN_RPC_URL`/`SOLANA_RPC_URL`, `GORCHAIN_DOMAIN_ID`/`SOLANA_DOMAIN_ID`):
- `target_rpc` = `<TARGET>_RPC_URL`, `remote_rpc` = `<REMOTE>_RPC_URL`,
  `remote_domain` = `<REMOTE>_DOMAIN_ID`.

**Pipeline:**

1. `load_validators` → `build_desired`.
2. Resolve from `{{ generated_dir }}/program-ids.json` (slurp + `from_json`):
   `ism_program_id = .[target_chain].multisig_ism_message_id`,
   `va_program_id  = .[ism_remote_chain].validator_announce`.
3. Reconstruct the registry: `mkdir -p {{ work_dir }}/chains` and copy
   `{{ generated_dir }}/registry/metadata.yaml` →
   `{{ work_dir }}/chains/metadata.yaml` (configure expects `<registry>/chains/metadata.yaml`).
4. Write `ism_desired_config | to_json` → `{{ work_dir }}/multisig-config.json`.
5. **Gate 2 — on-chain ground-truth cross-check.** For each address in
   `ism_desired_config[ism_remote_chain].validators`, run on the **remote** chain:
   ```
   {{ sealevel_client_bin }} --url {{ remote_rpc }} \
     validator-announce query --program-id {{ va_program_id }} --validator <addr>
   ```
   `failed_when: '"Validator not yet announced" in result.stdout'` (and non-zero rc).
   Any miss aborts before signing — catches typos, stale values, the inversion,
   and "validator not running yet."
6. **Preflight.** Run `multisig-ism-message-id query --program-id {{
   ism_program_id }} --domains {{ remote_domain }}` on `target_rpc` (read-only),
   `debug` its output as the current state, print `ism_desired_validators` +
   threshold, then `pause` ("Confirm the transaction on the Ledger when prompted;
   Enter to proceed, Ctrl-C to abort").
7. **Apply (Ledger-signed).**
   ```
   {{ sealevel_client_bin }} --url {{ target_rpc }} --keypair {{ ledger_keypair }} \
     multisig-ism-message-id configure \
     --program-id {{ ism_program_id }} \
     --multisig-config-file {{ work_dir }}/multisig-config.json \
     --registry {{ work_dir }}
   ```
   The Ledger screen is the real signing gate. Owner-gating means a non-owner
   Ledger fails on-chain. A declined/failed signature → non-zero rc → task fails
   (nothing partially applied: a single domain = a single instruction).
8. **Gate 3 — post-condition verify.** Re-run the exact `configure` from step 7.
   Since step 7 succeeded, on-chain == desired, so this is a pure no-op read (no
   tx, no Ledger prompt). Assert success markers and the absence of the
   change marker:
   - `assert: '"needs configuration update" not in verify.stdout'`
   - and (`"already correctly configured" in verify.stdout` or
     `"No configuration updates needed" in verify.stdout`).

### 4. `ops/tests/test_ism_update.yml` (new — CI, no hardware)

`hosts: localhost`, fixture `validators.yaml`. Includes `build_desired` and asserts:
- **Positive, `target_chain=gorchain`:** `ism_remote_chain == 'solana'`;
  `ism_desired_config.solana.validators` == the solana validators' addresses;
  `…threshold == ism_thresholds.solana`; dict shape correct.
- **Positive, `target_chain=solana`:** mirror.
- **Negative (each via `block`/`rescue`, assert the rescue fired):** threshold >
  set size; empty remote set; malformed address; missing `ism_thresholds[remote]`;
  entry missing `address`.

Pure logic only — no Ledger, no RPC. Runs under `ansible-playbook` and the CI
syntax-check.

### 5. `ops/tests/fixtures/validators.yaml` (extend)

Add `address` to each entry + an `ism_thresholds` block. Existing
`test_validators.yml` assertions are unaffected (they don't read the new fields).

### 6. `ops/inventories/{prod,staging,local}/group_vars/all.yml` (extend)

- `sealevel_client_bin` — path to the operator's native Ledger-capable
  `hyperlane-sealevel-client` (the published binary). Required for maintenance
  playbooks.
- `ledger_keypair: "usb://ledger?key=0/0"` — overridable derivation path.

RPC URLs and domain IDs already exist.

### 7. CI — `.github/workflows/ops-lint.yml`

Extend the syntax-check glob so the subfolder is covered:
`for p in playbooks/*.yml playbooks/maintenance/*.yml tests/test_*.yml`.

### 8. Docs — `docs/ops-decisions.md`

Rewrite the `ism-update.yml` row to the real design: one chain per run,
`validators.yaml` source of truth, and the three correctness gates.

---

## Inputs & path summary (RPC asymmetry — the subtle point)

| What | Source | Chain/RPC |
|---|---|---|
| ISM program-id | `generated/program-ids.json .[target].multisig_ism_message_id` | — |
| validator-announce program-id | `generated/program-ids.json .[remote].validator_announce` | — |
| registry (`--registry`) | `generated/registry/metadata.yaml` → `<work>/chains/metadata.yaml` | both chains |
| `configure` (apply + verify) | — | **target_chain RPC** (ISM lives on target) |
| `validator-announce query` | — | **remote chain RPC** (validators announce on the chain they watch) |
| preflight `query --domains` | `remote_domain` from group_vars | target_chain RPC |

`generated/` is read from the **controller's** repo checkout (operator machine on
`deploy_branch`, where `publish-bridge-state.yml` committed it) — same anchor as
`validators.yaml`.

## Error handling / edge cases

- Invalid `target_chain`, empty remote set, missing/over-large threshold,
  malformed/duplicate/absent address → fail in `build_desired` before any RPC or
  signing.
- A targeted validator not announced on the remote chain → fail at Gate 2.
- Ledger not the ISM owner, or operator declines → `configure` non-zero rc → task
  fails; atomic (single domain, single instruction).
- Already-correct ISM → step 7 is a no-op (no Ledger prompt); Gate 3 still passes.
- Fully **idempotent** and re-run-safe end to end.

## Testing strategy

- **CI (no hardware):** `test_ism_update.yml` exercises all `build_desired` logic
  and validation; `ops-lint.yml` syntax-checks the new playbook.
- **Hardware (manual / staging):** a real Ledger can't run in CI. Verify the
  apply→verify path on staging against a running deployment with a physical
  Ledger (mirrors the `test_14` e2e procedure already validated): announce
  cross-check passes, on-device confirm, Gate 3 reports no-op, and a follow-up
  `query` shows the new set.

## Keep-in-sync

- `validators.yaml` schema change touches: the file, `tests/fixtures/validators.yaml`,
  `load_validators.yml`, and (future) hyp-564.2 when the deployer adopts it.
- New maintenance playbook ⇒ `ops-lint.yml` glob + `docs/ops-decisions.md`.
