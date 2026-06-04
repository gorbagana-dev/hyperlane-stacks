#!/usr/bin/env bash
# Create the collateral SPL token (USDC stand-in) on the local Solana chain and
# print its mint for WARP_TOKEN_MINT. The deployer keypair is the fee-payer, mint
# authority, and token-account owner — read straight from the keyfile, so nothing
# references a pubkey before it exists.
#
# Usage:
#   SOLANA_RPC=http://localhost:18899 \
#   DEPLOYER_KEYPAIR=~/.credentials/hyperlane/deployer-keypair.json \
#     ops/scripts/deploy-spl-token.sh [decimals] [supply]
set -euo pipefail

SOLANA_RPC="${SOLANA_RPC:-http://localhost:18899}"
DEPLOYER_KEYPAIR="${DEPLOYER_KEYPAIR:-$HOME/.credentials/hyperlane/deployer-keypair.json}"
DECIMALS="${1:-6}"
SUPPLY="${2:-1000000}"

command -v spl-token >/dev/null || { echo "ERROR: spl-token not found (install the SPL token CLI)"; exit 1; }
command -v solana-keygen >/dev/null || { echo "ERROR: solana-keygen not found"; exit 1; }
[ -f "$DEPLOYER_KEYPAIR" ] || { echo "ERROR: deployer keypair not found at $DEPLOYER_KEYPAIR (run gen-local-keys.sh first)"; exit 1; }

# Scoped solana CLI config so spl-token signs as the deployer without mutating the
# operator's global ~/.config/solana/cli/config.yml.
CFG="$(mktemp)"
trap 'rm -f "$CFG"' EXIT
cat > "$CFG" <<EOF
---
json_rpc_url: $SOLANA_RPC
websocket_url: ''
keypair_path: $DEPLOYER_KEYPAIR
commitment: confirmed
EOF

run() { spl-token -C "$CFG" "$@"; }

echo "Creating SPL token (decimals=$DECIMALS, authority=$(solana-keygen pubkey "$DEPLOYER_KEYPAIR")) ..."
create_out="$(run create-token --decimals "$DECIMALS")"
echo "$create_out"
mint="$(printf '%s\n' "$create_out" | awk '/Creating token/{print $3; exit}')"
[ -n "$mint" ] || { echo "ERROR: could not parse the mint address from create-token output" >&2; exit 1; }

echo "Creating the deployer's token account ..."
run create-account "$mint"
echo "Minting $SUPPLY ..."
run mint "$mint" "$SUPPLY"

echo
echo "== SPL token deployed =="
echo "  mint: $mint"
echo "  -> set WARP_TOKEN_MINT to this in the warp-deployer spec (deployment/<env>/spec-warp-usdc.yml)"
# machine-readable trailer for the prepare-chains playbook to capture
echo "WARP_TOKEN_MINT=$mint"
