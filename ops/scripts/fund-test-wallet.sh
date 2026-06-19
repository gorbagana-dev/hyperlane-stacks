#!/usr/bin/env bash
# Fund an operator TEST wallet (e.g. Backpack) for trying the bridge — never the
# deployer account. Balance-driven and idempotent: tops up to targets, so re-runs
# never double-fund.
#
# - GOR on gorchain: own faucet, chunked <=10 SOL/request (the faucet cap).
# - SOL on solana: local test validator (chunk 10) or devnet (chunk 2, rate-limited
#   best effort — shortfalls reported, exit non-zero, re-run after topping up).
# - USDC on solana (local only): transferred from the deployer's minted supply
#   when USDC_MINT_FILE is set. Staging uses Circle's devnet USDC — get it from
#   https://faucet.circle.com (no local mint authority).
#
# Env: WALLET           (required) the test wallet's Solana address
#      GORCHAIN_RPC     [http://localhost:8899]
#      SOLANA_RPC       [http://localhost:18899]
#      GOR_TARGET       [50]   SOL_TARGET [10]   SOL_CHUNK [10]   SOL_RETRIES [5]
#      USDC_MINT        ['']  the collateral mint address (enables USDC), or
#      USDC_MINT_FILE   ['']  path to the persisted warp-token-mint
#      DEPLOYER_KEYPAIR [~/.credentials/hyperlane/deployer-keypair.json]
#      USDC_TARGET      [100]
set -uo pipefail

WALLET="${WALLET:?set WALLET to the test wallet address (base58)}"
GORCHAIN_RPC="${GORCHAIN_RPC:-http://localhost:8899}"
SOLANA_RPC="${SOLANA_RPC:-http://localhost:18899}"
GOR_TARGET="${GOR_TARGET:-50}"
SOL_TARGET="${SOL_TARGET:-10}"
SOL_CHUNK="${SOL_CHUNK:-10}"
SOL_RETRIES="${SOL_RETRIES:-5}"
USDC_MINT="${USDC_MINT:-}"
USDC_MINT_FILE="${USDC_MINT_FILE:-}"
DEPLOYER_KEYPAIR="${DEPLOYER_KEYPAIR:-$HOME/.credentials/hyperlane/deployer-keypair.json}"
USDC_TARGET="${USDC_TARGET:-100}"

command -v solana >/dev/null || { echo "ERROR: solana CLI not found"; exit 1; }

balance_sol() {  # <addr> <rpc> — whole SOL rounded down; 0 if account absent
  local out
  out=$(solana balance "$1" --url "$2" 2>/dev/null) || { echo 0; return; }
  echo "$out" | awk '{print int($1)}'
}

rc=0
top_up() {  # <label> <target_sol> <rpc> <chunk> <max_retries>
  # Progress is measured by re-reading the balance, never by airdrop exit codes:
  # the gorchain faucet returns success while silently transferring nothing for
  # refused requests. max_retries bounds consecutive no-progress attempts.
  local label=$1 target=$2 rpc=$3 chunk=$4 max_retries=$5 have prev amount fails=0
  have=$(balance_sol "$WALLET" "$rpc")
  while [ "$have" -lt "$target" ] && [ "$fails" -lt "$max_retries" ]; do
    amount=$(( (target - have) < chunk ? (target - have) : chunk ))
    solana airdrop "$amount" "$WALLET" --url "$rpc" >/dev/null 2>&1 || true
    sleep 3
    prev=$have
    have=$(balance_sol "$WALLET" "$rpc")
    if [ "$have" -gt "$prev" ]; then fails=0; else fails=$(( fails + 1 )); fi
  done
  if [ "$have" -ge "$target" ]; then
    echo "  ✓ $label: $have SOL (>= $target)"
  else
    echo "  ✗ $label: have $have SOL, want $target — top up and re-run"
    rc=1
  fi
}

echo "Funding test wallet $WALLET"
echo "gorchain ($GORCHAIN_RPC):"
solana cluster-version --url "$GORCHAIN_RPC" >/dev/null 2>&1 \
  || { echo "ERROR: gorchain RPC unreachable — is the chain running?"; exit 1; }
top_up "GOR" "$GOR_TARGET" "$GORCHAIN_RPC" 10 5

echo "solana ($SOLANA_RPC):"
top_up "SOL" "$SOL_TARGET" "$SOLANA_RPC" "$SOL_CHUNK" "$SOL_RETRIES"

if [ -n "$USDC_MINT" ] || [ -n "$USDC_MINT_FILE" ]; then
  if [ -n "$USDC_MINT" ]; then
    MINT="$USDC_MINT"
  else
    [ -f "$USDC_MINT_FILE" ] || { echo "ERROR: $USDC_MINT_FILE not found — deploy the SPL token first"; exit 1; }
    MINT="$(tr -d '[:space:]' < "$USDC_MINT_FILE")"
  fi
  [ -f "$DEPLOYER_KEYPAIR" ] || { echo "ERROR: deployer keypair not found at $DEPLOYER_KEYPAIR"; exit 1; }
  command -v spl-token >/dev/null || { echo "ERROR: spl-token not found"; exit 1; }
  # Scoped CLI config: sign/pay as the deployer (the mint's supply holder) without
  # touching the operator's global solana config.
  CFG="$(mktemp)"
  trap 'rm -f "$CFG"' EXIT
  cat > "$CFG" <<EOF
---
json_rpc_url: $SOLANA_RPC
websocket_url: ''
keypair_path: $DEPLOYER_KEYPAIR
commitment: confirmed
EOF
  have=$(spl-token -C "$CFG" balance "$MINT" --owner "$WALLET" 2>/dev/null | awk '{print int($1)}')
  have="${have:-0}"
  if [ "$have" -ge "$USDC_TARGET" ]; then
    echo "  ✓ USDC: $have (>= $USDC_TARGET)"
  elif spl-token -C "$CFG" transfer "$MINT" "$(( USDC_TARGET - have ))" "$WALLET" \
         --fund-recipient --allow-unfunded-recipient >/dev/null; then
    echo "  ✓ USDC: $(spl-token -C "$CFG" balance "$MINT" --owner "$WALLET")"
  else
    echo "  ✗ USDC transfer failed (deployer supply exhausted?)"
    rc=1
  fi
else
  echo "  (USDC skipped — staging collateral is Circle's devnet USDC: https://faucet.circle.com)"
fi
exit "$rc"
