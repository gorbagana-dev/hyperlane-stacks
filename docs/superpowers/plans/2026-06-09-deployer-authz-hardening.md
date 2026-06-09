# Deployer Authorization Hardening (hyp-d9c) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move ISM and warp-route app-level ownership off the hot deployer key onto the hardware wallet (fail-closed), and gate the relayer with a menu-derived `HYP_WHITELIST` so a leaked deploy key can neither drain funds nor get rogue routes relayed.

**Architecture:** Two deploy scripts gain real `transfer-ownership` calls (the CLI commands exist at the pinned monorepo tree) and become fail-closed. A new builder script emits a Hyperlane `MatchingList` from the per-route warp program addresses; e2e (conftest) and prod (`publish-bridge-state.yml`) inject it as the relayer's `HYP_WHITELIST` env. A focused e2e module asserts the on-chain owners and the generated whitelist.

**Tech Stack:** Bash (deploy scripts, `jq`), Python/pytest (e2e), Ansible (ops), docker-compose + laconic-so specs.

**Spec:** `docs/superpowers/specs/2026-06-09-deployer-authz-hardening-design.md`

**Key constant — deny-all sentinel** (used in Tasks 3, 4): an *empty* `HYP_WHITELIST='[]'` deserializes to `MatchingList(None)` = relay-everything (verified in `matching_list.rs`). To deny all, use a non-empty rule that never matches a real message:
```
[{"recipientaddress":"0x0000000000000000000000000000000000000000000000000000000000000000"}]
```

**Verification note for shell tasks (1, 2):** these edit deploy scripts that only run inside the deployer containers during a full deploy. After each edit, run `bash -n <script>` (syntax) and `shellcheck <script>` if available. End-to-end behavior is verified by the e2e in Task 7. Do not attempt to unit-test the deploy scripts in isolation.

---

### Task 1: Core deploy.sh — ISM ownership transfer + fail-closed handoffs (hyp-d9c.1)

**Files:**
- Modify: `stack_orchestrator/data/config/deployer-scripts-config/deploy.sh` (per-chain ownership block, currently lines 269–306)

- [ ] **Step 1: Mark the pebble in progress**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
NO_COLOR=1 pb update hyp-d9c.1 --status in_progress
```

- [ ] **Step 2: Make the upgrade-authority and mailbox transfers fatal, and add the ISM transfer**

In `stack_orchestrator/data/config/deployer-scripts-config/deploy.sh`, replace this block (currently lines 269–306):

```bash
    # Transfer upgrade authority for all programs (uses solana CLI, not hyperlane)
    # JSON keys differ from binary names for IGP (igp_program_id) and ISM (multisig_ism_message_id)
    for ENTRY in mailbox:mailbox validator_announce:validator_announce interchain_gas_paymaster:igp_program_id multisig_ism_message_id:multisig_ism_message_id; do
      PROGRAM="${ENTRY%%:*}"
      JSON_KEY="${ENTRY##*:}"
      PROGRAM_ID=$(jq -r ".${JSON_KEY} // empty" "${PROGRAMS_FILE}" 2>/dev/null || true)
      if [ -n "$PROGRAM_ID" ]; then
        echo "Transferring upgrade authority for ${PROGRAM} (${PROGRAM_ID}) on ${CHAIN_OUTPUT}..."
        solana program set-upgrade-authority "$PROGRAM_ID" \
          --new-upgrade-authority "${HARDWARE_WALLET_PUBKEY}" \
          --skip-new-upgrade-authority-signer-check \
          --keypair "${DEPLOYER_KEY_FILE}" \
          --url "$RPC_URL" || echo "WARNING: Failed to transfer upgrade authority for ${PROGRAM} on ${CHAIN_OUTPUT}"
      fi
    done

    # Transfer mailbox account ownership (the only transfer-ownership we know works)
    MAILBOX_ID=$(jq -r '.mailbox // empty' "${PROGRAMS_FILE}" 2>/dev/null || true)
    if [ -n "$MAILBOX_ID" ]; then
      echo "Transferring mailbox account ownership on ${CHAIN_OUTPUT}..."
      hyperlane-sealevel-client \
        --url "$RPC_URL" \
        --keypair "${DEPLOYER_KEY_FILE}" \
        mailbox transfer-ownership \
        --program-id "$MAILBOX_ID" \
        "${HARDWARE_WALLET_PUBKEY}" \
        || echo "WARNING: mailbox transfer-ownership on ${CHAIN_OUTPUT} failed or not supported"
    fi

    # Note: core transfer-ownership does not exist in the CLI.
    # validator_announce and multisig_ism account ownership transfer commands
    # are not yet known. Skipping with a warning.
    for PROGRAM in multisig_ism_message_id validator_announce; do
      PROGRAM_ID=$(jq -r ".${PROGRAM} // empty" "${PROGRAMS_FILE}" 2>/dev/null || true)
      if [ -n "$PROGRAM_ID" ]; then
        echo "WARNING: No known account ownership transfer command for ${PROGRAM} on ${CHAIN_OUTPUT} (program-id: ${PROGRAM_ID}). Skipping."
      fi
    done
```

with (fail-closed: no `|| echo` guards — under `set -euo pipefail` any failure aborts the deploy):

```bash
    # Transfer upgrade authority for all programs (uses solana CLI, not hyperlane).
    # Fail closed: a failed handoff must abort, never leave the hot key in control.
    # JSON keys differ from binary names for IGP (igp_program_id) and ISM (multisig_ism_message_id)
    for ENTRY in mailbox:mailbox validator_announce:validator_announce interchain_gas_paymaster:igp_program_id multisig_ism_message_id:multisig_ism_message_id; do
      PROGRAM="${ENTRY%%:*}"
      JSON_KEY="${ENTRY##*:}"
      PROGRAM_ID=$(jq -r ".${JSON_KEY} // empty" "${PROGRAMS_FILE}" 2>/dev/null || true)
      if [ -n "$PROGRAM_ID" ]; then
        echo "Transferring upgrade authority for ${PROGRAM} (${PROGRAM_ID}) on ${CHAIN_OUTPUT}..."
        solana program set-upgrade-authority "$PROGRAM_ID" \
          --new-upgrade-authority "${HARDWARE_WALLET_PUBKEY}" \
          --skip-new-upgrade-authority-signer-check \
          --keypair "${DEPLOYER_KEY_FILE}" \
          --url "$RPC_URL"
      fi
    done

    # Transfer mailbox account ownership to the hardware wallet.
    MAILBOX_ID=$(jq -r '.mailbox // empty' "${PROGRAMS_FILE}" 2>/dev/null || true)
    if [ -n "$MAILBOX_ID" ]; then
      echo "Transferring mailbox account ownership on ${CHAIN_OUTPUT}..."
      hyperlane-sealevel-client \
        --url "$RPC_URL" \
        --keypair "${DEPLOYER_KEY_FILE}" \
        mailbox transfer-ownership \
        --program-id "$MAILBOX_ID" \
        "${HARDWARE_WALLET_PUBKEY}"
    fi

    # Transfer multisig-ISM account ownership to the hardware wallet.
    # validator-announce has no owner concept (Init/Announce only) — nothing to transfer.
    ISM_ID=$(jq -r '.multisig_ism_message_id // empty' "${PROGRAMS_FILE}" 2>/dev/null || true)
    if [ -n "$ISM_ID" ]; then
      echo "Transferring multisig-ISM account ownership on ${CHAIN_OUTPUT}..."
      hyperlane-sealevel-client \
        --url "$RPC_URL" \
        --keypair "${DEPLOYER_KEY_FILE}" \
        multisig-ism-message-id transfer-ownership \
        --program-id "$ISM_ID" \
        "${HARDWARE_WALLET_PUBKEY}"
    fi
```

- [ ] **Step 3: Syntax-check**

Run: `bash -n stack_orchestrator/data/config/deployer-scripts-config/deploy.sh && command -v shellcheck >/dev/null && shellcheck stack_orchestrator/data/config/deployer-scripts-config/deploy.sh || echo "shellcheck not installed; bash -n passed"`
Expected: no output from `bash -n`; shellcheck clean or skipped.

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/config/deployer-scripts-config/deploy.sh .pebbles/events.jsonl
git commit -m "$(cat <<'EOF'
fix(deployer): transfer multisig-ISM ownership to the hardware wallet

Replace the stale "no CLI command" skip with a real
multisig-ism-message-id transfer-ownership, drop the validator-announce
warning (it has no owner), and make all ownership handoffs fail-closed
(no warn-and-continue) so a failure aborts the deploy. Closes hyp-d9c.1.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Warp deploy.sh — route app-level ownership transfer + fail-closed (hyp-d9c.2)

**Files:**
- Modify: `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh` (per-chain loop, currently lines 237–260)

- [ ] **Step 1: Mark the pebble in progress**

```bash
NO_COLOR=1 pb update hyp-d9c.2 --status in_progress
```

- [ ] **Step 2: Make upgrade-authority fatal and add token transfer-ownership**

In `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh`, replace the tail of the per-chain loop (currently lines 253–260):

```bash
        echo "Transferring warp route upgrade authority on ${CHAIN_NAME}: ${PROGRAM_ID}..."
        solana program set-upgrade-authority "$PROGRAM_ID" \
          --new-upgrade-authority "${HARDWARE_WALLET_PUBKEY}" \
          --skip-new-upgrade-authority-signer-check \
          --keypair "${DEPLOYER_KEY_FILE}" \
          --url "$RPC_URL" \
          || echo "WARNING: Failed to transfer upgrade authority for warp route on ${CHAIN_NAME}"
      done
```

with (both transfers fail-closed; the `continue` guards above for a missing program-id / RPC are intentional skips and stay unchanged):

```bash
        echo "Transferring warp route upgrade authority on ${CHAIN_NAME}: ${PROGRAM_ID}..."
        solana program set-upgrade-authority "$PROGRAM_ID" \
          --new-upgrade-authority "${HARDWARE_WALLET_PUBKEY}" \
          --skip-new-upgrade-authority-signer-check \
          --keypair "${DEPLOYER_KEY_FILE}" \
          --url "$RPC_URL"

        # Transfer the Hyperlane app-level route owner (gates enroll/set-ISM/
        # set-destination-gas). Runs after warp-route deploy did its owner-gated
        # setup; only read-only queries follow. Fail closed.
        echo "Transferring warp route app-level ownership on ${CHAIN_NAME}: ${PROGRAM_ID}..."
        hyperlane-sealevel-client \
          --url "$RPC_URL" \
          --keypair "${DEPLOYER_KEY_FILE}" \
          token transfer-ownership \
          --program-id "$PROGRAM_ID" \
          "${HARDWARE_WALLET_PUBKEY}"
      done
```

- [ ] **Step 3: Syntax-check**

Run: `bash -n stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh && command -v shellcheck >/dev/null && shellcheck stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh || echo "shellcheck not installed; bash -n passed"`
Expected: no output from `bash -n`; shellcheck clean or skipped.

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh .pebbles/events.jsonl
git commit -m "$(cat <<'EOF'
fix(warp-deployer): transfer warp-route app-level ownership to hardware wallet

Add token transfer-ownership per chain after the upgrade-authority handoff
and make both fail-closed, so the deployer key no longer retains the route
owner that gates enroll/set-ISM. Closes hyp-d9c.2.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: build-relayer-whitelist.sh builder + unit tests (hyp-d9c.3, part 1)

**Files:**
- Create: `stack_orchestrator/data/config/warp-deployer-scripts-config/build-relayer-whitelist.sh`
- Modify: `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh` (invoke after build-warp-ui-config.sh, currently line 369)
- Test: `tests/unit/test_relayer_whitelist_builder.py` (pure unit test — not under `tests/e2e/`)

- [ ] **Step 1: Mark the pebble in progress**

```bash
NO_COLOR=1 pb update hyp-d9c.3 --status in_progress
```

- [ ] **Step 2: Write the failing unit tests**

Create `tests/unit/test_relayer_whitelist_builder.py`:

```python
"""Unit tests for build-relayer-whitelist.sh — pure tempdir, no cluster."""
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "stack_orchestrator/data/config/warp-deployer-scripts-config/build-relayer-whitelist.sh"
)


def _run(tmp_path, warp_routes, menu, programs):
    state = tmp_path / "state"
    routes_dir = tmp_path / "config" / "warp-routes"
    routes_dir.mkdir(parents=True)
    for stem, doc in menu.items():
        (routes_dir / f"{stem}.json").write_text(json.dumps(doc))
    for name, pids in programs.items():
        out = state / "warp-routes" / name / "warp-deploy-outputs"
        out.mkdir(parents=True)
        (out / "program-ids.json").write_text(json.dumps(pids))
    env = {
        "STATE_DIR": str(state),
        "WARP_ROUTES_DIR": str(routes_dir),
        "WARP_ROUTES": warp_routes,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True, text=True)
    return json.loads((state / "relayer-whitelist.json").read_text())


def test_unions_both_chain_program_hexes(tmp_path):
    menu = {"usdc": {"name": "USDC"}}
    programs = {"USDC": {"gorchain": {"hex": "0x" + "11" * 32}, "solana": {"hex": "0x" + "22" * 32}}}
    wl = _run(tmp_path, "usdc", menu, programs)
    assert {"recipientaddress": "0x" + "11" * 32} in wl
    assert {"recipientaddress": "0x" + "22" * 32} in wl
    assert len(wl) == 2


def test_dedupes_shared_program_across_routes(tmp_path):
    menu = {"usdc": {"name": "USDC"}, "sol": {"name": "SOL"}}
    shared = "0x" + "11" * 32
    programs = {
        "USDC": {"gorchain": {"hex": shared}, "solana": {"hex": "0x" + "22" * 32}},
        "SOL": {"gorchain": {"hex": shared}, "solana": {"hex": "0x" + "33" * 32}},
    }
    wl = _run(tmp_path, "usdc,sol", menu, programs)
    recipients = [e["recipientaddress"] for e in wl]
    assert recipients.count(shared) == 1
    assert len(wl) == 3


def test_prefixes_bare_hex_with_0x(tmp_path):
    menu = {"usdc": {"name": "USDC"}}
    programs = {"USDC": {"gorchain": {"hex": "44" * 32}, "solana": {"hex": "0x" + "55" * 32}}}
    wl = _run(tmp_path, "usdc", menu, programs)
    assert {"recipientaddress": "0x" + "44" * 32} in wl
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd tests/unit && python -m pytest test_relayer_whitelist_builder.py -v`
Expected: FAIL — the script does not exist yet (`bash: …build-relayer-whitelist.sh: No such file`).

- [ ] **Step 4: Write the builder script**

Create `stack_orchestrator/data/config/warp-deployer-scripts-config/build-relayer-whitelist.sh`:

```bash
#!/bin/bash
# Build the relayer message whitelist (relayer-whitelist.json) from the per-route
# warp program addresses the warp-deployer wrote under ${STATE_DIR}/warp-routes/<name>/.
# Emits a Hyperlane MatchingList — one {recipientaddress: 0x<hex>} rule per warp
# program (both chain sides) across exactly the routes named in WARP_ROUTES. The
# relayer loads this as HYP_WHITELIST and relays only messages delivered to a known
# warp program, defeating rogue-route relaying.
#
# Recipient-only is sufficient: on-chain enrollment already rejects spoofed senders.
# Empty result -> a deny-all sentinel (recipient = 32 zero bytes), NOT [] — an empty
# MatchingList deserializes to "no filter" (relay everything) in the agent.
set -euo pipefail

STATE_DIR="${STATE_DIR:-${STATE_OUTPUT_DIR:-/state}}"
WARP_ROUTES_DIR="${WARP_ROUTES_DIR:-/config/warp-routes}"  # must match the warp-routes mount in the warp-deployer compose
: "${WARP_ROUTES:?WARP_ROUTES must be set to a comma-separated list of route names}"

DENY_ALL='[{"recipientaddress":"0x0000000000000000000000000000000000000000000000000000000000000000"}]'

rules="[]"
for route in $(echo "${WARP_ROUTES}" | tr ',' ' '); do
  cfg="${WARP_ROUTES_DIR}/${route}.json"
  [ -s "$cfg" ] || { echo "ERROR: relayer whitelist: menu $cfg not found for route '${route}'" >&2; exit 1; }
  name=$(jq -r '.name' "$cfg")
  wpids="${STATE_DIR}/warp-routes/${name}/warp-deploy-outputs/program-ids.json"
  [ -s "$wpids" ] || { echo "ERROR: relayer whitelist: missing ${wpids} for route '${name}'" >&2; exit 1; }

  for chain in $(jq -r 'keys[]' "$wpids"); do
    hex=$(jq -r --arg c "$chain" '.[$c].hex // ""' "$wpids")
    [ -n "$hex" ] || { echo "ERROR: relayer whitelist: no hex address for ${chain} in ${wpids}" >&2; exit 1; }
    case "$hex" in 0x*) ;; *) hex="0x${hex}" ;; esac
    rules=$(jq -c --arg r "$hex" '. + [{recipientaddress:$r}]' <<<"$rules")
  done
done

whitelist=$(jq -c 'unique' <<<"$rules")
if [ "$whitelist" = "[]" ]; then
  whitelist="$DENY_ALL"
fi

echo "$whitelist" > "${STATE_DIR}/relayer-whitelist.json"
echo "Wrote ${STATE_DIR}/relayer-whitelist.json ($(jq 'length' <<<"$whitelist") rule(s))"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd tests/unit && python -m pytest test_relayer_whitelist_builder.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Invoke the builder from the warp deployer**

In `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh`, replace (currently lines 367–369):

```bash
echo ""
echo "=== Building warp-UI route config ==="
STATE_DIR="${STATE_DIR}" bash /opt/scripts/build-warp-ui-config.sh
```

with:

```bash
echo ""
echo "=== Building warp-UI route config ==="
STATE_DIR="${STATE_DIR}" bash /opt/scripts/build-warp-ui-config.sh

echo ""
echo "=== Building relayer whitelist ==="
STATE_DIR="${STATE_DIR}" bash /opt/scripts/build-relayer-whitelist.sh
```

- [ ] **Step 7: Syntax-check the deploy script**

Run: `bash -n stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh && echo OK`
Expected: `OK`

- [ ] **Step 8: Commit**

```bash
git add stack_orchestrator/data/config/warp-deployer-scripts-config/build-relayer-whitelist.sh \
        stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh \
        tests/unit/test_relayer_whitelist_builder.py
git commit -m "$(cat <<'EOF'
feat(warp-deployer): build relayer whitelist from deployed route programs

Emit a Hyperlane MatchingList (one recipientaddress rule per warp program,
both chains) for the routes in WARP_ROUTES, with a deny-all sentinel for the
empty case. Part of hyp-d9c.3.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Wire HYP_WHITELIST through compose + specs (hyp-d9c.3, part 2)

**Files:**
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml` (relayer `environment:`, currently line 21)
- Modify: `deployment/spec-relayer.yml` (config block, currently after line 12)
- Modify: `deployment/local/spec-relayer.yml` (config block, currently after line 17)
- Modify: `tests/e2e/fixtures/test-spec-relayer.yml` (config block, currently after line 14)

- [ ] **Step 1: Pass HYP_WHITELIST into the relayer container**

In `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`, change (currently lines 19–21):

```yaml
      # Gas enforcement: None — Sealevel process_estimate_costs returns
      # hardcoded zeros, making OnChainFeeQuoting non-functional
      HYP_GASPAYMENTENFORCEMENT: '[{"type": "none"}]'
```

to:

```yaml
      # Gas enforcement: None — Sealevel process_estimate_costs returns
      # hardcoded zeros, making OnChainFeeQuoting non-functional
      HYP_GASPAYMENTENFORCEMENT: '[{"type": "none"}]'
      # Relay endorsement: only messages delivered to a known warp program.
      # Built by the warp-deployer; populated into config: by conftest (e2e) /
      # publish-bridge-state (prod). Default below is deny-all (an empty []
      # would mean relay-everything).
      HYP_WHITELIST: ${HYP_WHITELIST}
```

- [ ] **Step 2: Add the deny-all default to the prod spec**

In `deployment/spec-relayer.yml`, change (currently lines 11–12):

```yaml
  GORCHAIN_IGP_ACCOUNT: "REPLACE_WITH_GORCHAIN_IGP_ACCOUNT"
  SOLANA_IGP_ACCOUNT: "REPLACE_WITH_SOLANA_IGP_ACCOUNT"
```

to:

```yaml
  GORCHAIN_IGP_ACCOUNT: "REPLACE_WITH_GORCHAIN_IGP_ACCOUNT"
  SOLANA_IGP_ACCOUNT: "REPLACE_WITH_SOLANA_IGP_ACCOUNT"
  # Relayer message whitelist (Hyperlane MatchingList). Default deny-all
  # (recipient = 32 zero bytes); publish-bridge-state.yml patches this from the
  # warp-deployer's relayer-whitelist.json. An empty [] would relay everything.
  HYP_WHITELIST: '[{"recipientaddress":"0x0000000000000000000000000000000000000000000000000000000000000000"}]'
```

- [ ] **Step 3: Add the deny-all default to the local spec**

In `deployment/local/spec-relayer.yml`, change (currently lines 16–17):

```yaml
  GORCHAIN_IGP_ACCOUNT: "REPLACE_WITH_GORCHAIN_IGP_ACCOUNT"
  SOLANA_IGP_ACCOUNT: "REPLACE_WITH_SOLANA_IGP_ACCOUNT"
```

to:

```yaml
  GORCHAIN_IGP_ACCOUNT: "REPLACE_WITH_GORCHAIN_IGP_ACCOUNT"
  SOLANA_IGP_ACCOUNT: "REPLACE_WITH_SOLANA_IGP_ACCOUNT"
  # Relayer message whitelist (Hyperlane MatchingList). Default deny-all
  # (recipient = 32 zero bytes); publish-bridge-state.yml patches this from the
  # warp-deployer's relayer-whitelist.json. An empty [] would relay everything.
  HYP_WHITELIST: '[{"recipientaddress":"0x0000000000000000000000000000000000000000000000000000000000000000"}]'
```

- [ ] **Step 4: Add the runtime placeholder to the e2e fixture**

In `tests/e2e/fixtures/test-spec-relayer.yml`, change (currently lines 13–15):

```yaml
  GORCHAIN_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"
  SOLANA_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"
  CLAIM_INTERVAL_SECONDS: "600"
```

to:

```yaml
  GORCHAIN_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"
  SOLANA_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"
  HYP_WHITELIST: "REPLACE_AT_RUNTIME"
  CLAIM_INTERVAL_SECONDS: "600"
```

- [ ] **Step 5: Sanity-check YAML parses**

Run: `cd /home/dev/git_puller/repos/hyperlane-stacks && for f in deployment/spec-relayer.yml deployment/local/spec-relayer.yml tests/e2e/fixtures/test-spec-relayer.yml stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml; do python -c "import yaml,sys; yaml.safe_load(open('$f')); print('OK $f')"; done`
Expected: `OK` for all four.

- [ ] **Step 6: Commit**

```bash
git add stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml \
        deployment/spec-relayer.yml deployment/local/spec-relayer.yml \
        tests/e2e/fixtures/test-spec-relayer.yml
git commit -m "$(cat <<'EOF'
feat(relayer): add HYP_WHITELIST config wiring with deny-all default

Pass HYP_WHITELIST into the relayer container and add the config key to the
prod/local specs (deny-all default) and a runtime placeholder to the e2e
fixture. Part of hyp-d9c.3.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Inject HYP_WHITELIST in e2e conftest (hyp-d9c.3, part 3)

**Files:**
- Modify: `tests/e2e/conftest.py` (relayer spec patch block, currently ~lines 1197–1216)

- [ ] **Step 1: Ensure `json` is imported**

Run: `grep -n "^import json" tests/e2e/conftest.py || echo "MISSING"`
If it prints `MISSING`, add `import json` with the other stdlib imports near the top of `tests/e2e/conftest.py`. Otherwise no change.

- [ ] **Step 2: Patch the whitelist into the relayer spec**

In `tests/e2e/conftest.py`, find the relayer IGP patch block and the line that writes the patched spec (currently line ~1215):

```python
    content = content.replace(
        'SOLANA_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"',
        f'SOLANA_IGP_ACCOUNT: "{solana_igp_account}"',
    )
    patched_path = E2E_DIR / ".relayer-spec-patched.yml"
    patched_path.write_text(content)
```

Insert the whitelist patch between the last `.replace(...)` and the `patched_path = …` line, so it reads:

```python
    content = content.replace(
        'SOLANA_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"',
        f'SOLANA_IGP_ACCOUNT: "{solana_igp_account}"',
    )
    # Relayer whitelist: built by the warp-deployer (build-relayer-whitelist.sh).
    # Single-quote the JSON so the embedded double quotes stay valid YAML.
    whitelist = bridge_state_loader.read_json("relayer-whitelist.json")
    content = content.replace(
        'HYP_WHITELIST: "REPLACE_AT_RUNTIME"',
        "HYP_WHITELIST: '" + json.dumps(whitelist, separators=(",", ":")) + "'",
    )
    patched_path = E2E_DIR / ".relayer-spec-patched.yml"
    patched_path.write_text(content)
```

- [ ] **Step 3: Verify conftest imports cleanly**

Run: `cd tests/e2e && python -c "import ast; ast.parse(open('conftest.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "$(cat <<'EOF'
test(e2e): inject relayer HYP_WHITELIST from deployer state

Read relayer-whitelist.json and patch it into the relayer spec before
deployment start, mirroring the IGP config patching. Part of hyp-d9c.3.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Inject HYP_WHITELIST in prod ops (hyp-d9c.3, part 4)

**Files:**
- Modify: `ops/playbooks/publish-bridge-state.yml` (after the program-ids parse, ~line 56; and a new patch task after the existing replace loop, ~line 94)

- [ ] **Step 1: Load and parse the relayer whitelist**

In `ops/playbooks/publish-bridge-state.yml`, after the `Parse program-ids.json` task (currently ends line 56), insert:

```yaml
    - name: Load relayer whitelist
      ansible.builtin.slurp:
        src: "{{ deployment_root }}/{{ generated_rel }}/relayer-whitelist.json"
      register: _whitelist_raw
      when: not ansible_check_mode

    - name: Render relayer whitelist as compact JSON
      ansible.builtin.set_fact:
        _whitelist: "{{ _whitelist_raw.content | b64decode | from_json | to_json }}"
      when: not ansible_check_mode
```

- [ ] **Step 2: Patch HYP_WHITELIST into spec-relayer.yml**

In the same file, after the `Patch core deployment-derived config keys into committed specs` task (the big `replace` loop, currently ends line 94) and before the `Stage the generated paths …` task, insert:

```yaml
    - name: Patch HYP_WHITELIST into spec-relayer.yml (single-quoted JSON scalar)
      ansible.builtin.replace:
        path: "{{ deployment_root }}/spec-relayer.yml"
        regexp: '^(\s*HYP_WHITELIST:\s*).*$'
        replace: "\\g<1>'{{ _whitelist }}'"
      when: not ansible_check_mode
```

(The existing loop double-quotes its scalar values, which would break on the
whitelist's embedded double quotes; this dedicated task single-quotes instead.
`spec-relayer.yml` is already in the `git add` argv at the staging step, so no
change is needed there.)

- [ ] **Step 3: Lint the playbook**

Run: `cd /home/dev/git_puller/repos/hyperlane-stacks && python -c "import yaml; yaml.safe_load(open('ops/playbooks/publish-bridge-state.yml')); print('OK')" && command -v ansible-lint >/dev/null && ansible-lint ops/playbooks/publish-bridge-state.yml || echo "ansible-lint not installed; YAML parse OK"`
Expected: `OK` (and ansible-lint clean if installed).

- [ ] **Step 4: Commit**

```bash
git add ops/playbooks/publish-bridge-state.yml
git commit -m "$(cat <<'EOF'
feat(ops): publish relayer HYP_WHITELIST into spec-relayer.yml

Slurp the warp-deployer's relayer-whitelist.json and patch it into the
committed relayer spec as a single-quoted JSON scalar, alongside the
existing IGP/mailbox config publishing. Closes hyp-d9c.3.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: E2E ownership + whitelist assertions

**Files:**
- Create: `tests/e2e/test_13_ownership_whitelist.py`

This module asserts post-deploy on-chain state, so it runs after all deploy fixtures. It uses the `run_deployer_cli` docker-run helper (`tests/e2e/lib/common.py:640`) and the `bridge_state_loader` fixture. `HARDWARE_WALLET_PUBKEY` is in `os.environ` (set by conftest before the deploys).

- [ ] **Step 1: Write the test module**

Create `tests/e2e/test_13_ownership_whitelist.py`:

```python
"""hyp-d9c: assert ownership moved to the hardware wallet and the relayer
whitelist covers exactly the deployed warp programs."""
import json
import os
import re

import pytest

from lib.common import run_deployer_cli

GORCHAIN_RPC = "http://localhost:8899"
SOLANA_RPC = "http://localhost:18899"
RPC = {"gorchain": GORCHAIN_RPC, "solana": SOLANA_RPC}
OWNER_RE = re.compile(r"owner:\s*Some\(\s*([1-9A-HJ-NP-Za-km-z]{32,44})")


@pytest.fixture(scope="module")
def hw_pubkey():
    pk = os.environ.get("HARDWARE_WALLET_PUBKEY")
    if not pk:
        pytest.skip("HARDWARE_WALLET_PUBKEY not set; ownership transfer not exercised")
    return pk


def _owner_from(stdout: str) -> str | None:
    m = OWNER_RE.search(stdout)
    return m.group(1) if m else None


def test_ism_owner_is_hardware_wallet(bridge_state_loader, hw_pubkey):
    for chain in ("gorchain", "solana"):
        ism = bridge_state_loader.read_program_ids(chain)["multisig_ism_message_id"]
        res = run_deployer_cli(
            "multisig-ism-message-id", "query", "--program-id", ism, rpc=RPC[chain]
        )
        assert res.returncode == 0, res.stderr
        owner = _owner_from(res.stdout)
        assert owner == hw_pubkey, f"{chain} ISM owner {owner!r} != hw {hw_pubkey!r}"


def test_route_owners_are_hardware_wallet(bridge_state_loader, hw_pubkey):
    routes = bridge_state_loader.discover_routes()
    assert routes, "no deployed warp routes found in state"
    for route in routes:
        token_config = bridge_state_loader.read_route_token_config(route)["warpRoute"]
        programs = bridge_state_loader.read_route_program_addresses(route)
        for chain, program_id in programs.items():
            token_type = token_config[chain]["type"]  # collateral|synthetic|native
            res = run_deployer_cli(
                "token", "query", "--program-id", program_id, token_type, rpc=RPC[chain]
            )
            assert res.returncode == 0, res.stderr
            owner = _owner_from(res.stdout)
            assert owner == hw_pubkey, (
                f"{route}/{chain} token owner {owner!r} != hw {hw_pubkey!r}"
            )


def test_whitelist_matches_deployed_programs(bridge_state_loader):
    whitelist = bridge_state_loader.read_json("relayer-whitelist.json")
    actual = {e["recipientaddress"] for e in whitelist}

    expected = set()
    state_dir = bridge_state_loader.state_dir
    for route in bridge_state_loader.discover_routes():
        outputs = state_dir / "warp-routes" / route / "warp-deploy-outputs"
        for f in outputs.iterdir():
            if f.is_file():
                for _chain, entry in json.loads(f.read_text()).items():
                    if isinstance(entry, dict) and entry.get("hex"):
                        h = entry["hex"]
                        expected.add(h if h.startswith("0x") else "0x" + h)

    assert expected, "no warp program hexes found in state"
    assert actual == expected, f"whitelist {actual} != deployed programs {expected}"
```

- [ ] **Step 2: Verify the module imports and collects**

Run: `cd tests/e2e && python -m pytest test_13_ownership_whitelist.py --collect-only -q`
Expected: 3 tests collected, no import errors. (Confirm the `run_deployer_cli` import path matches the style other `tests/e2e/test_*.py` use — adjust to `from lib.common import run_deployer_cli` or the project's convention if collection errors on import.)

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_13_ownership_whitelist.py
git commit -m "$(cat <<'EOF'
test(e2e): assert ISM/route ownership handoff and relayer whitelist

Query on-chain owners for the multisig-ISM and each warp route and assert
they equal the hardware wallet, and assert relayer-whitelist.json covers
exactly the deployed warp programs. Covers hyp-d9c.1/.2/.3.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Docs, keep-in-sync, and close pebbles

**Files:**
- Modify: `docs/ops-decisions.md` (Ownership Transfer section)
- Modify: `docs/architecture-decisions.md` (Key Management section)
- Modify: `CLAUDE.md` (keep-in-sync table + config patterns)

- [ ] **Step 1: Update ops-decisions.md**

In `docs/ops-decisions.md`, find the "Current implementation status — gaps tracked in pebbles `hyp-d9c`" note in the Ownership Transfer section and replace it with a closed-status note. The text must state: ISM ownership (`hyp-d9c.1`) and warp-route app-level ownership (`hyp-d9c.2`) are now transferred to the hardware wallet, all ownership handoffs are fail-closed, the relayer is gated by a menu-derived `HYP_WHITELIST` (`hyp-d9c.3`), and the deployer-key minimization remains the deliberately-untracked accepted residual. Keep the wording consistent with the surrounding section.

- [ ] **Step 2: Update architecture-decisions.md**

In `docs/architecture-decisions.md`, find the "Current implementation status (gap):" note in the Key Management (Tier 1) section and rewrite it to record that `hyp-d9c.1`/`.2`/`.3` have landed (ISM + route ownership now on the hardware wallet; relayer whitelist active), with the deployer-key minimization still the untracked accepted residual.

- [ ] **Step 3: Update the CLAUDE.md keep-in-sync table**

In `CLAUDE.md`, in the "Compose ↔ Deployment specs ↔ Test fixtures" table, the relayer row is:

```
| `compose/docker-compose-hyperlane-relayer.yml` | `deployment/spec-relayer.yml` | — |
```

Add a sentence after the table (or in the relayer-specific notes) noting that the relayer's `HYP_WHITELIST` is built by `warp-deployer-scripts-config/build-relayer-whitelist.sh` (written to `relayer-whitelist.json`), injected by conftest (e2e) and `publish-bridge-state.yml` (prod), and that an empty `[]` means relay-all so the default is a deny-all sentinel.

- [ ] **Step 4: Verify docs render (no broken markdown tables)**

Run: `cd /home/dev/git_puller/repos/hyperlane-stacks && grep -n "build-relayer-whitelist" CLAUDE.md docs/ops-decisions.md docs/architecture-decisions.md`
Expected: at least the CLAUDE.md hit; confirm the docs no longer say the gaps are open.

- [ ] **Step 5: Close the pebbles**

```bash
NO_COLOR=1 pb update hyp-d9c.1 --status closed
NO_COLOR=1 pb update hyp-d9c.2 --status closed
NO_COLOR=1 pb update hyp-d9c.3 --status closed
NO_COLOR=1 pb show hyp-d9c
```
Expected: `hyp-d9c.1/.2/.3` closed. (Leave the epic `hyp-d9c` open if the follow-ups `hyp-20e`/`hyp-405` are considered part of it; otherwise close it too.)

- [ ] **Step 6: Commit**

```bash
git add docs/ops-decisions.md docs/architecture-decisions.md CLAUDE.md .pebbles/events.jsonl
git commit -m "$(cat <<'EOF'
docs: record hyp-d9c ownership + relayer-whitelist fixes as landed

Update ops/architecture decisions and the CLAUDE.md keep-in-sync table now
that ISM/route ownership transfer and the relayer HYP_WHITELIST are in place.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification (after all tasks)

- [ ] **Run the builder unit tests:** `cd tests/unit && python -m pytest test_relayer_whitelist_builder.py -v` → 3 passed.
- [ ] **Lint:** `ruff check tests/unit/test_relayer_whitelist_builder.py tests/e2e/test_13_ownership_whitelist.py tests/e2e/conftest.py` → clean.
- [ ] **Full e2e (requires the local gorchain/solana chains + cluster):** run the suite per `specs/e2e-test-spec.md`. The deploys must stay green under the now-fatal transfers, and `test_13_ownership_whitelist.py` must pass. This is the real integration gate for Tasks 1, 2, 6.
- [ ] **Confirm no committed secrets:** `git diff main --stat` and spot-check that only program addresses (public) were added to specs — never keys.
