#!/usr/bin/env bash
# Fund wallets on the local own-chains (gorchain + solana-test-validator).
#
# gorchain's faucet caps each airdrop at 10 SOL (solana-faucet --per-request-cap
# 10), so this airdrops in chunks of 10 and verifies the resulting balance. Plain
# `solana airdrop 100` against gorchain returns a signature but the faucet refuses
# the over-cap request, leaving the balance silently unchanged — chunking avoids
# that. Each chunk is retried a few times to ride out a faucet/validator hiccup.
#
# Funds every address on BOTH chains (the deployer, oracle, etc. must exist on
# gorchain and solana for the bridge to work).
#
# Usage:
#   ops/scripts/fund-test-wallets.sh <address> <amount_sol> [<address> <amount_sol> ...]
#
# Example — deployer 100, Privy oracle 1:
#   ops/scripts/fund-test-wallets.sh \
#     4tHX2HafcHF5Pjr5wTg5XGWvEebigsH25S41rDWW3a2L 100 \
#     5VcYt3R4fKjQ7ANDcFveyu8hxVFvnDyruN7E8jjdJbQP 1
#
# Override the RPCs (defaults match the local runbook):
#   GORCHAIN_RPC=http://localhost:8899 SOLANA_RPC=http://localhost:18899 \
#     ops/scripts/fund-test-wallets.sh <address> <amount_sol> ...
set -euo pipefail

GORCHAIN_RPC="${GORCHAIN_RPC:-http://localhost:8899}"
SOLANA_RPC="${SOLANA_RPC:-http://localhost:18899}"
CHUNK=10
MAX_RETRIES=5

command -v solana >/dev/null || { echo "ERROR: solana CLI not found (install the Solana/Agave CLI)"; exit 1; }

if [ "$#" -lt 2 ] || [ $(( $# % 2 )) -ne 0 ]; then
  echo "Usage: $0 <address> <amount_sol> [<address> <amount_sol> ...]" >&2
  exit 1
fi

# whole-SOL balance rounded down; 0 if the account doesn't exist yet
balance_sol() {  # <addr> <rpc>
  local out
  out=$(solana balance "$1" --url "$2" 2>/dev/null) || { echo 0; return; }
  echo "$out" | awk '{print int($1)}'
}

fund() {  # <amount_sol> <addr> <rpc> <chain-label>
  local want=$1 addr=$2 rpc=$3 label=$4 remaining=$1 chunk tries got
  while [ "$remaining" -gt 0 ]; do
    chunk=$(( remaining < CHUNK ? remaining : CHUNK ))
    tries=0
    until solana airdrop "$chunk" "$addr" --url "$rpc" >/dev/null 2>&1; do
      tries=$(( tries + 1 ))
      if [ "$tries" -ge "$MAX_RETRIES" ]; then
        echo "  ✗ $label $addr: airdrop failed ${MAX_RETRIES}x (faucet down? chain not ready?)" >&2
        return 1
      fi
      sleep 2
    done
    remaining=$(( remaining - chunk ))
  done
  got=$(balance_sol "$addr" "$rpc")
  if [ "$got" -ge "$want" ]; then
    echo "  ✓ $label $addr: $got SOL"
  else
    echo "  ✗ $label $addr: wanted >=$want SOL, have $got SOL" >&2
    return 1
  fi
}

rc=0
for rpc_pair in "gorchain|$GORCHAIN_RPC" "solana|$SOLANA_RPC"; do
  label="${rpc_pair%%|*}"
  rpc="${rpc_pair##*|}"
  echo "Funding on $label ($rpc)..."
  if ! solana cluster-version --url "$rpc" >/dev/null 2>&1; then
    echo "  ✗ $rpc unreachable — is the chain running?" >&2
    rc=1
    continue
  fi
  i=1
  while [ "$i" -lt "$#" ]; do
    j=$(( i + 1 ))
    fund "${!j}" "${!i}" "$rpc" "$label" || rc=1
    i=$(( i + 2 ))
  done
done

exit "$rc"
