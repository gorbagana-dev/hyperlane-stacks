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
    value = data.get("result", {}).get("value")
    if value is None:
        print(f"  [ERROR] getBalance {address}: malformed response {data}", flush=True)
        return None
    return value / LAMPORTS_PER_SOL


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
        try:
            amt = acct["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += float(amt.get("uiAmount") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            print(f"  [WARNING] skipping malformed token account for {owner}/{mint}: {exc}", flush=True)
    return total


def get_balance(chain: str, address: str, mint: str) -> float | None:
    """Dispatch native vs SPL using the chain's RPC env. Unknown chain -> None."""
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
            try:
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
            except (KeyError, TypeError, ValueError) as exc:
                print(f"  [WARNING] skipping malformed watch entry {w.get('label', '?')}: {exc}", flush=True)
                continue
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
