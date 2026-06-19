#!/usr/bin/env bash
# Verify the warp-route deployer key is funded to deploy a route on BOTH chains
# before the deployer runs. Adding a route costs the deployer ~3.3 per chain (rent
# for the warp program account; see funding-estimate.md), so a short key fails the
# deploy partway and can leave a half-deployed route (one chain's side without the
# other). REPORT ONLY — exits non-zero listing the shortfall so the calling play
# stops before anything is spent; the operator funds the deployer and re-runs
# (balance-driven: re-runs just re-check). The same keypair signs on both chains,
# so one derived address is checked against each RPC.
#
# MIN_BALANCE is a one-route baseline (+ buffer); selecting several new routes at
# once needs more — already-deployed routes self-skip, so it gates the next deploy.
#
# Env: CRED_DIR      [~/.credentials/hyperlane]  holds deployer-keypair.json
#      GORCHAIN_RPC  [https://rpc.gorbagana.wtf]
#      SOLANA_RPC    (required) the Solana RPC URL
#      MIN_BALANCE   [4]  per-chain target in native token (one route + buffer)
set -uo pipefail

CRED_DIR="${CRED_DIR:-$HOME/.credentials/hyperlane}"
GORCHAIN_RPC="${GORCHAIN_RPC:-https://rpc.gorbagana.wtf}"
SOLANA_RPC="${SOLANA_RPC:?set SOLANA_RPC (the Solana RPC URL)}"
MIN_BALANCE="${MIN_BALANCE:-4}"

command -v solana >/dev/null || { echo "ERROR: solana CLI not found"; exit 1; }
command -v solana-keygen >/dev/null || { echo "ERROR: solana-keygen not found"; exit 1; }
KEYPAIR="$CRED_DIR/deployer-keypair.json"
[ -f "$KEYPAIR" ] || { echo "ERROR: $KEYPAIR not found"; exit 1; }
DEPLOYER_ADDR=$(solana-keygen pubkey "$KEYPAIR") \
  || { echo "ERROR: could not derive deployer pubkey from $KEYPAIR"; exit 1; }

# Work in integer lamports so sub-token targets compare exactly; the target is
# written in whole tokens for readability and converted here.
to_lamports() { awk -v s="$1" 'BEGIN{printf "%d", s * 1000000000}'; }
fmt_sol()     { awk -v l="$1" 'BEGIN{printf "%.4f", l / 1000000000}'; }
balance_lamports() {  # <addr> <rpc> — integer lamports; 0 if account absent
  local out
  out=$(solana balance "$1" --url "$2" --lamports 2>/dev/null) || { echo 0; return; }
  echo "$out" | awk '{print $1 + 0}'
}

SHORTFALLS=()
check() {  # <chain-label> <rpc>
  local label=$1 rpc=$2 target have
  target=$(to_lamports "$MIN_BALANCE")
  have=$(balance_lamports "$DEPLOYER_ADDR" "$rpc")
  if [ "$have" -ge "$target" ]; then
    echo "  ✓ deployer $DEPLOYER_ADDR on $label: $(fmt_sol "$have") (>= $MIN_BALANCE)"
  else
    echo "  ✗ deployer $DEPLOYER_ADDR on $label: have $(fmt_sol "$have"), want $MIN_BALANCE"
    SHORTFALLS+=("deployer needs $(fmt_sol "$(( target - have ))") more on $label")
  fi
}

echo "Checking deployer funding (min $MIN_BALANCE per chain) before warp-route deploy..."
solana cluster-version --url "$GORCHAIN_RPC" >/dev/null 2>&1 \
  || { echo "ERROR: gorchain RPC unreachable"; exit 1; }
check "gorchain" "$GORCHAIN_RPC"
solana cluster-version --url "$SOLANA_RPC" >/dev/null 2>&1 \
  || { echo "ERROR: solana RPC unreachable"; exit 1; }
check "solana" "$SOLANA_RPC"

if [ "${#SHORTFALLS[@]}" -ne 0 ]; then
  echo ""
  echo "Deployer underfunded — fund it and re-run (a short deploy can leave a half-deployed route):"
  printf '  %s\n' "${SHORTFALLS[@]}"
  exit 1
fi
echo "Deployer funded on both chains."
exit 0
