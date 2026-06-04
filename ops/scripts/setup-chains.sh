#!/usr/bin/env bash
# Stand up the local own-chains the bridge deploys against: gorchain (the agave
# fork, via gorchain-stacks) and a solana-test-validator. Run on the box that
# hosts the chains — single-host: local-1; multi-host: the out-of-band chains box.
# Idempotent: re-running recreates the gorchain deployment and restarts solana.
#
# The gorchain dev-RPC settings go in the deploy spec's `config:` (laconic-so
# writes them to config.env itself) — no hand-written config.env.
#
# Requires: laconic-so, docker, solana CLI, git (SSH for fetch-stack). The
# gorchain images are private, so GHCR auth is needed:
#   GHCR_USER=<gh-user> GHCR_PAT=<pat> ops/scripts/setup-chains.sh
#
# Env knobs [default]:
#   CHAINS_DIR        [./chains]   working dir: gorchain deployment + solana ledger
#   GORCHAIN_RPC_PORT [8899]   SOLANA_RPC_PORT [18899]
#   SOLANA_FAUCET_PORT [19900] SOLANA_GOSSIP_PORT [18001]
#   GORCHAIN_STACK_REF [github.com/gorbagana-dev/gorchain-stacks@main]
#   SKIP_GORCHAIN / SKIP_SOLANA  [unset]   set to skip one chain
set -euo pipefail

CHAINS_DIR="${CHAINS_DIR:-./chains}"
GORCHAIN_RPC_PORT="${GORCHAIN_RPC_PORT:-8899}"
SOLANA_RPC_PORT="${SOLANA_RPC_PORT:-18899}"
SOLANA_FAUCET_PORT="${SOLANA_FAUCET_PORT:-19900}"
SOLANA_GOSSIP_PORT="${SOLANA_GOSSIP_PORT:-18001}"
GORCHAIN_STACK_REF="${GORCHAIN_STACK_REF:-github.com/gorbagana-dev/gorchain-stacks@main}"

GORCHAIN_RPC="http://localhost:${GORCHAIN_RPC_PORT}"
SOLANA_RPC="http://localhost:${SOLANA_RPC_PORT}"

for bin in laconic-so docker solana solana-test-validator; do
  command -v "$bin" >/dev/null || { echo "ERROR: '$bin' not found in PATH"; exit 1; }
done

mkdir -p "$CHAINS_DIR"
CHAINS_DIR="$(cd "$CHAINS_DIR" && pwd)"   # absolute, so detached solana survives a cwd change

# Wait for an RPC to answer /health, then assert slots actually advance. A healthy
# RPC that isn't producing blocks can't fund or land transactions.
wait_for_chain() {  # <rpc> <name>
  local rpc=$1 name=$2 i
  echo "  waiting for $name RPC at $rpc ..."
  for i in $(seq 1 120); do
    curl -fS "$rpc/health" >/dev/null 2>&1 && break
    sleep 2
    if [ "$i" -eq 120 ]; then
      echo "  ✗ $name RPC never became healthy. Last response:"
      curl -i "$rpc/health" || true        # no -s: show exactly what came back
      return 1
    fi
  done
  local s1 s2
  s1=$(solana slot --url "$rpc" 2>/dev/null || echo 0)
  sleep 4
  s2=$(solana slot --url "$rpc" 2>/dev/null || echo 0)
  if [ "$s2" -gt "$s1" ]; then
    echo "  ✓ $name healthy and producing blocks (slot $s1 -> $s2)"
  else
    echo "  ✗ $name healthy but slots are NOT advancing ($s1 -> $s2) — not producing blocks" >&2
    return 1
  fi
}

start_gorchain() {
  echo "== gorchain =="
  if [ -n "${GHCR_PAT:-}" ]; then
    GHCR_USER="${GHCR_USER:-gorbagana-dev}"   # GHCR auths by the PAT; the user just needs to be non-empty
    echo "  logging in to ghcr.io as ${GHCR_USER} for the private gorchain images"
    echo "$GHCR_PAT" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin
  else
    echo "  WARNING: GHCR_PAT unset — image pull will fail if not already cached" >&2
  fi

  echo "  fetching gorchain stack ($GORCHAIN_STACK_REF) ..."
  laconic-so fetch-stack --git-ssh --pull "$GORCHAIN_STACK_REF"
  local stack_path="$HOME/cerc/gorchain-stacks/stack-orchestrator/stacks/gorchain"
  [ -d "$stack_path" ] || { echo "ERROR: gorchain stack not found at $stack_path"; exit 1; }

  local deploy_dir="$CHAINS_DIR/gorchain"
  local spec="$CHAINS_DIR/gorchain-spec.yml"
  [ -d "$deploy_dir" ] && { echo "  removing stale deployment $deploy_dir"; laconic-so deployment --dir "$deploy_dir" stop --delete-volumes 2>/dev/null || true; rm -rf "$deploy_dir"; }

  # Dev-RPC spec: the chain config lives in config: (laconic-so renders config.env
  # from it), NOT a hand-written config.env. GORCHAIN_DEV_RPC enables full client
  # RPC on the single producer; ENABLE_FAUCET gives requestAirdrop.
  cat > "$spec" <<EOF
stack: $stack_path
deploy-to: compose
network:
  ports:
    agave-validator:
      - '${GORCHAIN_RPC_PORT}'
      - '8900'
      - '8001'
      - '9900'
config:
  CLUSTER_TYPE: testnet
  RPC_PORT: '${GORCHAIN_RPC_PORT}'
  RPC_WS_PORT: '8900'
  GOSSIP_PORT: '8001'
  DYNAMIC_PORT_RANGE: '9050-9075'
  PUBLIC_GOSSIP_HOST: '127.0.0.1'
  PUBLIC_RPC_ADDRESS: '127.0.0.1:${GORCHAIN_RPC_PORT}'
  GORCHAIN_DEV_RPC: 'true'
  ENABLE_FAUCET: 'true'
EOF

  echo "  deploy create ..."
  laconic-so --stack "$stack_path" deploy create --spec-file "$spec" --deployment-dir "$deploy_dir"
  echo "  deploy start ..."
  laconic-so deployment --dir "$deploy_dir" start
  wait_for_chain "$GORCHAIN_RPC" gorchain
}

start_solana() {
  echo "== solana-test-validator =="
  local ledger="$CHAINS_DIR/data/test-ledger-solana"
  mkdir -p "$CHAINS_DIR/data"

  # already healthy? leave it (preserves deployed programs/accounts)
  if curl -fS "$SOLANA_RPC/health" >/dev/null 2>&1; then
    echo "  already running at $SOLANA_RPC"
  else
    echo "  starting (ledger: $ledger) ..."
    # setsid + nohup so it outlives this script (and the ansible SSH session)
    setsid nohup solana-test-validator \
      --ledger "$ledger" \
      --rpc-port "$SOLANA_RPC_PORT" \
      --faucet-port "$SOLANA_FAUCET_PORT" \
      --gossip-port "$SOLANA_GOSSIP_PORT" \
      --dynamic-port-range 19050-19075 \
      --bind-address 0.0.0.0 \
      --quiet >"$CHAINS_DIR/data/solana-validator.log" 2>&1 < /dev/null &
    disown || true
  fi
  wait_for_chain "$SOLANA_RPC" solana
}

[ -n "${SKIP_GORCHAIN:-}" ] || start_gorchain
[ -n "${SKIP_SOLANA:-}" ]   || start_solana

echo
echo "Chains up:"
echo "  gorchain : $GORCHAIN_RPC"
echo "  solana   : $SOLANA_RPC"
