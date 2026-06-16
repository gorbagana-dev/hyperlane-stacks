#!/usr/bin/env bash
# Generate hot signing keys for the hot-key-signer environments (local, staging,
# and prod). Prod hot signers (deployer, relayer x3, validator announce x2) are
# generated on the deploy host by prepare-prod.yml — they are hot keys in every
# env and must live in the on-host credentials dir for laconic-so to read them.
# It refuses to overwrite existing files, so it cannot clobber funded keys.
#
# Writes the ed25519 keyfiles the local stack consumes into the operator
# credentials dir, prints the addresses to fund, and exports them to
# $CRED_DIR/addresses.env (shell-sourceable, one place for funding commands
# to read from).
# It refuses to overwrite existing files, so it cannot clobber real keys.
#
# Privy-derived values (GORCHAIN/SOLANA_VALIDATOR_ADDRESS, IGP_ORACLE_PUBKEY,
# BRIDGE_OWNER_PUBKEY) do NOT come from here — see ops/runbooks/privy-wallets.md.
#
# Usage:  ops/scripts/gen-local-keys.sh [--yes] [--fund --oracle <IGP_ORACLE_PUBKEY>]
#         CRED_DIR=/path ops/scripts/gen-local-keys.sh   # override target dir
#
# With --fund, after generating it funds every on-chain signer (deployer 100,
# the rest 1) plus the Privy oracle on BOTH chains via fund-test-wallets.sh —
# the chains must already be running. Honors GORCHAIN_RPC/SOLANA_RPC overrides.
set -euo pipefail

CRED_DIR="${CRED_DIR:-$HOME/.credentials/hyperlane}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

command -v solana-keygen >/dev/null || { echo "ERROR: solana-keygen not found (install the Solana/Agave CLI)"; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }

ASSUME_YES=false
FUND=false
ORACLE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes) ASSUME_YES=true ;;
    --fund) FUND=true ;;
    --oracle) ORACLE="${2:-}"; shift ;;
    --oracle=*) ORACLE="${1#--oracle=}" ;;
    *) echo "ERROR: unknown argument '$1'" >&2; exit 1 ;;
  esac
  shift
done

if [ "$FUND" = true ] && [ -z "$ORACLE" ]; then
  echo "ERROR: --fund requires --oracle <IGP_ORACLE_PUBKEY> (the Privy wallet, not a local keyfile)" >&2
  exit 1
fi

cat <<BANNER
============================================================
 gen-local-keys — hot signing keys (local / staging / prod)
 Target dir: $CRED_DIR
 Existing files are never touched.
============================================================
BANNER

if [ "$ASSUME_YES" != true ]; then
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
# fund address is the seed's Solana pubkey. We can't cheaply re-derive it from the
# bare seed, so the address is cached in a sibling .pub at creation — that lets a
# later --fund run recover it without regenerating the key.
gen_hex_key() {
  local file="$CRED_DIR/$1" label="$2" tmp addr
  if [ -e "$file" ]; then
    addr=$(cat "$file.pub" 2>/dev/null || true)
    if [ -n "$addr" ]; then
      echo "  exists:  $1  ($label)"
    else
      echo "  exists:  $1  ($label)  [no .pub sidecar — delete + rerun to recover its address]"
      addr="(exists)"
    fi
    chmod 600 "$file" 2>/dev/null || true
    SUMMARY+=("$label|$1|$addr")
    return
  fi
  tmp=$(mktemp)
  solana-keygen new --no-bip39-passphrase --silent --force -o "$tmp" >/dev/null
  python3 -c "import json; b=json.load(open('$tmp'))[:32]; open('$file','w').write('0x'+bytes(b).hex())"
  addr=$(solana-keygen pubkey "$tmp")
  rm -f "$tmp"
  chmod 600 "$file"
  printf '%s\n' "$addr" > "$file.pub"
  echo "  created: $1  ($label)"
  SUMMARY+=("$label|$1|$addr")
}

echo "Generating keyfiles in $CRED_DIR ..."
gen_keypair_json deployer-keypair.json  "deployer — deploys programs; fund heavily"
gen_keypair_json relayer-fee-claim.json "IGP fee-claim signer (RELAYER_KEYPAIR_JSON)"
gen_hex_key validator-gorchain.key      "gorchain validator announce (HYP_DEFAULTSIGNER_KEY)"
gen_hex_key validator-solana.key        "solana validator announce (HYP_DEFAULTSIGNER_KEY)"
gen_hex_key relayer-gorchain.key        "relayer gorchain signer (HYP_CHAINS_GORCHAIN_SIGNER_KEY)"
gen_hex_key relayer-solana.key          "relayer solana signer (HYP_CHAINS_SOLANA_SIGNER_KEY)"

# Sourceable address export — regenerated every run (addresses are derived data,
# never secret). Var name from the filename: deployer-keypair.json -> DEPLOYER_KEYPAIR_ADDR.
ADDR_FILE="$CRED_DIR/addresses.env"
{
  echo "# generated by gen-local-keys.sh — addresses of the hot signing keys"
  for row in "${SUMMARY[@]}"; do
    IFS='|' read -r label file addr <<<"$row"
    case "$addr" in ""|"?"|"(exists)") continue ;; esac
    var=$(basename "$file" | sed 's/\.[^.]*$//' | tr 'a-z-' 'A-Z_')
    echo "${var}_ADDR=$addr  # $label"
  done
} > "$ADDR_FILE"
echo
echo "Addresses exported to $ADDR_FILE"

# Assemble <address> <amount> pairs for the funder: deployer 100, everything else 1.
FUND_PAIRS=()
for row in "${SUMMARY[@]}"; do
  IFS='|' read -r label file addr <<<"$row"
  [ "$addr" = "(exists)" ] && continue
  if [ "$file" = "deployer-keypair.json" ]; then
    FUND_PAIRS+=("$addr" 100)
  else
    FUND_PAIRS+=("$addr" 1)
  fi
done
[ -n "$ORACLE" ] && FUND_PAIRS+=("$ORACLE" 1)

if [ "$FUND" = true ]; then
  echo
  echo "== Funding all on-chain signers + oracle on BOTH chains =="
  "$SCRIPT_DIR/fund-test-wallets.sh" "${FUND_PAIRS[@]}"
else
  echo
  echo "== Fund these on BOTH chains (run with --fund --oracle <pubkey> to do it now) =="
  for row in "${SUMMARY[@]}"; do
    IFS='|' read -r label file addr <<<"$row"
    [ "$addr" = "(exists)" ] && continue
    printf "  %-44s # %s\n" "$addr" "$label"
  done
fi

cat <<NOTE

NOT generated here (come from Privy — see ops/runbooks/privy-wallets.md):
  GORCHAIN_VALIDATOR_ADDRESS / SOLANA_VALIDATOR_ADDRESS   (Privy EVM wallets)
  IGP_ORACLE_PUBKEY                                        (Privy Solana wallet)
  BRIDGE_OWNER_PUBKEY                                      (Privy Solana wallet)

The local stack wires NO dedicated IGP beneficiary key — fees accrue to the
deployer default. (e2e generates a separate igp-beneficiary key only to observe
fee-claim balance changes in its tests.)
NOTE
