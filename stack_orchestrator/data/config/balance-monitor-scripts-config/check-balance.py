#!/usr/bin/env python3
"""Balance monitor — checks wallet balances via Solana JSON-RPC and pushes to Pushgateway.

Runs in a loop, checking balances at configurable intervals.
No external dependencies — uses only Python stdlib.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "http://localhost:9091")
INTERVAL = int(os.environ.get("BALANCE_CHECK_INTERVAL", "300"))
THRESHOLD = float(os.environ.get("BALANCE_THRESHOLD_SOL", "1.0"))

GORCHAIN_RPC = os.environ.get("GORCHAIN_RPC_URL", "")
SOLANA_RPC = os.environ.get("SOLANA_RPC_URL", "")

WALLETS_GORCHAIN = os.environ.get("MONITORED_WALLETS_GORCHAIN", "")
WALLETS_SOLANA = os.environ.get("MONITORED_WALLETS_SOLANA", "")

# Solana native token has 9 decimals (1 SOL = 1e9 lamports)
LAMPORTS_PER_SOL = 1_000_000_000


def get_balance(rpc_url: str, address: str) -> float | None:
    """Fetch wallet balance in SOL via Solana JSON-RPC getBalance."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [address],
    }).encode()

    req = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"  [ERROR] RPC call failed for {address}: {exc}", flush=True)
        return None

    if "error" in data:
        print(f"  [ERROR] RPC error for {address}: {data['error']}", flush=True)
        return None

    lamports = data.get("result", {}).get("value", 0)
    return lamports / LAMPORTS_PER_SOL


def push_metric(chain: str, label: str, address: str, balance: float) -> None:
    """Push a single balance metric to Pushgateway."""
    body = (
        "# HELP hyperlane_wallet_balance_sol Wallet balance in SOL\n"
        "# TYPE hyperlane_wallet_balance_sol gauge\n"
        f'hyperlane_wallet_balance_sol{{chain="{chain}",'
        f'wallet="{label}",address="{address}"}} {balance}\n'
    ).encode()

    url = f"{PUSHGATEWAY_URL}/metrics/job/balance_monitor/chain/{chain}/wallet/{label}"
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except (urllib.error.URLError, OSError) as exc:
        print(f"  [ERROR] Pushgateway push failed: {exc}", flush=True)


def parse_wallets(raw: str) -> list[tuple[str, str]]:
    """Parse comma-separated 'label:address' pairs."""
    wallets = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        label, address = entry.split(":", 1)
        wallets.append((label.strip(), address.strip()))
    return wallets


def check_wallets(rpc_url: str, chain: str, wallets: list[tuple[str, str]]) -> None:
    """Check and push balance for each wallet on a chain."""
    for label, address in wallets:
        balance = get_balance(rpc_url, address)
        if balance is None:
            continue
        push_metric(chain, label, address, balance)
        if balance < THRESHOLD:
            print(
                f"  [WARNING] {label} on {chain}: {balance:.4f} SOL "
                f"< threshold {THRESHOLD} SOL",
                flush=True,
            )


def main() -> None:
    print(f"[balance-monitor] Starting (interval: {INTERVAL}s, threshold: {THRESHOLD} SOL)", flush=True)

    gorchain_wallets = parse_wallets(WALLETS_GORCHAIN)
    solana_wallets = parse_wallets(WALLETS_SOLANA)

    if not gorchain_wallets and not solana_wallets:
        print("[balance-monitor] No wallets configured, exiting.", flush=True)
        return

    print(f"[balance-monitor] Gorchain wallets: {len(gorchain_wallets)}, Solana wallets: {len(solana_wallets)}", flush=True)

    while True:
        if gorchain_wallets and GORCHAIN_RPC:
            check_wallets(GORCHAIN_RPC, "gorchain", gorchain_wallets)
        if solana_wallets and SOLANA_RPC:
            check_wallets(SOLANA_RPC, "solana", solana_wallets)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
