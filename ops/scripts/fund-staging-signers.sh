#!/usr/bin/env bash
# Fund the staging signers to per-chain TARGET BALANCES, reading addresses from
# the addresses.env gen-local-keys.sh wrote. Balance-driven and idempotent: each
# address is only topped up to its target, so re-runs never double-fund.
#
# gorchain (own faucet, capped 10 SOL/request): chunked airdrops, hard guarantee.
# solana devnet (public faucet, ~2 SOL/request, rate-limited): best effort with
# bounded retries; shortfalls are collected and reported with top-up guidance,
# and the script exits non-zero so the calling play fails visibly. Re-run after
# topping up from an operator devnet wallet or https://faucet.circle.com.
#
# Env: CRED_DIR        [~/.credentials/hyperlane]  holds addresses.env
#      GORCHAIN_RPC    [http://localhost:8899]
#      DEVNET_RPC      [https://api.devnet.solana.com]
#      ORACLE_PUBKEY   (required) the Privy IGP oracle's Solana pubkey
set -uo pipefail

CRED_DIR="${CRED_DIR:-$HOME/.credentials/hyperlane}"
GORCHAIN_RPC="${GORCHAIN_RPC:-http://localhost:8899}"
DEVNET_RPC="${DEVNET_RPC:-https://api.devnet.solana.com}"
ORACLE_PUBKEY="${ORACLE_PUBKEY:?set ORACLE_PUBKEY (the Privy IGP oracle pubkey)}"

command -v solana >/dev/null || { echo "ERROR: solana CLI not found"; exit 1; }
ADDR_FILE="$CRED_DIR/addresses.env"
[ -f "$ADDR_FILE" ] || { echo "ERROR: $ADDR_FILE not found — run gen-local-keys.sh first"; exit 1; }
# shellcheck disable=SC1090
source "$ADDR_FILE"

balance_sol() {  # <addr> <rpc> — whole SOL rounded down; 0 if account absent
  local out
  out=$(solana balance "$1" --url "$2" 2>/dev/null) || { echo 0; return; }
  echo "$out" | awk '{print int($1)}'
}

SHORTFALLS=()
top_up() {  # <label> <addr> <target_sol> <rpc> <chunk> <max_retries>
  local label=$1 addr=$2 target=$3 rpc=$4 chunk=$5 max_retries=$6 have need tries amount
  have=$(balance_sol "$addr" "$rpc")
  if [ "$have" -ge "$target" ]; then
    echo "  ✓ $label $addr: $have SOL (>= $target)"
    return 0
  fi
  need=$(( target - have ))
  while [ "$need" -gt 0 ]; do
    amount=$(( need < chunk ? need : chunk ))
    tries=0
    until solana airdrop "$amount" "$addr" --url "$rpc" >/dev/null 2>&1; do
      tries=$(( tries + 1 ))
      [ "$tries" -ge "$max_retries" ] && break
      sleep 3
    done
    [ "$tries" -ge "$max_retries" ] && break
    need=$(( need - amount ))
  done
  have=$(balance_sol "$addr" "$rpc")
  if [ "$have" -ge "$target" ]; then
    echo "  ✓ $label $addr: $have SOL (>= $target)"
  else
    echo "  ✗ $label $addr: have $have SOL, want $target"
    SHORTFALLS+=("$addr needs $(( target - have )) more SOL ($label)")
    return 1
  fi
}

rc=0
echo "Funding on gorchain ($GORCHAIN_RPC)..."
solana cluster-version --url "$GORCHAIN_RPC" >/dev/null 2>&1 \
  || { echo "ERROR: gorchain RPC unreachable — is the chain running?"; exit 1; }
top_up "deployer"          "$DEPLOYER_KEYPAIR_ADDR"   100 "$GORCHAIN_RPC" 10 5 || rc=1
top_up "gorchain validator" "$VALIDATOR_GORCHAIN_ADDR"  1 "$GORCHAIN_RPC" 10 5 || rc=1
top_up "relayer gorchain"  "$RELAYER_GORCHAIN_ADDR"     1 "$GORCHAIN_RPC" 10 5 || rc=1
top_up "IGP fee-claim"     "$RELAYER_FEE_CLAIM_ADDR"    1 "$GORCHAIN_RPC" 10 5 || rc=1
top_up "Privy IGP oracle"  "$ORACLE_PUBKEY"             1 "$GORCHAIN_RPC" 10 5 || rc=1

echo "Funding on solana devnet ($DEVNET_RPC)..."
top_up "deployer"          "$DEPLOYER_KEYPAIR_ADDR"    10 "$DEVNET_RPC" 2 3 || rc=1
top_up "solana validator"  "$VALIDATOR_SOLANA_ADDR"     1 "$DEVNET_RPC" 2 3 || rc=1
top_up "relayer solana"    "$RELAYER_SOLANA_ADDR"       1 "$DEVNET_RPC" 2 3 || rc=1
top_up "IGP fee-claim"     "$RELAYER_FEE_CLAIM_ADDR"    1 "$DEVNET_RPC" 2 3 || rc=1
top_up "Privy IGP oracle"  "$ORACLE_PUBKEY"             1 "$DEVNET_RPC" 2 3 || rc=1

if [ "$rc" -ne 0 ]; then
  echo ""
  echo "Underfunded (devnet faucet is rate-limited — top up from an operator"
  echo "devnet wallet, then re-run this play; funding is balance-driven):"
  printf '  %s\n' "${SHORTFALLS[@]}"
fi
exit "$rc"
