#!/usr/bin/env bash
# Drain a signer's balance on ONE chain to a treasury, leaving a rent/fee buffer.
# Confirms the transfer (no --no-wait). Idempotent: a no-op when at/below the buffer.
#
# Handles both keyfile formats:
#   - Solana keypair JSON (deployer, fee-claim): used directly.
#   - Hyperlane HexKey "0x<seed>" (validator announce keys): the solana CLI cannot sign
#     with these, so we reconstruct a temp keypair JSON from the 32-byte seed + the cached
#     .pub address (written by gen-local-keys.sh), self-check it, drain, then shred it. If
#     the reconstruction is inconsistent the on-chain signature is rejected, so funds can
#     never be sent from the wrong account.
#
# Env: KEYFILE (req)  RPC_URL (req)  TREASURY_ADDRESS (req)  RENT_BUFFER_SOL [0.01]
set -euo pipefail

KEYFILE="${KEYFILE:?set KEYFILE}"
RPC_URL="${RPC_URL:?set RPC_URL}"
TREASURY_ADDRESS="${TREASURY_ADDRESS:?set TREASURY_ADDRESS}"
RENT_BUFFER_SOL="${RENT_BUFFER_SOL:-0.01}"

command -v solana >/dev/null || { echo "ERROR: solana CLI not found"; exit 1; }
command -v solana-keygen >/dev/null || { echo "ERROR: solana-keygen not found"; exit 1; }
[ -f "$KEYFILE" ] || { echo "ERROR: $KEYFILE not found"; exit 1; }

TMP_KP=""
cleanup() {
  [ -n "$TMP_KP" ] || return 0
  shred -u "$TMP_KP" 2>/dev/null || rm -f "$TMP_KP"
}
trap cleanup EXIT

# Resolve KEYFILE to a solana-CLI-usable keypair JSON (SIGNER).
if head -c2 "$KEYFILE" 2>/dev/null | grep -q '^0x'; then
  PUB=$(cat "$KEYFILE.pub" 2>/dev/null) || true
  [ -n "$PUB" ] || { echo "ERROR: $KEYFILE is a HexKey but $KEYFILE.pub (its address) is missing — cannot reconstruct the keypair"; exit 1; }
  TMP_KP=$(mktemp)
  python3 - "$KEYFILE" "$PUB" "$TMP_KP" <<'PY'
import sys, json
keyfile, pub_b58, out = sys.argv[1], sys.argv[2], sys.argv[3]
seed = bytes.fromhex(open(keyfile).read().strip()[2:])
assert len(seed) == 32, "expected 32-byte seed, got %d" % len(seed)
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
s = pub_b58.strip()
n = 0
for c in s:
    n = n * 58 + ALPHABET.index(c)
raw = n.to_bytes((n.bit_length() + 7) // 8, 'big') if n else b''
pad = len(s) - len(s.lstrip('1'))
pub = (b'\x00' * pad + raw).rjust(32, b'\x00')
assert len(pub) == 32, "expected 32-byte pubkey, got %d" % len(pub)
json.dump(list(seed) + list(pub), open(out, 'w'))
PY
  GOT=$(solana-keygen pubkey "$TMP_KP")
  [ "$GOT" = "$PUB" ] || { echo "ERROR: reconstructed address ($GOT) != cached ($PUB) — aborting, no funds moved"; exit 1; }
  SIGNER="$TMP_KP"
else
  SIGNER="$KEYFILE"
fi

bal=$(solana balance "$SIGNER" --url "$RPC_URL" 2>/dev/null | awk '{print $1}') || bal=0
[ -n "$bal" ] || bal=0
echo "$(basename "$KEYFILE") on $RPC_URL: balance ${bal} SOL"
awk -v b="$bal" -v r="$RENT_BUFFER_SOL" 'BEGIN{exit !(b>r+0.001)}' || { echo "  nothing to drain"; exit 0; }
amount=$(awk -v b="$bal" -v r="$RENT_BUFFER_SOL" 'BEGIN{printf "%.9f", b-r}')
echo "  transferring ${amount} SOL to ${TREASURY_ADDRESS}"
solana transfer "$TREASURY_ADDRESS" "$amount" \
  --from "$SIGNER" --fee-payer "$SIGNER" \
  --url "$RPC_URL" --allow-unfunded-recipient
echo "  drained."
