#!/bin/bash
# Balance monitor sidecar — checks wallet balances and pushes to pushgateway
# Runs in a loop, checking balances at configurable intervals.

set -euo pipefail

PUSHGATEWAY_URL="http://localhost:9091"
INTERVAL="${BALANCE_CHECK_INTERVAL:-300}"

check_balance() {
  local label="$1" address="$2" rpc_url="$3" chain="$4"
  local balance
  balance=$(solana balance "$address" --url "$rpc_url" 2>/dev/null | awk '{print $1}')
  if [[ -n "$balance" ]]; then
    cat <<PEOF | curl -s --data-binary @- "${PUSHGATEWAY_URL}/metrics/job/balance_monitor/chain/${chain}/wallet/${label}"
# HELP hyperlane_wallet_balance_sol Wallet balance in SOL
# TYPE hyperlane_wallet_balance_sol gauge
hyperlane_wallet_balance_sol{chain="${chain}",wallet="${label}",address="${address}"} ${balance}
PEOF

    # Alert threshold check
    threshold="${BALANCE_THRESHOLD_SOL:-1}"
    if (( $(echo "$balance < $threshold" | bc -l 2>/dev/null || echo 0) )); then
      echo "[balance-monitor] WARNING: ${label} on ${chain} balance ${balance} SOL < threshold ${threshold} SOL"
    fi
  fi
}

echo "[balance-monitor] Starting balance monitor (interval: ${INTERVAL}s)"
while true; do
  # Parse MONITORED_WALLETS: comma-separated "label:address" pairs
  IFS=',' read -ra GORCHAIN_WALLETS <<< "${MONITORED_WALLETS_GORCHAIN:-}"
  for entry in "${GORCHAIN_WALLETS[@]}"; do
    [[ -z "$entry" ]] && continue
    label="${entry%%:*}"
    address="${entry#*:}"
    check_balance "$label" "$address" "${GORCHAIN_RPC_URL}" "gorchain"
  done

  IFS=',' read -ra SOLANA_WALLETS <<< "${MONITORED_WALLETS_SOLANA:-}"
  for entry in "${SOLANA_WALLETS[@]}"; do
    [[ -z "$entry" ]] && continue
    label="${entry%%:*}"
    address="${entry#*:}"
    check_balance "$label" "$address" "${SOLANA_RPC_URL}" "solanasvm"
  done

  sleep "$INTERVAL"
done
