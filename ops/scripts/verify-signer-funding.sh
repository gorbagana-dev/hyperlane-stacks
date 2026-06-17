#!/usr/bin/env bash
# Verify the prod hot signers are funded to their per-chain TARGET BALANCES,
# reading addresses from the addresses.env that gen-local-keys.sh wrote. REPORT
# ONLY — never airdrops (mainnet has no faucet). Exits non-zero listing every
# shortfall so the calling play fails visibly; the operator funds from a treasury
# and re-runs (balance-driven: re-runs just re-check).
#
# Env: CRED_DIR       [~/.credentials/hyperlane]  holds addresses.env
#      GORCHAIN_RPC   [https://rpc.gorbagana.wtf]
#      SOLANA_RPC     (required) the Helius mainnet RPC URL
#      ORACLE_PUBKEY  (required) the Privy IGP oracle's Solana pubkey
set -uo pipefail

CRED_DIR="${CRED_DIR:-$HOME/.credentials/hyperlane}"
GORCHAIN_RPC="${GORCHAIN_RPC:-https://rpc.gorbagana.wtf}"
SOLANA_RPC="${SOLANA_RPC:?set SOLANA_RPC (the Helius mainnet RPC URL)}"
ORACLE_PUBKEY="${ORACLE_PUBKEY:?set ORACLE_PUBKEY (the Privy IGP oracle pubkey)}"

command -v solana >/dev/null || { echo "ERROR: solana CLI not found"; exit 1; }
ADDR_FILE="$CRED_DIR/addresses.env"
[ -f "$ADDR_FILE" ] || { echo "ERROR: $ADDR_FILE not found — run prepare-prod.yml first"; exit 1; }
# shellcheck disable=SC1090
source "$ADDR_FILE"
for v in DEPLOYER_KEYPAIR_ADDR VALIDATOR_GORCHAIN_ADDR VALIDATOR_SOLANA_ADDR \
         RELAYER_GORCHAIN_ADDR RELAYER_SOLANA_ADDR RELAYER_FEE_CLAIM_ADDR; do
  [ -n "${!v:-}" ] || { echo "ERROR: $v missing from $ADDR_FILE — regenerate that key and re-run"; exit 1; }
done

# Work in integer lamports so sub-SOL targets (e.g. the validator's 0.1) compare
# exactly; targets are written in SOL for readability and converted here.
to_lamports() { awk -v s="$1" 'BEGIN{printf "%d", s * 1000000000}'; }
fmt_sol()     { awk -v l="$1" 'BEGIN{printf "%.4f", l / 1000000000}'; }
balance_lamports() {  # <addr> <rpc> — integer lamports; 0 if account absent
  local out
  out=$(solana balance "$1" --url "$2" --lamports 2>/dev/null) || { echo 0; return; }
  echo "$out" | awk '{print $1 + 0}'
}

SHORTFALLS=()
check() {  # <label> <addr> <target_sol> <rpc>
  local label=$1 addr=$2 target_sol=$3 rpc=$4 target have
  target=$(to_lamports "$target_sol")
  have=$(balance_lamports "$addr" "$rpc")
  if [ "$have" -ge "$target" ]; then
    echo "  ✓ $label $addr: $(fmt_sol "$have") SOL (>= $target_sol)"
  else
    echo "  ✗ $label $addr: have $(fmt_sol "$have") SOL, want $target_sol"
    SHORTFALLS+=("$addr needs $(fmt_sol "$(( target - have ))") more SOL ($label)")
  fi
}

echo "Checking funding on gorchain ($GORCHAIN_RPC)..."
solana cluster-version --url "$GORCHAIN_RPC" >/dev/null 2>&1 \
  || { echo "ERROR: gorchain RPC unreachable"; exit 1; }
check "deployer"           "$DEPLOYER_KEYPAIR_ADDR"  100 "$GORCHAIN_RPC"
check "gorchain validator" "$VALIDATOR_GORCHAIN_ADDR" 0.1 "$GORCHAIN_RPC"
check "relayer gorchain"   "$RELAYER_GORCHAIN_ADDR"    1 "$GORCHAIN_RPC"
check "IGP fee-claim"      "$RELAYER_FEE_CLAIM_ADDR"   1 "$GORCHAIN_RPC"
check "Privy IGP oracle"   "$ORACLE_PUBKEY"            1 "$GORCHAIN_RPC"

echo "Checking funding on solana mainnet..."
solana cluster-version --url "$SOLANA_RPC" >/dev/null 2>&1 \
  || { echo "ERROR: solana RPC unreachable"; exit 1; }
check "deployer"           "$DEPLOYER_KEYPAIR_ADDR"   10 "$SOLANA_RPC"
check "solana validator"   "$VALIDATOR_SOLANA_ADDR"   0.1 "$SOLANA_RPC"
check "relayer solana"     "$RELAYER_SOLANA_ADDR"      1 "$SOLANA_RPC"
check "IGP fee-claim"      "$RELAYER_FEE_CLAIM_ADDR"   1 "$SOLANA_RPC"
check "Privy IGP oracle"   "$ORACLE_PUBKEY"            1 "$SOLANA_RPC"

if [ "${#SHORTFALLS[@]}" -ne 0 ]; then
  echo ""
  echo "Underfunded (mainnet has no faucet — fund from a treasury wallet, then re-run):"
  printf '  %s\n' "${SHORTFALLS[@]}"
  exit 1
fi
echo "All signers funded to target."
exit 0
