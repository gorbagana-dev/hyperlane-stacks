# Balance Monitoring + Slack Alerting & Configurable Intervals — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make balance monitoring generic (native + multiple SPL tokens per account, across both chains) with direct Slack-webhook alerting, and make the IGP fee-claim and gas-oracle loop intervals operator-settable from the deployment config.

**Architecture:** The existing long-running `balance-monitor` loop container in the `hyperlane-monitoring` stack is rewritten to read a single generated `watches.json` (mounted via a new runtime-populated `balance-monitor-config` ConfigMap) and POST low-balance alerts to a Slack webhook (anti-spam, in-memory state). The metrics path (gauge → Pushgateway → Grafana panel) is removed. ops renders `watches.json` from the bridge's own signers (addresses gathered cross-host via non-secret `.pub` files + config vars) with per-role thresholds from deployment-config; operators add extra/SPL watches by editing the generated file and `laconic-so … restart`. Intervals are surfaced through the existing `spec_token_renders` path.

**Tech Stack:** Python 3.12 stdlib (monitor + pytest unit tests), docker-compose (SO compose→k8s), laconic-so specs, Ansible (ops), pytest (e2e).

**Design spec:** `docs/superpowers/specs/2026-06-16-balance-monitoring-and-intervals-design.md`

**Branch:** `balance-monitoring-and-intervals` (already checked out).

**Conventions used below:**
- `repo` = `/home/dev/git_puller/repos/hyperlane-stacks`.
- Ansible lint/syntax commands must be prefixed with `export LC_ALL=C.UTF-8 LANG=C.UTF-8` (locale fix) and run from `repo/ops`.
- **Do not run the e2e suite or live deployments on this shared machine.** Static checks only; the full e2e run is handed off to the user at the end (see "Handoff").

---

## Phase 1 — Configurable intervals (Feature 2, independent)

### Task 1: Gas-oracle interval token

Make `GAS_ORACLE_INTERVAL_MS` operator-settable from deployment-config via `spec_token_renders`, defaulting to today's `900000` (15 min). e2e fixtures keep the literal (e2e doesn't render via ops).

**Files:**
- Modify: `deployment/spec-gas-oracle.yml:26`
- Modify: `deployment/staging/spec-gas-oracle.yml:26`
- Modify: `deployment/local/spec-gas-oracle.yml:25`
- Modify: `ops/inventories/prod/group_vars/all.yml` (`spec_token_renders:` block, ~line 64)
- Modify: `ops/inventories/staging/group_vars/all.yml` (`spec_token_renders:` block, ~line 68)
- Modify: `ops/inventories/local/group_vars/all.yml` (`spec_token_renders:` block, ~line 149)
- Modify: `ops/inventories/prod/deployment-config.example.yml`
- Modify: `ops/inventories/staging/deployment-config.example.yml`
- Modify: `ops/inventories/local/deployment-config.example.yml`

- [ ] **Step 1: Tokenize the three deployment specs**

In each of the three `spec-gas-oracle.yml` files, change the literal line:

```yaml
  GAS_ORACLE_INTERVAL_MS: "900000"
```

to:

```yaml
  GAS_ORACLE_INTERVAL_MS: "__GAS_ORACLE_INTERVAL_MS__"
```

(Leave `tests/e2e/fixtures/test-spec-gas-oracle.yml` untouched — it keeps a literal.)

- [ ] **Step 2: Add the render token to all three `spec_token_renders` blocks**

In each `group_vars/all.yml`, inside the existing `spec_token_renders:` mapping, add:

```yaml
  # Gas-oracle loop interval (ms); operator override, default 15 min.
  __GAS_ORACLE_INTERVAL_MS__: "{{ gas_oracle_interval_ms | default('900000') }}"
```

- [ ] **Step 3: Document the knob in all three deployment-config examples**

Add, under a new `# --- Tuning (optional) ---` section near the end of each
`deployment-config.example.yml` (create the section if absent; commented out so the
default applies):

```yaml
# --- Tuning (optional; defaults shown) ---
# gas_oracle_interval_ms: "900000"     # gas-oracle price-update loop interval (15 min)
```

- [ ] **Step 4: Verify the render is well-formed (dry static check)**

Confirm no spec still carries a bare literal and the token is present:

Run: `cd repo && grep -rn "GAS_ORACLE_INTERVAL_MS" deployment/`
Expected: the three `deployment/**/spec-gas-oracle.yml` show `"__GAS_ORACLE_INTERVAL_MS__"`; `tests/e2e/fixtures/test-spec-gas-oracle.yml` (if listed) shows the literal.

Run: `cd repo/ops && export LC_ALL=C.UTF-8 LANG=C.UTF-8 && ansible-lint inventories/prod/group_vars/all.yml inventories/staging/group_vars/all.yml inventories/local/group_vars/all.yml`
Expected: 0 failures (warnings about the vendored `collections/` are pre-existing and unrelated).

- [ ] **Step 5: Commit**

```bash
cd repo
git add deployment/spec-gas-oracle.yml deployment/staging/spec-gas-oracle.yml \
        deployment/local/spec-gas-oracle.yml \
        ops/inventories/prod/group_vars/all.yml ops/inventories/staging/group_vars/all.yml \
        ops/inventories/local/group_vars/all.yml \
        ops/inventories/prod/deployment-config.example.yml \
        ops/inventories/staging/deployment-config.example.yml \
        ops/inventories/local/deployment-config.example.yml
git commit -m "gas-oracle: make GAS_ORACLE_INTERVAL_MS configurable from deployment config

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Fee-claim interval token

Make `CLAIM_INTERVAL_SECONDS` (the `igp-fee-claim` relayer sidecar) operator-settable, defaulting to `21600` (6 h). It is currently only a commented-out line in the relayer specs.

**Files:**
- Modify: `deployment/spec-relayer.yml:30`
- Modify: `deployment/staging/spec-relayer.yml:30`
- Modify: `deployment/local/spec-relayer.yml` (the `config:` block, ~line 26 — add the key; no commented line exists there)
- Modify: `ops/inventories/{prod,staging,local}/group_vars/all.yml` (`spec_token_renders:`)
- Modify: `ops/inventories/{prod,staging,local}/deployment-config.example.yml`

- [ ] **Step 1: Tokenize the relayer specs**

In `deployment/spec-relayer.yml` and `deployment/staging/spec-relayer.yml`, replace the commented line (currently line 30):

```yaml
  # CLAIM_INTERVAL_SECONDS: "21600"  # IGP fee claim interval (default: 6h)
```

with an active token line:

```yaml
  CLAIM_INTERVAL_SECONDS: "__CLAIM_INTERVAL_SECONDS__"  # IGP fee claim interval (default 6h)
```

In `deployment/local/spec-relayer.yml`, add the same active line inside the `config:`
block (after the last existing `config:` entry, before `configmaps:`):

```yaml
  CLAIM_INTERVAL_SECONDS: "__CLAIM_INTERVAL_SECONDS__"  # IGP fee claim interval (default 6h)
```

(Leave `tests/e2e/fixtures/test-spec-relayer.yml` untouched.)

- [ ] **Step 2: Add the render token to all three `spec_token_renders` blocks**

In each `group_vars/all.yml` `spec_token_renders:` mapping, add:

```yaml
  # IGP fee-claim interval (seconds); operator override, default 6 h.
  __CLAIM_INTERVAL_SECONDS__: "{{ fee_claim_interval_seconds | default('21600') }}"
```

- [ ] **Step 3: Document the knob in all three deployment-config examples**

Under the `# --- Tuning (optional; defaults shown) ---` section added in Task 1:

```yaml
# fee_claim_interval_seconds: "21600"  # IGP fee-claim loop interval (6 h)
```

- [ ] **Step 4: Static check**

Run: `cd repo && grep -rn "CLAIM_INTERVAL_SECONDS" deployment/`
Expected: the three `deployment/**/spec-relayer.yml` show `"__CLAIM_INTERVAL_SECONDS__"` as an active (uncommented) key.

Run: `cd repo/ops && export LC_ALL=C.UTF-8 LANG=C.UTF-8 && ansible-lint inventories/prod/group_vars/all.yml inventories/staging/group_vars/all.yml inventories/local/group_vars/all.yml`
Expected: 0 failures.

- [ ] **Step 5: Commit**

```bash
cd repo
git add deployment/spec-relayer.yml deployment/staging/spec-relayer.yml \
        deployment/local/spec-relayer.yml \
        ops/inventories/prod/group_vars/all.yml ops/inventories/staging/group_vars/all.yml \
        ops/inventories/local/group_vars/all.yml \
        ops/inventories/prod/deployment-config.example.yml \
        ops/inventories/staging/deployment-config.example.yml \
        ops/inventories/local/deployment-config.example.yml
git commit -m "relayer: make CLAIM_INTERVAL_SECONDS configurable from deployment config

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 2 — Balance monitor rewrite (Feature 1 core)

### Task 3: Rewrite `check-balance.py` (watches.json, native + SPL, Slack, anti-spam)

Replace the env-var/Pushgateway monitor with a `watches.json`-driven monitor that posts low-balance alerts to Slack. Pure, testable functions for the alert decision and message building; a unit test exercises them with a fake balance function (no cluster, no network).

**Files:**
- Modify (full rewrite): `stack_orchestrator/data/config/balance-monitor-scripts-config/check-balance.py`
- Create: `tests/e2e/test_balance_monitor_unit.py`

- [ ] **Step 1: Write the failing unit test**

Create `tests/e2e/test_balance_monitor_unit.py`:

```python
"""Unit tests for the balance-monitor script (no cluster/network).

Imports the script by path (it ships as a ConfigMap file, not a package) and
exercises the pure functions: the alert-decision state machine and the Slack
message builders.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "stack_orchestrator/data/config/balance-monitor-scripts-config/check-balance.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("check_balance", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cb():
    return _load_module()


def _watches():
    return [
        {"chain": "solana", "label": "relayer", "address": "AAA",
         "tokens": [{"symbol": "SOL", "mint": "native", "threshold": 5.0},
                    {"symbol": "USDC", "mint": "MINT", "threshold": 100.0}]},
    ]


def test_breach_then_no_repeat_then_repeat(cb):
    state = {}
    # SOL below (1.0 < 5.0), USDC ok (200 >= 100)
    def bal(chain, addr, mint):
        return 1.0 if mint == "native" else 200.0

    breaches, recoveries = cb.evaluate_cycle(_watches(), bal, state, now=0.0, repeat=100.0)
    assert [b["symbol"] for b in breaches] == ["SOL"]
    assert recoveries == []

    # Still low, within repeat window → no new breach
    breaches, _ = cb.evaluate_cycle(_watches(), bal, state, now=50.0, repeat=100.0)
    assert breaches == []

    # Still low, past repeat window → re-alert
    breaches, _ = cb.evaluate_cycle(_watches(), bal, state, now=150.0, repeat=100.0)
    assert [b["symbol"] for b in breaches] == ["SOL"]


def test_recovery_emitted_once(cb):
    state = {}
    low = lambda c, a, m: 1.0 if m == "native" else 200.0
    cb.evaluate_cycle(_watches(), low, state, now=0.0, repeat=100.0)

    high = lambda c, a, m: 10.0 if m == "native" else 200.0
    _, recoveries = cb.evaluate_cycle(_watches(), high, state, now=1.0, repeat=100.0)
    assert [r["symbol"] for r in recoveries] == ["SOL"]

    # No duplicate recovery on the next ok cycle
    _, recoveries = cb.evaluate_cycle(_watches(), high, state, now=2.0, repeat=100.0)
    assert recoveries == []


def test_none_balance_does_not_toggle_state(cb):
    state = {}
    # First, drive SOL into the low state
    low = lambda c, a, m: 1.0 if m == "native" else 200.0
    cb.evaluate_cycle(_watches(), low, state, now=0.0, repeat=100.0)
    # Transient failure (None) must not produce a recovery or a breach
    none = lambda c, a, m: None
    breaches, recoveries = cb.evaluate_cycle(_watches(), none, state, now=1.0, repeat=100.0)
    assert breaches == [] and recoveries == []
    assert state[("solana", "AAA", "native")]["low"] is True


def test_build_breach_message_lists_each(cb):
    msg = cb.build_breach_message([
        {"label": "relayer", "chain": "solana", "symbol": "SOL",
         "balance": 1.0, "threshold": 5.0, "address": "AAA"},
    ])
    assert "low balance" in msg.lower()
    assert "relayer" in msg and "SOL" in msg and "solana" in msg


def test_slack_post_noop_when_disabled(cb, monkeypatch):
    cb.SLACK_WEBHOOK_URL = ""
    called = {"n": 0}
    monkeypatch.setattr(cb.urllib.request, "urlopen",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    cb.slack_post("hi")
    assert called["n"] == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd repo && python -m pytest tests/e2e/test_balance_monitor_unit.py -q`
Expected: FAIL — the current `check-balance.py` has no `evaluate_cycle` / `build_breach_message` (AttributeError).

- [ ] **Step 3: Rewrite `check-balance.py`**

Replace the entire contents of
`stack_orchestrator/data/config/balance-monitor-scripts-config/check-balance.py` with:

```python
#!/usr/bin/env python3
"""Balance monitor — checks native + SPL balances from a generated watch file and
posts low-balance alerts to a Slack webhook.

Driven by /config/watches.json:

  {"watches": [
     {"chain": "solana", "label": "relayer", "address": "...",
      "tokens": [{"symbol": "SOL",  "mint": "native", "threshold": 5.0},
                 {"symbol": "USDC", "mint": "EPjF...", "threshold": 250.0}]}
  ]}

RPC per chain comes from env (GORCHAIN_RPC_URL / SOLANA_RPC_URL) so the secret
Helius URL never enters the generated file. SLACK_WEBHOOK_URL empty => alerting
disabled (the loop still runs and logs). Anti-spam: alert once on breach, re-alert
every ALERT_REPEAT_SECONDS while still low, one recovery note on return above.
Stdlib only.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

WATCHES_FILE = os.environ.get("WATCHES_FILE", "/config/watches.json")
INTERVAL = int(os.environ.get("BALANCE_CHECK_INTERVAL", "300"))
ALERT_REPEAT_SECONDS = int(os.environ.get("ALERT_REPEAT_SECONDS", "21600"))
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

RPC_BY_CHAIN = {
    "gorchain": os.environ.get("GORCHAIN_RPC_URL", "").strip(),
    "solana": os.environ.get("SOLANA_RPC_URL", "").strip(),
}

LAMPORTS_PER_SOL = 1_000_000_000


def _rpc(rpc_url: str, method: str, params: list) -> dict:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        rpc_url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def get_native_balance(rpc_url: str, address: str) -> float | None:
    """Native balance in whole tokens (e.g. SOL), or None on failure."""
    try:
        data = _rpc(rpc_url, "getBalance", [address])
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"  [ERROR] getBalance {address}: {exc}", flush=True)
        return None
    if "error" in data:
        print(f"  [ERROR] getBalance {address}: {data['error']}", flush=True)
        return None
    return data.get("result", {}).get("value", 0) / LAMPORTS_PER_SOL


def get_spl_balance(rpc_url: str, owner: str, mint: str) -> float | None:
    """Summed SPL uiAmount across the owner's token accounts for `mint` (decimals
    come from the RPC response), or None on failure."""
    try:
        data = _rpc(
            rpc_url,
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"  [ERROR] getTokenAccountsByOwner {owner}/{mint}: {exc}", flush=True)
        return None
    if "error" in data:
        print(f"  [ERROR] getTokenAccountsByOwner {owner}/{mint}: {data['error']}", flush=True)
        return None
    total = 0.0
    for acct in data.get("result", {}).get("value", []):
        amt = acct["account"]["data"]["parsed"]["info"]["tokenAmount"]
        total += float(amt.get("uiAmount") or 0)
    return total


def get_balance(chain: str, address: str, mint: str) -> float | None:
    """Dispatch native vs SPL using the chain's RPC env. Unknown chain → None."""
    rpc_url = RPC_BY_CHAIN.get(chain, "")
    if not rpc_url:
        print(f"  [ERROR] no RPC URL for chain {chain!r}", flush=True)
        return None
    if mint in ("", "native", None):
        return get_native_balance(rpc_url, address)
    return get_spl_balance(rpc_url, address, mint)


def load_watches(path: str) -> list[dict]:
    with open(path) as fh:
        return json.load(fh).get("watches", [])


def evaluate_cycle(
    watches: list[dict],
    balance_fn,
    alert_state: dict,
    now: float,
    repeat: float,
) -> tuple[list[dict], list[dict]]:
    """Check every (account, token) once; return (breaches, recoveries).

    alert_state[(chain, address, mint)] = {"low": bool, "last_alert": float}.
    A breach is reported on first crossing and re-reported once `repeat` seconds
    have elapsed while still low. A recovery is reported once when a low watch
    returns to/above threshold. A None balance (transient RPC failure) is ignored
    and does not toggle state.
    """
    breaches: list[dict] = []
    recoveries: list[dict] = []
    for w in watches:
        for t in w.get("tokens", []):
            mint = t.get("mint", "native")
            bal = balance_fn(w["chain"], w["address"], mint)
            if bal is None:
                continue
            threshold = float(t["threshold"])
            key = (w["chain"], w["address"], mint)
            st = alert_state.setdefault(key, {"low": False, "last_alert": 0.0})
            item = {
                "label": w["label"], "chain": w["chain"], "symbol": t["symbol"],
                "balance": bal, "threshold": threshold, "address": w["address"],
            }
            if bal < threshold:
                if (not st["low"]) or (now - st["last_alert"] >= repeat):
                    breaches.append(item)
                    st["last_alert"] = now
                st["low"] = True
                print(f"  [WARNING] {w['label']}/{t['symbol']} on {w['chain']}: "
                      f"{bal:.4f} < {threshold}", flush=True)
            else:
                if st["low"]:
                    recoveries.append(item)
                st["low"] = False
    return breaches, recoveries


def build_breach_message(breaches: list[dict]) -> str:
    lines = ["*:rotating_light: Hyperlane bridge — low balance*"]
    for b in breaches:
        lines.append(
            f"• `{b['label']}` {b['symbol']} on *{b['chain']}*: "
            f"{b['balance']:.4f} < {b['threshold']} (`{b['address']}`)"
        )
    return "\n".join(lines)


def build_recovery_message(recoveries: list[dict]) -> str:
    lines = ["*:white_check_mark: Hyperlane bridge — balance recovered*"]
    for r in recoveries:
        lines.append(
            f"• `{r['label']}` {r['symbol']} on *{r['chain']}*: "
            f"{r['balance']:.4f} >= {r['threshold']}"
        )
    return "\n".join(lines)


def slack_post(text: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except (urllib.error.URLError, OSError) as exc:
        print(f"  [ERROR] Slack post failed: {exc}", flush=True)


def main() -> None:
    print(
        f"[balance-monitor] Starting (interval {INTERVAL}s, repeat "
        f"{ALERT_REPEAT_SECONDS}s, slack {'on' if SLACK_WEBHOOK_URL else 'off'})",
        flush=True,
    )
    watches = load_watches(WATCHES_FILE)
    if not watches:
        print("[balance-monitor] No watches configured, exiting.", flush=True)
        return
    n_tokens = sum(len(w.get("tokens", [])) for w in watches)
    print(
        f"[balance-monitor] {len(watches)} account(s), {n_tokens} token watch(es)",
        flush=True,
    )

    alert_state: dict = {}
    while True:
        breaches, recoveries = evaluate_cycle(
            watches, get_balance, alert_state, time.time(), ALERT_REPEAT_SECONDS
        )
        if breaches:
            slack_post(build_breach_message(breaches))
        if recoveries:
            slack_post(build_recovery_message(recoveries))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the unit test to verify it passes**

Run: `cd repo && python -m pytest tests/e2e/test_balance_monitor_unit.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint the script + test**

Run: `cd repo && ruff check stack_orchestrator/data/config/balance-monitor-scripts-config/check-balance.py tests/e2e/test_balance_monitor_unit.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
cd repo
git add stack_orchestrator/data/config/balance-monitor-scripts-config/check-balance.py \
        tests/e2e/test_balance_monitor_unit.py
git commit -m "balance-monitor: watch-file driven native+SPL checks with Slack alerts

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 3 — Monitoring stack config

### Task 4: Monitoring compose — drop Pushgateway/wallet env, add watch config + Slack

**Files:**
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-monitoring.yml`

- [ ] **Step 1: Remove the `pushgateway` service**

Delete the whole `pushgateway:` service block (lines 31–39):

```yaml
  pushgateway:
    image: prom/pushgateway:latest
    restart: unless-stopped
    hostname: pushgateway
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:9091/-/healthy"]
      interval: 15s
      timeout: 5s
      retries: 3
```

- [ ] **Step 2: Rewrite the `balance-monitor` service env + volumes**

Replace the `balance-monitor` service block (lines 68–80) with:

```yaml
  balance-monitor:
    image: python:3.12-alpine
    restart: unless-stopped
    hostname: balance-monitor
    command: ["python3", "/opt/scripts/check-balance.py"]
    environment:
      GORCHAIN_RPC_URL: ${GORCHAIN_RPC_URL}
      # SOLANA_RPC_URL injected via secrets: in spec.yml (envFrom.secretRef)
      # SLACK_WEBHOOK_URL injected via secrets: (empty => alerting disabled)
      BALANCE_CHECK_INTERVAL: ${BALANCE_CHECK_INTERVAL:-300}
      ALERT_REPEAT_SECONDS: ${ALERT_REPEAT_SECONDS:-21600}
    volumes:
      - balance-monitor-scripts-config:/opt/scripts:ro
      - balance-monitor-config:/config:ro
```

- [ ] **Step 3: Declare the new config volume, drop nothing else**

In the `volumes:` block, add `balance-monitor-config:` alongside the other config
volumes (after `balance-monitor-scripts-config:`):

```yaml
  balance-monitor-scripts-config:
  balance-monitor-config:
```

- [ ] **Step 4: Static check (compose parses)**

Run: `cd repo && docker compose -f stack_orchestrator/data/compose/docker-compose-hyperlane-monitoring.yml config -q`
Expected: no output (valid). If `docker compose` is unavailable, run `python -c "import yaml,sys; yaml.safe_load(open('stack_orchestrator/data/compose/docker-compose-hyperlane-monitoring.yml'))"` (expected: no error) and confirm `pushgateway` is gone: `grep -c pushgateway stack_orchestrator/data/compose/docker-compose-hyperlane-monitoring.yml` → `0`.

- [ ] **Step 5: Commit**

```bash
cd repo
git add stack_orchestrator/data/compose/docker-compose-hyperlane-monitoring.yml
git commit -m "monitoring: drop pushgateway, drive balance-monitor from watch config + Slack

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Prometheus/Grafana — drop the balance metric path

**Files:**
- Modify: `stack_orchestrator/data/config/prometheus-config/prometheus.yml`
- Modify: `stack_orchestrator/data/config/prometheus-config/alerts.yml`
- Modify: `stack_orchestrator/data/config/grafana-dashboards-config/hyperlane-overview.json`

- [ ] **Step 1: Remove the Pushgateway scrape job**

In `prometheus.yml`, delete the `pushgateway` scrape job block:

```yaml
  # Pushgateway (balance monitor metrics)
  - job_name: pushgateway
    honor_labels: true
    static_configs:
      - targets: ["localhost:9091"]
```

- [ ] **Step 2: Remove the dead `WalletBalanceLow` alert rule**

In `alerts.yml`, delete the whole `WalletBalanceLow` rule:

```yaml
      # Wallet balance low (from balance monitor pushing to pushgateway).
      - alert: WalletBalanceLow
        expr: hyperlane_wallet_balance_sol < 1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Wallet {{ $labels.wallet }} on {{ $labels.chain }} balance low"
          description: "Balance is {{ $value }} SOL, below threshold."
```

- [ ] **Step 3: Remove the balance panel from the overview dashboard**

In `hyperlane-overview.json`, find the panel object whose target contains
`"expr": "hyperlane_wallet_balance_sol"` and remove that entire panel object from
the `panels` array (mind the surrounding commas so the JSON stays valid). After
editing, verify the JSON parses:

Run: `cd repo && python -c "import json; json.load(open('stack_orchestrator/data/config/grafana-dashboards-config/hyperlane-overview.json'))"`
Expected: no error.

Run: `cd repo && grep -c "hyperlane_wallet_balance" stack_orchestrator/data/config/grafana-dashboards-config/hyperlane-overview.json`
Expected: `0`.

- [ ] **Step 4: Confirm no lingering references**

Run: `cd repo && grep -rn "pushgateway\|hyperlane_wallet_balance\|WalletBalanceLow" stack_orchestrator/data/config/prometheus-config/ stack_orchestrator/data/config/grafana-dashboards-config/`
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
cd repo
git add stack_orchestrator/data/config/prometheus-config/prometheus.yml \
        stack_orchestrator/data/config/prometheus-config/alerts.yml \
        stack_orchestrator/data/config/grafana-dashboards-config/hyperlane-overview.json
git commit -m "monitoring: remove balance gauge scrape, alert rule, and Grafana panel

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Monitoring specs (×3) — watch configmap, Slack secret, drop wallet config

Apply the same set of edits to all three deployment specs:
`deployment/spec-monitoring.yml`, `deployment/staging/spec-monitoring.yml`,
`deployment/local/spec-monitoring.yml`.

**Files:**
- Modify: `deployment/spec-monitoring.yml`
- Modify: `deployment/staging/spec-monitoring.yml`
- Modify: `deployment/local/spec-monitoring.yml`

- [ ] **Step 1: Replace the wallet/threshold config keys with `ALERT_REPEAT_SECONDS`**

In each spec's `config:` block, delete these lines:

```yaml
  # Wallet format: label:address or label:address:threshold
  # Per-wallet threshold overrides BALANCE_THRESHOLD_SOL (the fallback).
  MONITORED_WALLETS_GORCHAIN: "relayer:ADDR:5.0,igp-oracle:ADDR:2.0"
  MONITORED_WALLETS_SOLANA: "relayer:ADDR:5.0,igp-oracle:ADDR:2.0"
  BALANCE_THRESHOLD_SOL: "1.0"  # fallback for wallets without explicit threshold
  BALANCE_CHECK_INTERVAL: "300"
```

and replace with:

```yaml
  BALANCE_CHECK_INTERVAL: "300"
  ALERT_REPEAT_SECONDS: "21600"  # re-alert cadence while a balance stays low (6 h)
```

(For `deployment/local/spec-monitoring.yml` the comment line is just
`# Wallet format: label:address or label:address:threshold` — remove that and the
two `MONITORED_WALLETS_*` + `BALANCE_THRESHOLD_SOL` lines the same way; keep its
existing `SOLANA_RPC_URL: "__SOLANA_RPC_URL__"` line untouched.)

- [ ] **Step 2: Add the `balance-monitor-config` configmap**

In each spec's `configmaps:` block, add a line after
`balance-monitor-scripts-config:`:

```yaml
  balance-monitor-config: ./configmaps/balance-monitor-config
```

- [ ] **Step 3: Add the `SLACK_WEBHOOK_URL` secret**

In the `secrets:` block of `deployment/spec-monitoring.yml` and
`deployment/staging/spec-monitoring.yml` (which already inject `SOLANA_RPC_URL`),
add the Slack key:

```yaml
secrets:
  hyperlane-monitoring-secrets:
    keys:
      GF_SECURITY_ADMIN_PASSWORD: { env: GF_SECURITY_ADMIN_PASSWORD }
      SOLANA_RPC_URL:             { env: SOLANA_RPC_URL }
      SLACK_WEBHOOK_URL:          { env: SLACK_WEBHOOK_URL }
```

For `deployment/local/spec-monitoring.yml` (own-chain; no `SOLANA_RPC_URL` secret
there — it renders `__SOLANA_RPC_URL__` into `config:`), add only the Slack key:

```yaml
secrets:
  hyperlane-monitoring-secrets:
    keys:
      GF_SECURITY_ADMIN_PASSWORD: { env: GF_SECURITY_ADMIN_PASSWORD }
      SLACK_WEBHOOK_URL:          { env: SLACK_WEBHOOK_URL }
```

- [ ] **Step 4: Update the header comments**

In each spec, change the top comment `# Prometheus, Grafana, pushgateway, and
balance monitoring.` to `# Prometheus, Grafana, and balance monitoring (Slack
alerts).` and update the `image-overrides:` example in the prod/staging specs to
drop the `pushgateway:` example line if present (prod/staging specs have a
commented `balance-monitor:` example — leave that).

- [ ] **Step 5: Static check (specs parse + parity)**

Run: `cd repo && for f in deployment/spec-monitoring.yml deployment/staging/spec-monitoring.yml deployment/local/spec-monitoring.yml; do python -c "import yaml; yaml.safe_load(open('$f'))" && echo "ok $f"; done`
Expected: `ok` for all three.

Run: `cd repo && grep -rn "MONITORED_WALLETS\|BALANCE_THRESHOLD\|pushgateway" deployment/spec-monitoring.yml deployment/staging/spec-monitoring.yml deployment/local/spec-monitoring.yml`
Expected: no matches.

If the repo has a spec-parity check (`tests/e2e/test_*spec_parity*` or
`scripts/check-spec-parity*`), run it and expect pass; otherwise skip.

- [ ] **Step 6: Commit**

```bash
cd repo
git add deployment/spec-monitoring.yml deployment/staging/spec-monitoring.yml \
        deployment/local/spec-monitoring.yml
git commit -m "monitoring specs: add balance-monitor watch configmap + Slack secret, drop wallet env

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 4 — Ops generation

### Task 7: Slack secret + balance_monitor config surface (ops)

Wire `SLACK_WEBHOOK_URL` as a monitoring secret env in the ops layer, and add the
`balance_monitor` config block to the deployment-config examples.

**Files:**
- Modify: `ops/inventories/{prod,staging}/group_vars/all.yml` (secret-env values + `stack_env_vars.hyperlane-monitoring`)
- Modify: `ops/inventories/local/group_vars/all.yml` (secret-env values + `stack_env_vars.hyperlane-monitoring`)
- Modify: `ops/inventories/{prod,staging,local}/deployment-config.example.yml`

- [ ] **Step 1: Add the secret-env value in all three group_vars**

In each `group_vars/all.yml`, in the `# --- Secret-env values ---` section (near
`GF_SECURITY_ADMIN_PASSWORD`), add:

```yaml
SLACK_WEBHOOK_URL: "{{ slack_webhook_url | default('') }}"
```

- [ ] **Step 2: List the secret in `stack_env_vars.hyperlane-monitoring`**

In each `group_vars/all.yml`, the `stack_env_vars: hyperlane-monitoring:` list
currently is:

```yaml
  hyperlane-monitoring:
    - GF_SECURITY_ADMIN_PASSWORD
    - SOLANA_RPC_URL
```

Add `SLACK_WEBHOOK_URL`:

```yaml
  hyperlane-monitoring:
    - GF_SECURITY_ADMIN_PASSWORD
    - SOLANA_RPC_URL
    - SLACK_WEBHOOK_URL
```

(For `local`, the monitoring entry has no `SOLANA_RPC_URL` line in some inventories —
add `SLACK_WEBHOOK_URL` to whatever list exists; if `hyperlane-monitoring` is absent
from `stack_env_vars`, add it with the two keys `GF_SECURITY_ADMIN_PASSWORD` and
`SLACK_WEBHOOK_URL`.)

- [ ] **Step 3: Add the `balance_monitor` block + Slack secret to deployment-config examples**

In each `deployment-config.example.yml`, add to the `# --- Secrets (sensitive) ---`
section:

```yaml
slack_webhook_url: ""           # Slack incoming-webhook URL; empty disables balance alerts
```

and add a new section (after the bridge-identity block):

```yaml
# --- Balance monitoring (optional; defaults shown) ---
# Auto-watched signers: relayer (both chains), fee-claim, igp-oracle, validators.
# Add extra/SPL watches post-deploy by editing the generated watches.json on the
# monitoring host and `laconic-so deployment <dir> restart` (see runbooks).
balance_monitor:
  default_threshold: 1.0        # native-balance floor for any signer without a role entry
  thresholds:
    relayer: 5.0
    fee-claim: 2.0
    igp-oracle: 2.0
    validator: 1.0
```

- [ ] **Step 4: Static check**

Run: `cd repo/ops && export LC_ALL=C.UTF-8 LANG=C.UTF-8 && ansible-lint inventories/prod/group_vars/all.yml inventories/staging/group_vars/all.yml inventories/local/group_vars/all.yml`
Expected: 0 failures.

Run: `cd repo && for f in ops/inventories/*/deployment-config.example.yml; do python -c "import yaml; yaml.safe_load(open('$f'))" && echo "ok $f"; done`
Expected: `ok` for all three.

- [ ] **Step 5: Commit**

```bash
cd repo
git add ops/inventories/prod/group_vars/all.yml ops/inventories/staging/group_vars/all.yml \
        ops/inventories/local/group_vars/all.yml \
        ops/inventories/prod/deployment-config.example.yml \
        ops/inventories/staging/deployment-config.example.yml \
        ops/inventories/local/deployment-config.example.yml
git commit -m "ops: surface SLACK_WEBHOOK_URL secret and balance_monitor thresholds

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: `build_watches` role + wire into the monitoring deploy

Generate `watches.json` from the bridge's own signers (addresses gathered
cross-host via non-secret `.pub` files + `igp_oracle_pubkey`), with per-role
thresholds, into the monitoring stack's `balance-monitor-config` configmap. Run it
as the monitoring stack's pre-start hook.

**Files:**
- Create: `ops/roles/build_watches/tasks/main.yml`
- Modify: `ops/playbooks/deploy-all.yml` (Monitoring play, lines 267–277)

- [ ] **Step 1: Create the role task file**

Create `ops/roles/build_watches/tasks/main.yml`:

```yaml
---
# Render balance-monitor's watches.json from the bridge's own signers, with
# per-role thresholds from deployment-config (balance_monitor.*). Runs as the
# monitoring stack's pre-start hook: deploy create has made the configmaps dir,
# start has not yet mounted it. Multi-machine safe — addresses are non-secret .pub
# material gathered via delegation (the controller reaches every host); the secret
# RPC URLs stay in the spec's secret env and never enter this file.
#
# Auto-watched: relayer (both chains), fee-claim (both chains), igp-oracle (both
# chains), each validator (its chain). The IGP beneficiary is a fee sink and is
# intentionally excluded. Operators add extra/SPL watches by editing the generated
# watches.json on this host and `laconic-so deployment <dir> restart`.
- name: Load the validator set
  ansible.builtin.include_tasks: "{{ playbook_dir }}/../roles/common/tasks/load_validators.yml"

- name: Resolve the relayer host
  ansible.builtin.set_fact:
    _relayer_host: "{{ groups['relayer_hosts'][0] }}"

- name: Gather min facts for the relayer + validator hosts (for HOME/credentials_dir)
  ansible.builtin.setup:
    gather_subset: ["!all", "min"]
  delegate_to: "{{ item }}"
  delegate_facts: true
  loop: "{{ ([_relayer_host] + (validators | map(attribute='host') | list)) | unique }}"

- name: Read the relayer signer .pub files (non-secret addresses)
  ansible.builtin.slurp:
    src: "{{ hostvars[_relayer_host].ansible_env.HOME }}/.credentials/hyperlane/{{ item }}"
  register: _relayer_pubs
  delegate_to: "{{ _relayer_host }}"
  loop:
    - relayer-gorchain.key.pub
    - relayer-solana.key.pub

- name: Derive the fee-claim address from its keypair
  ansible.builtin.command:
    cmd: >-
      {{ hostvars[_relayer_host].ansible_env.HOME }}/.local/share/solana/install/active_release/bin/solana-keygen
      pubkey {{ hostvars[_relayer_host].ansible_env.HOME }}/.credentials/hyperlane/relayer-fee-claim.json
  register: _fee_claim_pub
  delegate_to: "{{ _relayer_host }}"
  changed_when: false

- name: Read each validator's .pub (non-secret address)
  ansible.builtin.slurp:
    src: "{{ hostvars[item.host].ansible_env.HOME }}/.credentials/hyperlane/validator-{{ item.chain }}.key.pub"
  register: _validator_pubs
  delegate_to: "{{ item.host }}"
  loop: "{{ validators }}"
  loop_control:
    label: "{{ item.label }}"

- name: Assemble the watch list
  ansible.builtin.set_fact:
    _watches: >-
      {%- set thr = balance_monitor.thresholds | default({}) -%}
      {%- set dflt = balance_monitor.default_threshold | default(1.0) -%}
      {%- set sym = {'gorchain': 'GOR', 'solana': 'SOL'} -%}
      {%- set rg = _relayer_pubs.results[0].content | b64decode | trim -%}
      {%- set rs = _relayer_pubs.results[1].content | b64decode | trim -%}
      {%- set fc = _fee_claim_pub.stdout | trim -%}
      {%- set out = [] -%}
      {%- set _ = out.append({'chain': 'gorchain', 'label': 'relayer', 'address': rg,
            'tokens': [{'symbol': sym.gorchain, 'mint': 'native', 'threshold': thr.relayer | default(dflt)}]}) -%}
      {%- set _ = out.append({'chain': 'solana', 'label': 'relayer', 'address': rs,
            'tokens': [{'symbol': sym.solana, 'mint': 'native', 'threshold': thr.relayer | default(dflt)}]}) -%}
      {%- for ch in ['gorchain', 'solana'] -%}
        {%- set _ = out.append({'chain': ch, 'label': 'fee-claim', 'address': fc,
              'tokens': [{'symbol': sym[ch], 'mint': 'native', 'threshold': thr['fee-claim'] | default(dflt)}]}) -%}
        {%- set _ = out.append({'chain': ch, 'label': 'igp-oracle', 'address': igp_oracle_pubkey,
              'tokens': [{'symbol': sym[ch], 'mint': 'native', 'threshold': thr['igp-oracle'] | default(dflt)}]}) -%}
      {%- endfor -%}
      {%- for vp in _validator_pubs.results -%}
        {%- set _ = out.append({'chain': vp.item.chain, 'label': vp.item.label,
              'address': vp.content | b64decode | trim,
              'tokens': [{'symbol': sym[vp.item.chain], 'mint': 'native', 'threshold': thr.validator | default(dflt)}]}) -%}
      {%- endfor -%}
      {{ out }}

- name: Ensure the balance-monitor-config dir exists
  ansible.builtin.file:
    path: "{{ deploy_dir }}/configmaps/balance-monitor-config"
    state: directory
    mode: "0755"

- name: Render watches.json
  ansible.builtin.copy:
    dest: "{{ deploy_dir }}/configmaps/balance-monitor-config/watches.json"
    mode: "0644"
    content: "{{ {'watches': _watches} | to_nice_json }}"
```

- [ ] **Step 2: Wire it into the Monitoring play**

In `ops/playbooks/deploy-all.yml`, the Monitoring play (currently lines 267–277)
has only `stack_name: hyperlane-monitoring` under `vars:`. Replace its `vars:` with:

```yaml
  vars:
    stack_name: hyperlane-monitoring
    # build_watches runs as the pre-start hook (after deploy create makes the dir,
    # before start mounts it). deploy_dir is play-scoped so the hook and stack_deploy
    # resolve the same dir (same pattern as the Relayer/Warp-UI plays).
    deploy_dir: "{{ ansible_env.HOME }}/deployments/hyperlane-monitoring"
    stack_pre_start_tasks: "{{ playbook_dir }}/../roles/build_watches/tasks/main.yml"
```

- [ ] **Step 3: Syntax + lint check**

Run: `cd repo/ops && export LC_ALL=C.UTF-8 LANG=C.UTF-8 && ansible-lint roles/build_watches/tasks/main.yml playbooks/deploy-all.yml`
Expected: 0 failures.

Run: `cd repo/ops && export LC_ALL=C.UTF-8 LANG=C.UTF-8 && ansible-playbook -i inventories/prod/hosts.yml playbooks/deploy-all.yml --syntax-check`
Expected: `playbook: playbooks/deploy-all.yml` (no error). If `--syntax-check`
requires connectivity/vars it doesn't have, fall back to
`ansible-playbook playbooks/deploy-all.yml --syntax-check` (no inventory).

- [ ] **Step 4: Commit**

```bash
cd repo
git add ops/roles/build_watches/tasks/main.yml ops/playbooks/deploy-all.yml
git commit -m "ops: generate balance-monitor watches.json from bridge signers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 5 — e2e

### Task 9: e2e — watch file + mock Slack, drop metric assertions

Switch the e2e monitoring path off Pushgateway metrics and onto the watch file +
a host-side mock Slack endpoint (reached from the pod via the same
`REPLACE_HOST_IP` external-service pattern used for `gorchain-rpc`).

**Files:**
- Modify: `tests/e2e/fixtures/test-spec-monitoring.yml`
- Modify: `tests/e2e/conftest.py` (monitoring fixture + helpers, lines ~1437–1650)
- Modify: `tests/e2e/test_08_monitoring.py`
- Modify: `tests/e2e/test_05_validator.py` (drop the balance-metric assertion, ~line 230)

- [ ] **Step 1: Update the test spec**

In `tests/e2e/fixtures/test-spec-monitoring.yml`:

(a) Under `external-services:` add a mock-Slack endpoint (host-reachable, like the
RPCs):

```yaml
  slack-mock:
    ip: REPLACE_HOST_IP
    port: 18080
```

(b) In `config:`, delete the three wallet lines and set the watch/Slack/interval
config:

Remove:
```yaml
  MONITORED_WALLETS_GORCHAIN: "REPLACE_AT_RUNTIME"
  MONITORED_WALLETS_SOLANA: "REPLACE_AT_RUNTIME"
  BALANCE_THRESHOLD_SOL: "1.0"
  BALANCE_CHECK_INTERVAL: "30"
```
Add:
```yaml
  BALANCE_CHECK_INTERVAL: "30"
  ALERT_REPEAT_SECONDS: "3600"
  SLACK_WEBHOOK_URL: "http://slack-mock:18080/webhook"
```

(c) In `configmaps:` add:
```yaml
  balance-monitor-config: ./configmaps/balance-monitor-config
```

(d) In `image-overrides:` remove the `pushgateway: prom/pushgateway:latest` line.

- [ ] **Step 2: Rewrite the conftest balance helpers**

In `tests/e2e/conftest.py`, replace `_build_wallet_string` (lines ~1462–1491) with a
watch-file builder + a tiny threaded mock-Slack server. Insert near the other
monitoring helpers:

```python
def _build_watches(keypairs: KeypairSet) -> tuple[dict, list[str]]:
    """Build a watches.json doc for e2e and return (doc, low_labels).

    Uses keypairs funded during setup. The igp-beneficiary is left UNFUNDED in
    setup, so it is the deliberate low-balance watch that must trigger a Slack
    alert; the relayer/oracle are funded and must stay quiet. One SPL watch
    (the deployer's account, mint=native is native; we add an SPL entry with a
    high threshold only if a known mint exists) exercises the multi-token path.
    """
    high, low = "1.0", "1000000.0"  # low threshold = quiet; high = guaranteed breach
    watches = {
        "watches": [
            {"chain": "gorchain", "label": "relayer", "address": keypairs.deployer_pubkey,
             "tokens": [{"symbol": "GOR", "mint": "native", "threshold": high}]},
            {"chain": "solana", "label": "relayer", "address": keypairs.deployer_pubkey,
             "tokens": [{"symbol": "SOL", "mint": "native", "threshold": high}]},
            {"chain": "solana", "label": "igp-beneficiary", "address": keypairs.igp_beneficiary_pubkey,
             "tokens": [{"symbol": "SOL", "mint": "native", "threshold": low}]},
        ]
    }
    return watches, ["igp-beneficiary"]


class _SlackCapture:
    """Threaded HTTP server capturing POSTed Slack payloads for assertions."""

    def __init__(self, port: int = 18080) -> None:
        import http.server
        import threading

        self.payloads: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    outer.payloads.append(json.loads(body))
                except ValueError:
                    pass
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):  # silence
                return

        self._server = http.server.HTTPServer(("0.0.0.0", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_SlackCapture":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
```

- [ ] **Step 3: Rewrite the monitoring fixture body**

In the `monitoring_deployment` fixture (lines ~1494–1650), replace the
wallet-string patching + populate + metric-wait with watch-file writing + a
Slack-capture context. The key changes:

Replace (the `--skip-monitoring-deploy` recovery block's metric probe and the
fresh-deploy wallet patching) with this fresh-deploy body (keep the
`deploy_prepare` / `deploy_start` / pod-wait structure already present):

```python
    # Build the watch file and start a host-side Slack capture
    watches_doc, low_labels = _build_watches(keypairs)

    content = MONITORING_SPEC.read_text()
    patched_path = E2E_DIR / ".monitoring-spec-patched.yml"
    patched_path.write_text(content)  # no wallet patching needed any more

    log.info("Preparing monitoring stack...")
    deploy_info = deploy_prepare(
        "hyperlane-monitoring", patched_path,
        spec_replacements=SPEC_REPLACEMENTS,
        deployment_id="monitoring",
    )

    # Write watches.json into the runtime-populated configmap (no data/config src).
    watch_dir = deploy_info.deploy_dir / "configmaps" / "balance-monitor-config"
    watch_dir.mkdir(parents=True, exist_ok=True)
    (watch_dir / "watches.json").write_text(json.dumps(watches_doc))

    os.environ["GF_SECURITY_ADMIN_PASSWORD"] = GRAFANA_ADMIN_PASSWORD

    with _SlackCapture(port=18080) as slack:
        log.info("Starting monitoring stack...")
        deploy_start(deploy_info.deploy_dir)
        # ... existing pod-wait + ingress-wait code stays here ...
        # Wait for the balance monitor to complete a cycle and post an alert
        log.info("Waiting for balance monitor to alert on the underfunded wallet...")
        deadline = time.time() + 120
        while time.time() < deadline and not slack.payloads:
            time.sleep(3)

        yield {
            "deployment": deploy_info,
            "namespace": namespace,
            "pod_name": pod_name,
            "low_labels": low_labels,
            "slack_payloads": list(slack.payloads),
            "grafana_url": GRAFANA_URL,
            "prometheus_url": PROMETHEUS_URL,
        }
```

Notes for the implementer:
- Remove `_wait_for_balance_monitor`'s dependency on the `"Gorchain wallets:"` log
  line (that text no longer exists). Either delete `_wait_for_balance_monitor` and
  the call, or update its sentinel to `"account(s),"` (the new startup log). The
  Slack-payload wait above is the real readiness gate.
- Delete the `bridge_state_loader.populate("hyperlane-monitoring", …)` call —
  monitoring has no populate mapping (`state_loader.CONSUMER_STATE_FILES` lists it
  as `[]`); the watch file is written directly above.
- Remove the `time.sleep(20)` "wait for Prometheus to scrape balance metrics" and
  the `expected_wallet_labels` key (replaced by `low_labels` / `slack_payloads`).
- The `--skip-monitoring-deploy` reuse branch: drop the Prometheus metric probe; it
  may yield `slack_payloads: []` and `low_labels: []` (reuse can't replay alerts).

- [ ] **Step 4: Rewrite `test_08_monitoring.py`**

- Delete `test_prometheus_pushgateway_target`, `test_prometheus_has_balance_metrics`,
  and `test_balance_metrics_have_correct_labels` (all assert the removed gauge).
- Replace `test_balance_monitor_wallets_checked` with log + alert assertions:

```python
    def test_balance_monitor_started(self, monitoring_deployment: dict) -> None:
        """Balance monitor started and reported its watch counts."""
        ns = monitoring_deployment["namespace"]
        pod = monitoring_deployment["pod_name"]
        logs = _get_container_logs(ns, pod, "balance-monitor")
        assert "[balance-monitor] Starting" in logs, "balance monitor did not start"
        assert "account(s)," in logs, "balance monitor did not report watch counts"
        log.info("Balance monitor started and reported watches")

    def test_balance_monitor_alerts_low_wallet(self, monitoring_deployment: dict) -> None:
        """The underfunded watch produced a Slack alert; funded ones stayed quiet."""
        payloads = monitoring_deployment["slack_payloads"]
        assert payloads, "no Slack alert captured for the underfunded wallet"
        text = "\n".join(p.get("text", "") for p in payloads)
        for label in monitoring_deployment["low_labels"]:
            assert label in text, f"alert missing low wallet {label!r}: {text}"
        assert "relayer" not in text, f"funded relayer should not alert: {text}"
        log.info("Slack alert fired for: %s", monitoring_deployment["low_labels"])
```

- Keep all Prometheus/Grafana health/datasource/dashboard/scrape tests unchanged
  EXCEPT remove any that reference the deleted gauge (only the three named above).

- [ ] **Step 5: Drop the balance-metric assertion in `test_05_validator.py`**

In `tests/e2e/test_05_validator.py` (~lines 228–232) remove the assertion that
`"hyperlane_wallet_balance" in metrics`. If the surrounding test exists solely for
that metric, delete the test; otherwise delete just the two assertion lines and any
now-unused setup.

Run: `cd repo && grep -rn "hyperlane_wallet_balance\|pushgateway\|MONITORED_WALLETS\|expected_wallet_labels" tests/e2e/`
Expected: no matches.

- [ ] **Step 6: Lint (no cluster run on this machine)**

Run: `cd repo && ruff check tests/e2e/conftest.py tests/e2e/test_08_monitoring.py tests/e2e/test_05_validator.py tests/e2e/fixtures/test-spec-monitoring.yml 2>/dev/null; ruff check tests/e2e/conftest.py tests/e2e/test_08_monitoring.py tests/e2e/test_05_validator.py`
Expected: no errors.

Run: `cd repo && python -m pytest tests/e2e/test_balance_monitor_unit.py -q`
Expected: still PASS (unchanged).

Do NOT run the slow/cluster e2e here — that is handed off (see Handoff).

- [ ] **Step 7: Commit**

```bash
cd repo
git add tests/e2e/fixtures/test-spec-monitoring.yml tests/e2e/conftest.py \
        tests/e2e/test_08_monitoring.py tests/e2e/test_05_validator.py
git commit -m "e2e: assert balance-monitor Slack alert via watch file + mock; drop metric checks

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 6 — Docs

### Task 10: Docs — stack README, runbook, stack-spec, e2e-spec

**Files:**
- Modify: `stack_orchestrator/data/stacks/hyperlane-monitoring/README.md`
- Create: `ops/runbooks/monitoring.md`
- Modify: `docs/stack-specifications.md` (monitoring section)
- Modify: `docs/e2e-test-spec.md` (monitoring section)

- [ ] **Step 1: Rewrite the stack README balance section**

In `stack_orchestrator/data/stacks/hyperlane-monitoring/README.md`, replace the
"Wallet balances" item (item 2, the one referencing `getBalance` →
`hyperlane_wallet_balance_sol` → Pushgateway) with:

```markdown
2. **Balance monitoring + Slack alerts**: The balance monitor reads
   `/config/watches.json` (the `balance-monitor-config` ConfigMap) and, every
   `BALANCE_CHECK_INTERVAL` seconds, checks each account's native and/or SPL token
   balances against per-token thresholds. When a balance is below threshold it
   POSTs a batched alert to `SLACK_WEBHOOK_URL` (empty disables alerting); it
   re-alerts every `ALERT_REPEAT_SECONDS` while still low and posts a recovery
   message when it climbs back. RPC URLs come from `GORCHAIN_RPC_URL` /
   `SOLANA_RPC_URL` (kept out of the watch file). No Prometheus/Pushgateway metric
   is emitted for balances.
```

Remove any remaining "Pushgateway" mentions from the README's component list.

- [ ] **Step 2: Create the monitoring runbook**

Create `ops/runbooks/monitoring.md`:

```markdown
# Monitoring & balance alerts

The `hyperlane-monitoring` stack runs Prometheus + Grafana (agent metrics) and a
balance monitor that posts low-balance alerts to Slack.

## Slack alerts

Set `slack_webhook_url` in `deployment-config.yml` to a Slack incoming-webhook URL.
Leaving it empty disables alerting (the monitor still runs and logs). Tune
thresholds and cadence under `balance_monitor:` (see `deployment-config.example.yml`).

## What is watched automatically

`deploy-all` renders `watches.json` from the bridge's own signers, with native
thresholds from `balance_monitor.thresholds` (fallback `default_threshold`):

- relayer (gorchain + solana)
- fee-claim (gorchain + solana)
- igp-oracle (gorchain + solana)
- each validator (its chain)

The IGP beneficiary is a fee sink and is intentionally not watched.

## Adding extra or SPL token watches

Edit the generated file on the monitoring host and restart the stack:

```bash
# on the monitoring host
$EDITOR ~/deployments/hyperlane-monitoring/configmaps/balance-monitor-config/watches.json
laconic-so deployment ~/deployments/hyperlane-monitoring restart
```

Add entries like:

```json
{ "chain": "solana", "label": "treasury", "address": "<pubkey>",
  "tokens": [ { "symbol": "USDC", "mint": "EPjF...", "threshold": 1000 },
              { "symbol": "SOL",  "mint": "native", "threshold": 10 } ] }
```

`mint: "native"` watches the gas token; any other `mint` watches that SPL token's
balance for the account (summed across its token accounts).

**Caveat:** a full `deploy-all` (or any ops redeploy of monitoring) regenerates
`watches.json` from the auto-signers only — re-apply manual entries afterwards.
Plain `laconic-so … restart` preserves your edits.
```

- [ ] **Step 3: Update `docs/stack-specifications.md`**

In the monitoring stack section, replace any description of env-var wallet lists +
Pushgateway balance metrics with the watch-file + Slack model (mirror the README
wording). Add the new config keys (`ALERT_REPEAT_SECONDS`, `SLACK_WEBHOOK_URL`
secret, `balance-monitor-config` configmap) and note Pushgateway was removed.

- [ ] **Step 4: Update `docs/e2e-test-spec.md`**

In the monitoring test section, replace the Pushgateway/`hyperlane_wallet_balance_sol`
PromQL assertions (around the lines that mention them) with: "assert the balance
monitor starts and reports its watch counts; assert a Slack alert is captured by the
mock for the deliberately-underfunded watch and that funded signers stay quiet."

- [ ] **Step 5: Static check**

Run: `cd repo && grep -rn "pushgateway\|Pushgateway\|hyperlane_wallet_balance\|MONITORED_WALLETS" docs/ stack_orchestrator/data/stacks/hyperlane-monitoring/README.md`
Expected: no matches (or only historical/changelog references you deliberately keep).

- [ ] **Step 6: Commit**

```bash
cd repo
git add stack_orchestrator/data/stacks/hyperlane-monitoring/README.md \
        ops/runbooks/monitoring.md docs/stack-specifications.md docs/e2e-test-spec.md
git commit -m "docs: Slack-based balance monitoring (watch file, runbook, specs)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Handoff (after all tasks)

The e2e suite and live deploys must NOT run on this shared dev machine. Hand the
user these exact commands to validate end-to-end:

```bash
# unit (safe to run anywhere)
cd <repo> && python -m pytest tests/e2e/test_balance_monitor_unit.py -q

# full monitoring e2e (user runs on the e2e host)
cd <repo> && python -m pytest tests/e2e/test_08_monitoring.py -v

# ops dry-run against staging before a real deploy
cd <repo>/ops && export LC_ALL=C.UTF-8 LANG=C.UTF-8 && \
  ansible-playbook -i inventories/staging/hosts.yml playbooks/deploy-all.yml --check --diff
```

After the user confirms green, use **superpowers:finishing-a-development-branch**.

---

## Self-review (spec coverage)

- **Native + multiple SPL per account, native opt-in per account** → Task 3 (`get_balance`/`evaluate_cycle`, `tokens[]` schema).
- **Generic watch file, single generated config the operator edits** → Tasks 6/8 (`balance-monitor-config` configmap), runbook in Task 10.
- **Auto-include bridge signers (relayer, fee-claim, igp-oracle, validators); beneficiary excluded** → Task 8.
- **Slack delivery, empty disables, anti-spam (re-alert + recovery)** → Task 3; secret wiring Tasks 6/7.
- **Drop metrics + Pushgateway + Grafana panel + dead alert rule** → Tasks 4/5.
- **Edit-file + `laconic-so restart` preserves edits; redeploy clobbers (documented)** → runbook Task 10.
- **Multi-machine address gathering via non-secret `.pub` + config vars** → Task 8.
- **Configurable fee-claim + gas-oracle intervals via `spec_token_renders` with defaults; e2e fixtures literal** → Tasks 1/2.
- **Keep-in-sync (compose ↔ specs ↔ test fixtures ↔ stack_env_vars ↔ docs)** → Tasks 4/6/7/9/10.
- **Testing: unit (Slack/anti-spam/SPL) + e2e (watch file, mock Slack alert)** → Tasks 3/9.
