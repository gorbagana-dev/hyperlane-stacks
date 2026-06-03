#!/usr/bin/env bash
# Generate THROWAWAY test signing keys for the `local` own-chains environment.
#
# Writes the ed25519 keyfiles the local stack consumes into the operator
# credentials dir, then prints the pubkeys/addresses to paste into group_vars and
# to fund. TEST KEYS ONLY — never run this against a prod/staging credentials dir.
# It refuses to overwrite existing files, so it cannot clobber real keys.
#
# Privy-derived values (GORCHAIN/SOLANA_VALIDATOR_ADDRESS, IGP_ORACLE_PUBKEY) do
# NOT come from here — see ops/runbooks/privy-wallets.md.
#
# Usage:  ops/scripts/gen-local-keys.sh [--yes]
#         CRED_DIR=/path ops/scripts/gen-local-keys.sh   # override target dir
set -euo pipefail

CRED_DIR="${CRED_DIR:-$HOME/.credentials/hyperlane}"

command -v solana-keygen >/dev/null || { echo "ERROR: solana-keygen not found (install the Solana/Agave CLI)"; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }

cat <<BANNER
============================================================
 gen-local-keys — TEST signing keys for the LOCAL environment
 Target dir: $CRED_DIR
 Throwaway keys only. Do NOT use for prod/staging.
 Existing files are never overwritten.
============================================================
BANNER

if [ "${1:-}" != "--yes" ]; then
  read -r -p "Generate test keys in $CRED_DIR? Type 'yes': " ans
  [ "$ans" = "yes" ] || { echo "Aborted."; exit 1; }
fi

mkdir -p "$CRED_DIR"
chmod 700 "$CRED_DIR" 2>/dev/null || true

# rows of "label|file|address" for the closing summary
SUMMARY=()

# Solana keypair JSON (64-int array). Address is always re-derivable from the file.
gen_keypair_json() {
  local file="$CRED_DIR/$1" label="$2" addr
  if [ -e "$file" ]; then
    addr=$(solana-keygen pubkey "$file" 2>/dev/null || echo "?")
    echo "  exists:  $1  ($label)"
  else
    solana-keygen new --no-bip39-passphrase --silent --force -o "$file" >/dev/null
    addr=$(solana-keygen pubkey "$file")
    echo "  created: $1  ($label)"
  fi
  chmod 600 "$file" 2>/dev/null || true
  SUMMARY+=("$label|$1|$addr")
}

# Hyperlane HexKey: 0x + 32-byte ed25519 seed. The stack reads only the hex; the
# fund address is the seed's Solana pubkey, printed at creation time.
gen_hex_key() {
  local file="$CRED_DIR/$1" label="$2" tmp addr
  if [ -e "$file" ]; then
    echo "  exists:  $1  ($label)  [delete + rerun to re-show its address]"
    SUMMARY+=("$label|$1|(exists)")
    return
  fi
  tmp=$(mktemp)
  solana-keygen new --no-bip39-passphrase --silent --force -o "$tmp" >/dev/null
  python3 -c "import json; b=json.load(open('$tmp'))[:32]; open('$file','w').write('0x'+bytes(b).hex())"
  addr=$(solana-keygen pubkey "$tmp")
  rm -f "$tmp"
  chmod 600 "$file"
  echo "  created: $1  ($label)"
  SUMMARY+=("$label|$1|$addr")
}

echo "Generating keyfiles in $CRED_DIR ..."
gen_keypair_json deployer-keypair.json  "deployer — deploys programs; fund heavily"
gen_keypair_json hardware-wallet.json   "upgrade authority -> HARDWARE_WALLET_PUBKEY"
gen_keypair_json relayer-fee-claim.json "IGP fee-claim signer (RELAYER_KEYPAIR_JSON)"
gen_hex_key validator-gorchain.key      "gorchain validator announce (HYP_DEFAULTSIGNER_KEY)"
gen_hex_key validator-solana.key        "solana validator announce (HYP_DEFAULTSIGNER_KEY)"
gen_hex_key relayer-gorchain.key        "relayer gorchain signer (HYP_CHAINS_GORCHAIN_SIGNER_KEY)"
gen_hex_key relayer-solana.key          "relayer solana signer (HYP_CHAINS_SOLANA_SIGNER_KEY)"

hw_addr=""
for row in "${SUMMARY[@]}"; do
  IFS='|' read -r _ file addr <<<"$row"
  [ "$file" = "hardware-wallet.json" ] && hw_addr="$addr"
done

echo
echo "== Paste into ops/inventories/local/group_vars/all.yml =="
echo "  HARDWARE_WALLET_PUBKEY: \"$hw_addr\""
echo
echo "== Fund these on BOTH chains, e.g. solana airdrop 100 <deployer> / 1 <rest> =="
for row in "${SUMMARY[@]}"; do
  IFS='|' read -r label file addr <<<"$row"
  [ "$addr" = "(exists)" ] && continue
  printf "  %-44s # %s\n" "$addr" "$label"
done

cat <<NOTE

NOT generated here (come from Privy — see ops/runbooks/privy-wallets.md):
  GORCHAIN_VALIDATOR_ADDRESS / SOLANA_VALIDATOR_ADDRESS   (Privy EVM wallets)
  IGP_ORACLE_PUBKEY                                        (Privy Solana wallet)

The local stack wires NO dedicated IGP beneficiary key — fees accrue to the
deployer default. (e2e generates a separate igp-beneficiary key only to observe
fee-claim balance changes in its tests.)
NOTE
