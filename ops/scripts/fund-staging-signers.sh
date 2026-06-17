#!/usr/bin/env bash
# Fund the staging signers to per-chain TARGET BALANCES, reading addresses from
# the addresses.env gen-local-keys.sh wrote. Balance-driven and idempotent: each
# address is only topped up to its target, so re-runs never double-fund.
#
# gorchain (own faucet, capped 10 SOL/request): chunked airdrops, hard guarantee.
# solana devnet (public faucet, ~2 SOL/request, rate-limited): best effort with
# bounded retries; shortfalls are collected and reported with top-up guidance,
# and the script exits non-zero so the calling play fails visibly. Re-run after
# topping up from an operator devnet wallet or https://faucet.solana.com.
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
# gen-local-keys.sh omits any address it could not derive — fail up front with
# the gap named instead of an unbound-variable abort mid-funding.
for v in DEPLOYER_KEYPAIR_ADDR VALIDATOR_GORCHAIN_ADDR VALIDATOR_SOLANA_ADDR \
         RELAYER_GORCHAIN_ADDR RELAYER_SOLANA_ADDR RELAYER_FEE_CLAIM_ADDR; do
  [ -n "${!v:-}" ] || { echo "ERROR: $v missing from $ADDR_FILE — regenerate that key (see gen-local-keys.sh output) and re-run"; exit 1; }
done

# Work in integer lamports so sub-SOL targets (e.g. the validator's 0.1) compare
# exactly; targets and chunks are written in SOL for readability and converted here.
to_lamports() { awk -v s="$1" 'BEGIN{printf "%d", s * 1000000000}'; }
fmt_sol()     { awk -v l="$1" 'BEGIN{printf "%.4f", l / 1000000000}'; }
balance_lamports() {  # <addr> <rpc> — integer lamports; 0 if account absent
  local out
  out=$(solana balance "$1" --url "$2" --lamports 2>/dev/null) || { echo 0; return; }
  echo "$out" | awk '{print $1 + 0}'
}

SHORTFALLS=()
top_up() {  # <label> <addr> <target_sol> <rpc> <chunk_sol> <max_retries>
  # Progress is measured by re-reading the balance, never by airdrop exit codes:
  # the gorchain faucet returns success while silently transferring nothing for
  # refused requests. max_retries bounds consecutive no-progress attempts.
  local label=$1 addr=$2 target_sol=$3 rpc=$4 chunk_sol=$5 max_retries=$6
  local target chunk have prev amount_l amount fails=0
  target=$(to_lamports "$target_sol"); chunk=$(to_lamports "$chunk_sol")
  have=$(balance_lamports "$addr" "$rpc")
  while [ "$have" -lt "$target" ] && [ "$fails" -lt "$max_retries" ]; do
    amount_l=$(( (target - have) < chunk ? (target - have) : chunk ))
    amount=$(fmt_sol "$amount_l")
    solana airdrop "$amount" "$addr" --url "$rpc" >/dev/null 2>&1 || true
    sleep 3
    prev=$have
    have=$(balance_lamports "$addr" "$rpc")
    if [ "$have" -gt "$prev" ]; then fails=0; else fails=$(( fails + 1 )); fi
  done
  if [ "$have" -ge "$target" ]; then
    echo "  ✓ $label $addr: $(fmt_sol "$have") SOL (>= $target_sol)"
  else
    echo "  ✗ $label $addr: have $(fmt_sol "$have") SOL, want $target_sol"
    SHORTFALLS+=("$addr needs $(fmt_sol "$(( target - have ))") more SOL ($label)")
    return 1
  fi
}

rc=0
echo "Funding on gorchain ($GORCHAIN_RPC)..."
solana cluster-version --url "$GORCHAIN_RPC" >/dev/null 2>&1 \
  || { echo "ERROR: gorchain RPC unreachable — is the chain running?"; exit 1; }
top_up "deployer"          "$DEPLOYER_KEYPAIR_ADDR"   100 "$GORCHAIN_RPC" 10 5 || rc=1
top_up "gorchain validator" "$VALIDATOR_GORCHAIN_ADDR" 0.1 "$GORCHAIN_RPC" 10 5 || rc=1
top_up "relayer gorchain"  "$RELAYER_GORCHAIN_ADDR"     1 "$GORCHAIN_RPC" 10 5 || rc=1
top_up "IGP fee-claim"     "$RELAYER_FEE_CLAIM_ADDR"    1 "$GORCHAIN_RPC" 10 5 || rc=1
top_up "Privy IGP oracle"  "$ORACLE_PUBKEY"             1 "$GORCHAIN_RPC" 10 5 || rc=1

echo "Funding on solana devnet ($DEVNET_RPC)..."
top_up "deployer"          "$DEPLOYER_KEYPAIR_ADDR"    10 "$DEVNET_RPC" 2 3 || rc=1
top_up "solana validator"  "$VALIDATOR_SOLANA_ADDR"    0.1 "$DEVNET_RPC" 2 3 || rc=1
top_up "relayer solana"    "$RELAYER_SOLANA_ADDR"       1 "$DEVNET_RPC" 2 3 || rc=1
top_up "IGP fee-claim"     "$RELAYER_FEE_CLAIM_ADDR"    1 "$DEVNET_RPC" 2 3 || rc=1
top_up "Privy IGP oracle"  "$ORACLE_PUBKEY"             1 "$DEVNET_RPC" 2 3 || rc=1

if [ "$rc" -ne 0 ]; then
  echo ""
  echo "Underfunded (devnet faucet is rate-limited — top up from an operator"
  echo "devnet wallet or https://faucet.solana.com, then re-run this play;"
  echo "funding is balance-driven):"
  printf '  %s\n' "${SHORTFALLS[@]}"
fi
exit "$rc"
