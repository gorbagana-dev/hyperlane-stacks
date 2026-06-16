#!/usr/bin/env bash
# Drain the deployer key's balance on both chains to a treasury address, leaving a
# small rent/fee buffer. The deployer key is one-shot: after deploy + ownership
# handoff no running pod needs it. REQUIRES an explicit treasury address.
#
# Env: DEPLOYER_KEYFILE  (required) path to deployer-keypair.json
#      TREASURY_ADDRESS  (required) base58 destination
#      GORCHAIN_RPC      [https://rpc.gorbagana.wtf]
#      SOLANA_RPC        (required) Helius mainnet RPC URL
#      RENT_BUFFER_SOL   [0.01] left behind on each chain
set -euo pipefail

DEPLOYER_KEYFILE="${DEPLOYER_KEYFILE:?set DEPLOYER_KEYFILE}"
TREASURY_ADDRESS="${TREASURY_ADDRESS:?set TREASURY_ADDRESS}"
GORCHAIN_RPC="${GORCHAIN_RPC:-https://rpc.gorbagana.wtf}"
SOLANA_RPC="${SOLANA_RPC:?set SOLANA_RPC (the Helius mainnet RPC URL)}"
RENT_BUFFER_SOL="${RENT_BUFFER_SOL:-0.01}"

command -v solana >/dev/null || { echo "ERROR: solana CLI not found"; exit 1; }
[ -f "$DEPLOYER_KEYFILE" ] || { echo "ERROR: $DEPLOYER_KEYFILE not found"; exit 1; }

drain() {  # <rpc> <label>
  local rpc=$1 label=$2 bal
  bal=$(solana balance "$DEPLOYER_KEYFILE" --url "$rpc" 2>/dev/null | awk '{print $1}') || bal=0
  echo "$label: deployer balance ${bal} SOL"
  # solana transfer with ALL leaves the account empty; keep a buffer by transferring
  # (balance - buffer) only when there is something worth moving.
  awk -v b="$bal" -v r="$RENT_BUFFER_SOL" 'BEGIN{exit !(b>r+0.001)}' || { echo "  nothing to drain"; return 0; }
  local amount
  amount=$(awk -v b="$bal" -v r="$RENT_BUFFER_SOL" 'BEGIN{printf "%.9f", b-r}')
  echo "  transferring ${amount} SOL to ${TREASURY_ADDRESS}"
  solana transfer "$TREASURY_ADDRESS" "$amount" \
    --from "$DEPLOYER_KEYFILE" --fee-payer "$DEPLOYER_KEYFILE" \
    --url "$rpc" --allow-unfunded-recipient
}

drain "$GORCHAIN_RPC" "gorchain"
drain "$SOLANA_RPC"   "solana"
echo "Drain confirmed on both chains."
