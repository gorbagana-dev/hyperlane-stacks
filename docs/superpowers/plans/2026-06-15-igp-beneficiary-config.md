# Configurable IGP Beneficiary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let operators route claimed IGP fees to a configurable, operator-controlled address (defaulting to the bridge owner) instead of the throwaway deployer key.

**Architecture:** The sealevel client already has `igp set-igp-beneficiary`, authorized by the current IGP owner. `deploy.sh` calls it (deployer-signed) right before the existing IGP ownership handoff, driven by a new `IGP_BENEFICIARY_PUBKEY` spec var that resolves to `BRIDGE_OWNER_PUBKEY` when unset. No fork change. The e2e suite already sets a dedicated beneficiary as a conftest workaround (with a TODO to move it into the deployer) — this fulfils that TODO and the workaround is retired.

**Tech Stack:** Bash (deploy.sh), laconic-so YAML specs, docker-compose, Python/pytest e2e, Ansible.

**Spec:** `docs/superpowers/specs/2026-06-15-igp-beneficiary-config-design.md`

**Branch:** `igp-beneficiary-config` (already created off `main`; the design doc is already committed there).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `stack_orchestrator/data/config/deployer-scripts-config/deploy.sh` | Core deploy orchestration | Add a dedicated per-chain set-beneficiary loop before the bridge-owner ownership loop |
| `deployment/spec-deployer.yml` | Prod deployer spec | Add `IGP_BENEFICIARY_PUBKEY` to the secrets keys block + header comment |
| `deployment/staging/spec-deployer.yml` | Staging deployer spec | Same (required for prod↔staging parity) |
| `deployment/local/spec-deployer.yml` | Local deployer spec | Same |
| `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-deployer.yml` | Deployer compose | Comment-only: list the new var among secret-injected vars |
| `tests/e2e/fixtures/test-spec-deployer.yml` | E2E deployer spec | Add `IGP_BENEFICIARY_PUBKEY` to the secrets keys block |
| `tests/e2e/test_01_deployer.py` | Deployer assertions | New test: beneficiary == dedicated keypair on both chains |
| `tests/e2e/conftest.py` | E2E fixtures | Pass `IGP_BENEFICIARY_PUBKEY` in deployer env; remove the post-deploy workaround + helper |
| `ops/inventories/{prod,staging,local}/group_vars/all.yml` | Ansible env map | Define the var + list it in the deployer stack env |
| `ops/inventories/{prod,staging,local}/deployment-config.example.yml` | Operator config template | Add `igp_beneficiary_pubkey:` with a comment |
| `specs/stack-specifications.md` | Stack docs | Document the var under the deployer stack |
| `ops/runbooks/privy-wallets.md`, `ops/runbooks/staging.md` | Runbooks | Note bridge owner is the default fee beneficiary |

---

### Task 1: E2E deployer-level beneficiary assertion (failing test)

This is the TDD anchor. It fails today: at `test_01` time `deploy.sh` has not set the beneficiary (it stays the deployer pubkey) and the conftest workaround that sets it runs only later in `bridge_setup`, which `test_01` does not depend on. It passes after Tasks 2–4.

**Files:**
- Modify: `tests/e2e/test_01_deployer.py` (add a test method to the class containing `test_igp_configured_on_chain`)

- [ ] **Step 1: Write the failing test**

Add this method directly after `test_igp_configured_on_chain` in `tests/e2e/test_01_deployer.py` (it follows the exact pattern of that method, plus a `keypairs` param like the existing tests at lines 459/507):

```python
    def test_igp_beneficiary_set_to_configured_account(
        self,
        deployer_deployment: DeploymentInfo,
        bridge_state_loader: BridgeStateLoader,
        keypairs: KeypairSet,
    ) -> None:
        """The deployer sets the IGP beneficiary to the configured address.

        IGP_BENEFICIARY_PUBKEY is wired (in conftest) to the dedicated
        igp-beneficiary keypair, so deploy.sh must set that as the on-chain
        beneficiary on both chains — proving fees no longer accrue to the
        throwaway deployer key.
        """
        expected = keypairs.igp_beneficiary_pubkey

        for chain_name, chain_info in CHAINS.items():
            program_ids = bridge_state_loader.read_program_ids(chain_name)
            result = run_deployer_cli(
                "igp", "query",
                "--program-id", program_ids["igp_program_id"],
                "--igp-account", program_ids["igp_account"],
                rpc=chain_info["rpc"],
            )
            output = result.stdout + result.stderr
            assert result.returncode == 0, (
                f"{chain_name}: IGP query failed: {output}"
            )
            match = re.search(
                r"beneficiary:\s*([1-9A-HJ-NP-Za-km-z]{32,44})", output,
            )
            assert match, (
                f"{chain_name}: could not parse beneficiary from:\n{output}"
            )
            assert match.group(1) == expected, (
                f"{chain_name}: IGP beneficiary is {match.group(1)}, "
                f"expected configured account {expected}"
            )
```

`re`, `CHAINS`, `run_deployer_cli`, `DeploymentInfo`, `KeypairSet`, and `BridgeStateLoader` are already imported at the top of the file — no new imports.

- [ ] **Step 2: Static-check the test compiles and lints**

Run: `cd tests/e2e && python -m py_compile test_01_deployer.py && ruff check test_01_deployer.py`
Expected: no errors.

- [ ] **Step 3: (Hand-off) confirm it fails on e2e**

E2E requires a kind cluster + live chains and is run by the operator / CI, not in this session. Note in the task hand-off that, run before Tasks 2–4, this assertion MUST fail with "IGP beneficiary is <deployer pubkey>, expected configured account …". Do not mark the plan complete until a full e2e run (final verification) shows it passing.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_01_deployer.py
git commit -m "test(e2e): assert deployer sets IGP beneficiary to configured account

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: deploy.sh — set the IGP beneficiary before the ownership handoff

**Files:**
- Modify: `stack_orchestrator/data/config/deployer-scripts-config/deploy.sh` (insert immediately before the `# ---` "Transfer ownership to the bridge owner" header — i.e. after the `echo "IGP gas oracle configured on both chains"` line and before `if [ -n "${BRIDGE_OWNER_PUBKEY:-}" ]; then`)

- [ ] **Step 1: Insert the set-beneficiary loop**

Find this anchor (around line 251–255):

```bash
echo "IGP gas oracle configured on both chains"

# -------------------------------------------------------
# Transfer ownership to the bridge owner
# -------------------------------------------------------
if [ -n "${BRIDGE_OWNER_PUBKEY:-}" ]; then
```

Insert the new block between the `echo` line and the `# ---` comment, so it reads:

```bash
echo "IGP gas oracle configured on both chains"

# -------------------------------------------------------
# Set the IGP fee beneficiary (defaults to the bridge owner)
# -------------------------------------------------------
# Runs while the deployer is still the IGP owner, so it must precede the IGP
# ownership handoff in the bridge-owner loop below. set-igp-beneficiary is
# authorized by the current IGP owner (the deployer key here). A dedicated loop
# (not nested in the bridge-owner loop) so an explicit beneficiary still applies
# when no bridge owner is configured.
IGP_BENEFICIARY="${IGP_BENEFICIARY_PUBKEY:-${BRIDGE_OWNER_PUBKEY:-}}"
if [ -n "$IGP_BENEFICIARY" ]; then
  echo ""
  echo "=== Setting IGP fee beneficiary to ${IGP_BENEFICIARY} ==="
  for CHAIN_OUTPUT in gorchain solana; do
    if [ "$CHAIN_OUTPUT" = "gorchain" ]; then
      RPC_URL="${GORCHAIN_RPC_URL}"
      PROGRAMS_FILE="${GORCHAIN_PROGRAMS}"
    else
      RPC_URL="${SOLANA_RPC_URL}"
      PROGRAMS_FILE="${SOLANA_PROGRAMS}"
    fi

    IGP_ID=$(jq -r '.igp_program_id // empty' "${PROGRAMS_FILE}")
    IGP_ACCOUNT=$(jq -r '.igp_account // empty' "${PROGRAMS_FILE}")
    if [ -z "$IGP_ID" ] || [ -z "$IGP_ACCOUNT" ]; then
      echo "FATAL: igp_program_id or igp_account missing from ${CHAIN_OUTPUT} program-ids.json"
      exit 1
    fi
    echo "Setting IGP beneficiary on ${CHAIN_OUTPUT} (IGP account ${IGP_ACCOUNT})..."
    hyperlane-sealevel-client \
      --url "$RPC_URL" \
      --keypair "${DEPLOYER_KEY_FILE}" \
      igp set-igp-beneficiary \
      --program-id "$IGP_ID" \
      --igp-account "$IGP_ACCOUNT" \
      "$IGP_BENEFICIARY"
  done
fi

# -------------------------------------------------------
# Transfer ownership to the bridge owner
# -------------------------------------------------------
if [ -n "${BRIDGE_OWNER_PUBKEY:-}" ]; then
```

(`GORCHAIN_PROGRAMS` / `SOLANA_PROGRAMS` and `DEPLOYER_KEY_FILE` are already defined earlier in the script; `igp set-igp-beneficiary` takes `new_beneficiary` as a positional arg.)

- [ ] **Step 2: Syntax + lint check**

Run: `cd stack_orchestrator/data/config/deployer-scripts-config && bash -n deploy.sh && shellcheck -S warning deploy.sh`
Expected: `bash -n` clean. `shellcheck` shows no NEW findings vs. before the edit (the new block mirrors the existing IGP-ownership block, which already passes; pre-existing warnings elsewhere are out of scope).

- [ ] **Step 3: Commit**

```bash
git add stack_orchestrator/data/config/deployer-scripts-config/deploy.sh
git commit -m "feat(deployer): set IGP beneficiary to configured address before ownership handoff

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Spec + compose plumbing

**Files:**
- Modify: `deployment/spec-deployer.yml`
- Modify: `deployment/staging/spec-deployer.yml`
- Modify: `deployment/local/spec-deployer.yml`
- Modify: `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-deployer.yml`
- Modify: `tests/e2e/fixtures/test-spec-deployer.yml`

- [ ] **Step 1: Add the key to all three deployer specs**

In each of `deployment/spec-deployer.yml`, `deployment/staging/spec-deployer.yml`, `deployment/local/spec-deployer.yml`, add a line to the `hyperlane-deployer-secrets` keys block, right after the `IGP_ORACLE_PUBKEY` line. In `deployment/spec-deployer.yml` it becomes:

```yaml
      BRIDGE_OWNER_PUBKEY:        { env: BRIDGE_OWNER_PUBKEY }
      IGP_ORACLE_PUBKEY:          { env: IGP_ORACLE_PUBKEY }
      IGP_BENEFICIARY_PUBKEY:     { env: IGP_BENEFICIARY_PUBKEY }
      GORCHAIN_VALIDATOR_ADDRESS: { env: GORCHAIN_VALIDATOR_ADDRESS }
```

Use the existing per-file indentation/alignment (staging and local use single-space alignment `IGP_ORACLE_PUBKEY: { env: IGP_ORACLE_PUBKEY }` — match each file's own style; do not reformat the others).

Also extend the header comment in each spec (the `#   BRIDGE_OWNER_PUBKEY, IGP_ORACLE_PUBKEY,` line) to include the new var:

```
#   BRIDGE_OWNER_PUBKEY, IGP_ORACLE_PUBKEY, IGP_BENEFICIARY_PUBKEY,
```

- [ ] **Step 2: Add the same key to the e2e fixture spec**

In `tests/e2e/fixtures/test-spec-deployer.yml`, after the `IGP_ORACLE_PUBKEY` line in the `hyperlane-deployer-secrets` keys block:

```yaml
      IGP_ORACLE_PUBKEY: { env: IGP_ORACLE_PUBKEY }
      IGP_BENEFICIARY_PUBKEY: { env: IGP_BENEFICIARY_PUBKEY }
      GORCHAIN_VALIDATOR_ADDRESS: { env: GORCHAIN_VALIDATOR_ADDRESS }
```

- [ ] **Step 3: Extend the compose comment (no `environment:` entry)**

In `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-deployer.yml`, update the secret-injected-vars comment (lines 18–19) to list the new var. The operator pubkeys are injected purely via `envFrom.secretRef`; do NOT add an `environment:` line. Result:

```yaml
      # Key management + SOLANA_RPC_URL — injected via secrets: in spec.yml
      # (envFrom.secretRef), so they're not repeated in this environment block.
      # DEPLOYER_KEYPAIR, BRIDGE_OWNER_PUBKEY, IGP_ORACLE_PUBKEY,
      # IGP_BENEFICIARY_PUBKEY, GORCHAIN_VALIDATOR_ADDRESS,
      # SOLANA_VALIDATOR_ADDRESS, SOLANA_RPC_URL
```

- [ ] **Step 4: Lint + parity check**

Run:
```bash
yamllint deployment/spec-deployer.yml deployment/staging/spec-deployer.yml deployment/local/spec-deployer.yml tests/e2e/fixtures/test-spec-deployer.yml stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-deployer.yml
python3 ops/scripts/check-spec-parity.py
```
Expected: yamllint clean; `Spec shape parity OK: <N> specs match.`

- [ ] **Step 5: Commit**

```bash
git add deployment/spec-deployer.yml deployment/staging/spec-deployer.yml deployment/local/spec-deployer.yml tests/e2e/fixtures/test-spec-deployer.yml stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-deployer.yml
git commit -m "feat(deployer): thread IGP_BENEFICIARY_PUBKEY through specs and compose

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Wire the e2e deployer env and retire the conftest workaround

**Files:**
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Pass IGP_BENEFICIARY_PUBKEY into the deployer env**

In the `os.environ.update({...})` block that sets `DEPLOYER_KEYPAIR`/`BRIDGE_OWNER_PUBKEY`/… (around line 713), add the new key after `IGP_ORACLE_PUBKEY`:

```python
        "DEPLOYER_KEYPAIR":           keypairs.deployer_keypair,
        "BRIDGE_OWNER_PUBKEY":        keypairs.owner_pubkey,
        "IGP_ORACLE_PUBKEY":          keypairs.igp_oracle_pubkey,
        "IGP_BENEFICIARY_PUBKEY":     keypairs.igp_beneficiary_pubkey,
        "GORCHAIN_VALIDATOR_ADDRESS": keypairs.gorchain_validator_address,
        "SOLANA_VALIDATOR_ADDRESS":   keypairs.solana_validator_address,
        "SOLANA_RPC_URL":             "http://solana-rpc:18899",
```

- [ ] **Step 2: Remove the post-deploy workaround block from `bridge_setup`**

In the `bridge_setup` fixture, delete the entire "Change IGP beneficiary on both chains" block (the comment through the `for chain in ("gorchain", "solana"):` loop that calls `_set_igp_beneficiary`) — currently:

```python
    # Change IGP beneficiary on both chains from deployer → dedicated account.
    # The beneficiary keypair is generated and funded during initial test setup
    # (keygen.py). Without this, the deployer is both fee payer and beneficiary,
    # making fee collection invisible to fee claim tests.
    # TODO: add an ops job/playbook to configure the IGP beneficiary address
    # for production deployments (deployment/ops/).
    beneficiary_pubkey = subprocess.run(
        ["solana-keygen", "pubkey", str(KEYS_DIR / "igp-beneficiary.json")],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # IGP account ownership is transferred to the igp-oracle key during
    # core deployment. set-igp-beneficiary requires the owner's signature.
    igp_oracle_keypair = str(KEYS_DIR / "igp-oracle.json")
    log.info("Setting IGP beneficiary to %s...", beneficiary_pubkey)
    for chain in ("gorchain", "solana"):
        program_ids = bridge_state_loader.read_program_ids(chain)
        _set_igp_beneficiary(
            rpc=CHAINS[chain]["rpc"],
            program_id=program_ids["igp_program_id"],
            igp_account=program_ids["igp_account"],
            new_beneficiary=beneficiary_pubkey,
            chain=chain,
            owner_keypair=igp_oracle_keypair,
        )
```

Replace it with a one-line comment so the intent is documented at the call site:

```python
    # IGP beneficiary is set by the deployer Job at deploy time
    # (IGP_BENEFICIARY_PUBKEY → keypairs.igp_beneficiary_pubkey); the dedicated
    # keygen account is still funded so fee-claim tests observe a balance bump.
```

- [ ] **Step 3: Remove the now-unused `_set_igp_beneficiary` helper**

Delete the entire `def _set_igp_beneficiary(...)` function (the helper defined just above the `bridge_setup` fixture) — Step 2 removed its only caller. Confirm zero remaining references:

Run: `cd tests/e2e && grep -rn "_set_igp_beneficiary" . ; echo "exit: $?"`
Expected: no matches (grep exit 1).

- [ ] **Step 4: Compile + lint (catches now-unused imports)**

Run: `cd tests/e2e && python -m py_compile conftest.py && ruff check conftest.py`
Expected: no errors. If `ruff` flags `subprocess` or another symbol as now-unused (the deleted block used `subprocess.run`), confirm it is genuinely unused elsewhere in the file before removing the import — `grep -n "subprocess" conftest.py` — and remove only if it has no other use.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "test(e2e): set IGP beneficiary via deployer env, drop post-deploy workaround

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Ansible env map + operator config template

**Files:**
- Modify: `ops/inventories/prod/group_vars/all.yml`
- Modify: `ops/inventories/staging/group_vars/all.yml`
- Modify: `ops/inventories/local/group_vars/all.yml`
- Modify: `ops/inventories/prod/deployment-config.example.yml`
- Modify: `ops/inventories/staging/deployment-config.example.yml`
- Modify: `ops/inventories/local/deployment-config.example.yml`

- [ ] **Step 1: Define the var and list it in the deployer stack env (all 3 inventories)**

In each `group_vars/all.yml`, add the var definition right after the `IGP_ORACLE_PUBKEY: "{{ igp_oracle_pubkey }}"` line:

```yaml
IGP_ORACLE_PUBKEY: "{{ igp_oracle_pubkey }}"
IGP_BENEFICIARY_PUBKEY: "{{ igp_beneficiary_pubkey | default('') }}"
```

The `| default('')` keeps it optional — empty string makes `deploy.sh` fall back to `BRIDGE_OWNER_PUBKEY`.

Then add it to the `hyperlane-svm-deployer:` list under `stack_env_vars:`, after `IGP_ORACLE_PUBKEY`:

```yaml
  hyperlane-svm-deployer:
    - BRIDGE_OWNER_PUBKEY
    - IGP_ORACLE_PUBKEY
    - IGP_BENEFICIARY_PUBKEY
    - GORCHAIN_VALIDATOR_ADDRESS
    - SOLANA_VALIDATOR_ADDRESS
    - SOLANA_RPC_URL
    - GHCR_PAT
```

(The local inventory's `hyperlane-svm-deployer` list may be ordered differently — insert `IGP_BENEFICIARY_PUBKEY` after `IGP_ORACLE_PUBKEY` in whatever the file's existing list is. Do NOT add it to `hyperlane-svm-warp-deployer` — the warp deployer doesn't touch the IGP.)

- [ ] **Step 2: Add the operator-config key with a comment (all 3 example files)**

In each `deployment-config.example.yml`, after the `igp_oracle_pubkey:` line:

```yaml
igp_oracle_pubkey: ""            # oracle.json `address` (base58)
igp_beneficiary_pubkey: ""       # optional — IGP fee beneficiary; defaults to bridge_owner_pubkey if blank
```

(Match each file's existing comment-alignment style.)

- [ ] **Step 3: Lint + ansible env-contract test**

Run:
```bash
yamllint ops/inventories/prod/group_vars/all.yml ops/inventories/staging/group_vars/all.yml ops/inventories/local/group_vars/all.yml ops/inventories/prod/deployment-config.example.yml ops/inventories/staging/deployment-config.example.yml ops/inventories/local/deployment-config.example.yml
cd ops && ansible-playbook tests/test_stack_env.yml tests/test_env_contract.yml --syntax-check
```
Expected: yamllint clean; syntax-check OK. If a full local run of the env-contract tests is cheap on this host, run them too; otherwise hand off the assertion that the deployer stack's spec secrets now match `stack_env_vars`.

- [ ] **Step 4: Commit**

```bash
git add ops/inventories/prod/group_vars/all.yml ops/inventories/staging/group_vars/all.yml ops/inventories/local/group_vars/all.yml ops/inventories/prod/deployment-config.example.yml ops/inventories/staging/deployment-config.example.yml ops/inventories/local/deployment-config.example.yml
git commit -m "feat(ops): expose igp_beneficiary_pubkey in inventories and deployment config

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Documentation

**Files:**
- Modify: `specs/stack-specifications.md`
- Modify: `ops/runbooks/privy-wallets.md`
- Modify: `ops/runbooks/staging.md`

- [ ] **Step 1: Document the var under the deployer stack spec**

Locate the deployer stack section: `grep -n "Stack 1\|svm-deployer\|## .*[Dd]eployer" specs/stack-specifications.md`. In the deployer stack's config/env documentation, add a paragraph:

```markdown
**IGP fee beneficiary.** The deployer sets the InterchainGasPaymaster beneficiary
(the account that `igp claim` pays accumulated gas fees to) via the optional
`IGP_BENEFICIARY_PUBKEY`. It is applied on both chains by `deploy.sh`
(`igp set-igp-beneficiary`, deployer-signed) immediately before IGP ownership is
handed to the oracle wallet — so the deployer must still be the IGP owner at that
point. When unset it defaults to `BRIDGE_OWNER_PUBKEY`; if neither is set the
beneficiary stays the deployer key (pre-existing behavior). The base IGP account
carries the beneficiary; the overhead IGP has none and is untouched.
```

- [ ] **Step 2: Note the default in the Privy wallets doc**

In `ops/runbooks/privy-wallets.md`, in the bridge-owner row/description and the "Funding" section, note that the bridge owner is also the **default IGP fee beneficiary**. Add to the Funding section's bridge-owner bullet:

```markdown
- The **bridge-owner wallet** signs nothing during deployment (it only receives
  ownership) and is the **default IGP fee beneficiary** (where claimed gas fees
  land unless `igp_beneficiary_pubkey` overrides it) — no funding needed until
  maintenance ops or fee withdrawals start signing with it.
```

(Replace the existing bridge-owner Funding bullet.)

- [ ] **Step 3: Note it in the staging funding table**

In `ops/runbooks/staging.md`, in the signer funding table, update the "Privy bridge owner" row note from `— (transfer target only)` to `— (transfer target + default fee beneficiary)`.

- [ ] **Step 4: Lint the markdown**

Run: `yamllint -d '{rules: {line-length: disable}}' ops/runbooks/privy-wallets.md 2>/dev/null || true` (markdown isn't yamllint-checked; instead just `grep -n "IGP_BENEFICIARY_PUBKEY\|fee beneficiary" specs/stack-specifications.md ops/runbooks/privy-wallets.md ops/runbooks/staging.md` to confirm the edits landed).
Expected: the new strings appear in all three files.

- [ ] **Step 5: Commit**

```bash
git add specs/stack-specifications.md ops/runbooks/privy-wallets.md ops/runbooks/staging.md
git commit -m "docs: document configurable IGP fee beneficiary

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Update auto-memory

**Files:**
- Modify: `/home/dev/.claude/projects/-home-dev-git-puller-repos-hyperlane-stacks/memory/project_igp_beneficiary_open_question.md`
- Modify: `/home/dev/.claude/projects/-home-dev-git-puller-repos-hyperlane-stacks/memory/MEMORY.md` (pointer line)

- [ ] **Step 1: Rewrite the memory to reflect the resolution**

The "IGP beneficiary open question" is now resolved. Update the memory file body to state: the IGP beneficiary is configurable via `IGP_BENEFICIARY_PUBKEY` (spec secrets → `deploy.sh igp set-igp-beneficiary`, set before the ownership handoff), defaulting to `BRIDGE_OWNER_PUBKEY`; the e2e conftest workaround was retired in favor of the deployer doing it. Update the `MEMORY.md` pointer hook accordingly (e.g. "resolved: IGP beneficiary now configurable, defaults to bridge owner").

- [ ] **Step 2: Commit (memory is outside the repo — no git commit needed)**

Memory files live under `~/.claude/...`, not the repo. No commit; just save the files.

---

## Final Verification (hand-off to operator / CI)

- [ ] **Whole-branch lint + parity (runnable here):**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
python3 ops/scripts/check-spec-parity.py
bash -n stack_orchestrator/data/config/deployer-scripts-config/deploy.sh
cd tests/e2e && ruff check . && python -m py_compile conftest.py test_01_deployer.py
```

- [ ] **Full e2e (operator / CI — not this session):** run the e2e suite against a kind cluster. Confirm `test_01_deployer.py::*::test_igp_beneficiary_set_to_configured_account` PASSES on both chains and `test_10_fee_claim.py` still passes. Per the standing constraint, e2e/stacks are not run on this shared dev machine — hand the operator the exact suite command.

- [ ] **Review the diff** for the keep-in-sync groups (CLAUDE.md "Keep in sync — CRITICAL"): every spec change has its compose/fixture/group_vars counterpart.

---

## Self-Review

**Spec coverage:** deploy.sh set-beneficiary (Task 2 ✓); resolution default-to-bridge-owner (Task 2 ✓); single shared address — same var both chains (Task 2 loop ✓); spec/compose/fixture plumbing (Task 3 ✓); parity requirement prod+staging (Task 3 Step 4 ✓); e2e migration of the existing workaround (Task 4 ✓); e2e assertion (Task 1 ✓); ansible (Task 5 ✓); docs (Task 6 ✓); memory + out-of-scope existing-staging note (Task 7 / design ✓).

**Placeholder scan:** no TBD/TODO; every code step shows full content; commands have expected output.

**Type/name consistency:** env var `IGP_BENEFICIARY_PUBKEY` and ansible var `igp_beneficiary_pubkey` used consistently; shell var `IGP_BENEFICIARY`; e2e attr `keypairs.igp_beneficiary_pubkey` matches `lib/keygen.py`; test method name consistent between Task 1 and Final Verification.
