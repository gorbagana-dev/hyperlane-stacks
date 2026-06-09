#!/bin/bash
set -euo pipefail

STATE_DIR="${STATE_OUTPUT_DIR:-/state}"
LOGS_DIR="${LOGS_OUTPUT_DIR:-/logs}"
mkdir -p "${STATE_DIR}" "${LOGS_DIR}"

LOG_FILE="${LOGS_DIR}/svm-warp-deployer-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "${LOG_FILE}") 2>&1
echo "Logging to ${LOG_FILE}"

echo "=== Hyperlane SVM Warp Route Deployer ==="

# -------------------------------------------------------
# Prerequisite check: core deployment must exist
# -------------------------------------------------------
echo ""
echo "=== Checking core deployment artifacts ==="
PROGRAM_IDS_FILE="${STATE_DIR}/program-ids.json"
if [ ! -s "${PROGRAM_IDS_FILE}" ]; then
  echo "ERROR: ${PROGRAM_IDS_FILE} missing. Run the hyperlane-svm-deployer stack first."
  exit 1
fi

# Resolve a chain's RPC URL and domain id from the global chain config by name,
# e.g. gorchain -> $GORCHAIN_RPC_URL / $GORCHAIN_DOMAIN_ID.
chain_var() {  # $1=chain  $2=suffix(RPC_URL|DOMAIN_ID)
  upper=$(echo "$1" | tr '[:lower:]' '[:upper:]')
  varname="${upper}_${2}"
  printf '%s' "${!varname:-}"
}

# -------------------------------------------------------
# Write deployer keypair to file
# -------------------------------------------------------
DEPLOYER_KEY_FILE="/tmp/deployer-keypair.json"
echo "${DEPLOYER_KEYPAIR}" > "${DEPLOYER_KEY_FILE}"
chmod 600 "${DEPLOYER_KEY_FILE}"

WORK_DIR="/tmp/hyperlane-warp-deploy"
ENVIRONMENTS_DIR="${WORK_DIR}/environments"
ENVIRONMENT="e2e"
mkdir -p "${ENVIRONMENTS_DIR}" "${WORK_DIR}/output"

# -------------------------------------------------------
# Render config templates via envsubst
# The ConfigMap mounts contain .tmpl files with ${VAR} placeholders.
# envsubst substitutes them with env vars from the spec config.
# -------------------------------------------------------
echo ""
echo "=== Rendering config templates ==="

# Registry: CLI expects {registry}/chains/metadata.yaml
REGISTRY_DIR="/tmp/registry"
mkdir -p "${REGISTRY_DIR}/chains"
envsubst < /config/registry/metadata.yaml.tmpl > "${REGISTRY_DIR}/chains/metadata.yaml"
echo "Registry rendered at ${REGISTRY_DIR}/chains/metadata.yaml"

# Token config — build one side at a time from per-route config + core addresses.
# name/symbol/decimals are per-side: a route may label or scale the same asset
# differently on each chain (e.g. USDC on the origin, gUSDC on the synthetic side).
build_side() {  # $1=chain $2=type $3=name $4=symbol $5=decimals $6=token(optional)
  chain=$1; type=$2; name=$3; symbol=$4; decimals=$5; token=${6:-}
  progs=$(jq -c --arg c "$chain" '.[$c]' "${PROGRAM_IDS_FILE}")
  ism=$(printf '%s' "$progs" | jq -r '.multisig_ism_message_id')
  igp=$(printf '%s' "$progs" | jq -r '.overhead_igp_account')
  jq -n \
    --arg type "$type" --arg name "$name" --arg symbol "$symbol" \
    --argjson decimals "$decimals" --arg token "$token" \
    --arg uri "${WARP_TOKEN_METADATA_URI:-}" --arg ism "$ism" --arg igp "$igp" \
    '{type:$type, name:$name, symbol:$symbol, decimals:$decimals,
      interchainSecurityModule:$ism, interchainGasPaymaster:$igp}
     + (if $type=="collateral" then {token:$token} else {} end)
     + (if $type=="synthetic" and $uri!="" then {uri:$uri} else {} end)'
}

deploy_route() {  # $1 = /config/warp-routes/<name>.json
  cfg="$1"
  WARP_ROUTE_NAME=$(jq -r '.name' "$cfg")
  WARP_ORIGIN_CHAIN=$(jq -r '.origin.chain' "$cfg")
  WARP_ORIGIN_TYPE=$(jq -r '.origin.type' "$cfg")
  WARP_ORIGIN_TOKEN=$(jq -r '.origin.token // ""' "$cfg")
  WARP_ORIGIN_NAME=$(jq -r '.origin.name' "$cfg")
  WARP_ORIGIN_SYMBOL=$(jq -r '.origin.symbol' "$cfg")
  WARP_ORIGIN_DECIMALS=$(jq -r '.origin.decimals' "$cfg")
  WARP_REMOTE_CHAIN=$(jq -r '.remote.chain' "$cfg")
  WARP_REMOTE_TYPE=$(jq -r '.remote.type' "$cfg")
  WARP_REMOTE_NAME=$(jq -r '.remote.name' "$cfg")
  WARP_REMOTE_SYMBOL=$(jq -r '.remote.symbol' "$cfg")
  WARP_REMOTE_DECIMALS=$(jq -r '.remote.decimals' "$cfg")
  WARP_TOKEN_METADATA_URI=$(jq -r '.metadataUri // ""' "$cfg")

  ROUTE_STATE_DIR="${STATE_DIR}/warp-routes/${WARP_ROUTE_NAME}"
  mkdir -p "${ROUTE_STATE_DIR}"

  # -------------------------------------------------------
  # Idempotency check: skip if token-config already populated
  # -------------------------------------------------------
  if [ "${FORCE_REDEPLOY:-false}" != "true" ]; then
    EXISTING_FILE="${ROUTE_STATE_DIR}/token-config.json"
    if [ -s "${EXISTING_FILE}" ]; then
      CONTENT=$(cat "${EXISTING_FILE}")
      if [ "$CONTENT" != "{}" ] && [ "$CONTENT" != "null" ]; then
        echo ""
        echo "Warp route config already exists (${EXISTING_FILE} has data)."
        echo "Set FORCE_REDEPLOY=true to override. Exiting."
        exit 0
      fi
    fi
  fi

  ROUTE_LOG="${ROUTE_STATE_DIR}/deploy.log"
  exec > >(sed "s#${SOLANA_RPC_URL:-__no_solana_rpc__}#<REDACTED>#g" | tee "${ROUTE_LOG}") 2>&1

  echo "Warp route name: ${WARP_ROUTE_NAME}"

  echo "Origin chain: ${WARP_ORIGIN_CHAIN} (${WARP_ORIGIN_TYPE})"
  echo "Remote chain: ${WARP_REMOTE_CHAIN} (${WARP_REMOTE_TYPE})"

  for side_chain in "${WARP_ORIGIN_CHAIN}" "${WARP_REMOTE_CHAIN}"; do
    progs=$(jq -c --arg c "$side_chain" '.[$c] // {}' "${PROGRAM_IDS_FILE}")
    if [ "$progs" = "{}" ]; then
      echo "ERROR: program-ids.json missing data for ${side_chain} (route ${WARP_ROUTE_NAME})."
      exit 1
    fi
  done

  echo "Core program IDs found for both chains."

  # Create Solana CLI config (required by hyperlane-sealevel-client even when --keypair is set)
  mkdir -p /root/.config/solana/cli
  cat > /root/.config/solana/cli/config.yml <<SOLCFG
json_rpc_url: "$(chain_var "${WARP_ORIGIN_CHAIN}" RPC_URL)"
websocket_url: ""
keypair_path: "${DEPLOYER_KEY_FILE}"
commitment: finalized
SOLCFG

  # Write core program IDs where the CLI expects them:
  #   {environments_dir}/{environment}/{chain}/core/program-ids.json
  for side_chain in "${WARP_ORIGIN_CHAIN}" "${WARP_REMOTE_CHAIN}"; do
    mkdir -p "${ENVIRONMENTS_DIR}/${ENVIRONMENT}/${side_chain}/core"
    jq -c --arg c "$side_chain" '.[$c]' "${PROGRAM_IDS_FILE}" \
      > "${ENVIRONMENTS_DIR}/${ENVIRONMENT}/${side_chain}/core/program-ids.json"
  done

  if [ "${WARP_ORIGIN_TYPE}" = "collateral" ]; then
    case "${WARP_ORIGIN_TOKEN:-}" in
      "" | REPLACE_WITH_*)
        echo "ERROR: route ${WARP_ROUTE_NAME} is collateral but origin.token is unset or still a placeholder ('${WARP_ORIGIN_TOKEN:-}'). Set it to the collateral mint in bridges/<bridge>/warp-routes/<route>.yml."
        exit 1
        ;;
    esac
  fi

  jq -n \
    --arg oc "${WARP_ORIGIN_CHAIN}" --argjson o "$(build_side "${WARP_ORIGIN_CHAIN}" "${WARP_ORIGIN_TYPE}" "${WARP_ORIGIN_NAME}" "${WARP_ORIGIN_SYMBOL}" "${WARP_ORIGIN_DECIMALS}" "${WARP_ORIGIN_TOKEN:-}")" \
    --arg rc "${WARP_REMOTE_CHAIN}" --argjson r "$(build_side "${WARP_REMOTE_CHAIN}" "${WARP_REMOTE_TYPE}" "${WARP_REMOTE_NAME}" "${WARP_REMOTE_SYMBOL}" "${WARP_REMOTE_DECIMALS}" "")" \
    '{($oc):$o, ($rc):$r}' > "${WORK_DIR}/token-config.json"
  echo "Token config:"; cat "${WORK_DIR}/token-config.json"

  # -------------------------------------------------------
  # Deploy warp routes (single invocation for all chains)
  # -------------------------------------------------------
  echo ""
  echo "=== Deploying warp routes ==="

  hyperlane-sealevel-client \
    --keypair "${DEPLOYER_KEY_FILE}" \
    warp-route deploy \
    --environment "${ENVIRONMENT}" \
    --environments-dir "${ENVIRONMENTS_DIR}" \
    --built-so-dir /opt/hyperlane/programs \
    --warp-route-name "${WARP_ROUTE_NAME}" \
    --token-config-file "${WORK_DIR}/token-config.json" \
    --registry "${REGISTRY_DIR}" \
    --ata-payer-funding-amount 1000000000

  echo "Warp routes deployed"

  # -------------------------------------------------------
  # Collect deployment output
  # The CLI writes output to: {environments-dir}/{environment}/warp-routes/{name}/
  # -------------------------------------------------------
  echo ""
  echo "=== Checking deployment outputs ==="
  WARP_OUTPUT_DIR="${ENVIRONMENTS_DIR}/${ENVIRONMENT}/warp-routes/${WARP_ROUTE_NAME}"
  if [ -d "${WARP_OUTPUT_DIR}" ]; then
    echo "Warp route deployment outputs:"
    ls -la "${WARP_OUTPUT_DIR}/"
  else
    echo "WARNING: Expected output directory ${WARP_OUTPUT_DIR} not found."
    echo "Checking environment dir for output files..."
    find "${ENVIRONMENTS_DIR}" -name "*.json" -type f 2>/dev/null || true
  fi

  # -------------------------------------------------------
  # Post-deploy program hash verification
  # -------------------------------------------------------
  echo ""
  echo "=== Verifying deployed warp route program hashes ==="
  VERIFY_FAILED=0

  for SO_NAME in hyperlane_sealevel_token hyperlane_sealevel_token_native hyperlane_sealevel_token_collateral; do
    SO_FILE="/opt/hyperlane/programs/${SO_NAME}.so"
    [ -f "$SO_FILE" ] || continue

    # Note: We'd need the deployed program addresses from the CLI output
    # to verify hashes. This section is best-effort.
    LOCAL_HASH=$(solana-verify get-executable-hash "$SO_FILE" 2>/dev/null || echo "")
    if [ -n "$LOCAL_HASH" ]; then
      echo "Local hash for ${SO_NAME}: ${LOCAL_HASH}"
    fi
  done

  if [ "$VERIFY_FAILED" -ne 0 ]; then
    echo "FATAL: Program hash verification failed. Aborting."
    exit 1
  fi

  # -------------------------------------------------------
  # Transfer ownership to hardware wallet (if configured)
  # -------------------------------------------------------
  if [ -n "${HARDWARE_WALLET_PUBKEY:-}" ]; then
    echo ""
    echo "=== Transferring warp route upgrade authority to hardware wallet ==="
    echo "Hardware wallet pubkey: ${HARDWARE_WALLET_PUBKEY}"

    # The warp-route deploy writes program-ids.json with per-chain entries:
    #   {"solana": {"hex": "0x...", "base58": "..."}, "gorchain": {"hex": "0x...", "base58": "..."}}
    WARP_PROGRAMS_FILE="${WARP_OUTPUT_DIR}/program-ids.json"
    if [ ! -f "${WARP_PROGRAMS_FILE}" ]; then
      echo "WARNING: ${WARP_PROGRAMS_FILE} not found, cannot transfer upgrade authority."
      echo "Use 'solana program set-upgrade-authority' manually with the deployed program IDs."
    else
      # Map chain names to RPC URLs for the solana CLI
      for CHAIN_NAME in $(jq -r 'keys[]' "${WARP_PROGRAMS_FILE}"); do
        PROGRAM_ID=$(jq -r ".\"${CHAIN_NAME}\".base58 // empty" "${WARP_PROGRAMS_FILE}")
        if [ -z "$PROGRAM_ID" ]; then
          echo "WARNING: No base58 address for ${CHAIN_NAME} in warp program-ids.json, skipping."
          continue
        fi

        # Determine RPC URL from chain name
        CHAIN_UPPER=$(echo "$CHAIN_NAME" | tr '[:lower:]' '[:upper:]')
        RPC_VAR="${CHAIN_UPPER}_RPC_URL"
        RPC_URL=$(eval echo "\${${RPC_VAR}:-}")
        if [ -z "$RPC_URL" ]; then
          echo "WARNING: No RPC URL for ${CHAIN_NAME} (${RPC_VAR} not set), skipping."
          continue
        fi

        echo "Transferring warp route upgrade authority on ${CHAIN_NAME}: ${PROGRAM_ID}..."
        solana program set-upgrade-authority "$PROGRAM_ID" \
          --new-upgrade-authority "${HARDWARE_WALLET_PUBKEY}" \
          --skip-new-upgrade-authority-signer-check \
          --keypair "${DEPLOYER_KEY_FILE}" \
          --url "$RPC_URL"

        # Transfer the Hyperlane app-level route owner (gates enroll/set-ISM/
        # set-destination-gas). Runs after warp-route deploy did its owner-gated
        # setup; only read-only queries follow. Fail closed.
        echo "Transferring warp route app-level ownership on ${CHAIN_NAME}: ${PROGRAM_ID}..."
        hyperlane-sealevel-client \
          --url "$RPC_URL" \
          --keypair "${DEPLOYER_KEY_FILE}" \
          token transfer-ownership \
          --program-id "$PROGRAM_ID" \
          "${HARDWARE_WALLET_PUBKEY}"
      done
    fi
  fi

  # -------------------------------------------------------
  # Resolve the synthetic token mint (only when the remote side is synthetic).
  # It is a PDA of the synthetic warp program; the Hyperlane SDK requires it
  # explicitly in warp config and does not auto-derive it, so query the deployed
  # program and emit it for warp-UI to consume.
  # -------------------------------------------------------
  SYNTHETIC_MINT=""
  if [ "${WARP_REMOTE_TYPE}" = "synthetic" ]; then
    echo ""
    echo "=== Resolving synthetic token mint ==="
    WARP_PROGRAMS_FILE="${WARP_OUTPUT_DIR}/program-ids.json"
    SYNTHETIC_WARP_PROGRAM=""
    if [ -f "${WARP_PROGRAMS_FILE}" ]; then
      SYNTHETIC_WARP_PROGRAM=$(jq -r --arg c "${WARP_REMOTE_CHAIN}" '.[$c].base58 // empty' "${WARP_PROGRAMS_FILE}")
    fi
    if [ -n "${SYNTHETIC_WARP_PROGRAM}" ]; then
      MINT_QUERY_OUT=$(hyperlane-sealevel-client \
        --keypair "${DEPLOYER_KEY_FILE}" \
        --url "$(chain_var "${WARP_REMOTE_CHAIN}" RPC_URL)" \
        token query --program-id "${SYNTHETIC_WARP_PROGRAM}" synthetic 2>&1 || true)
      SYNTHETIC_MINT=$(echo "${MINT_QUERY_OUT}" \
        | grep -oE 'Mint / Mint Authority:[[:space:]]*[1-9A-HJ-NP-Za-km-z]{32,44}' \
        | grep -oE '[1-9A-HJ-NP-Za-km-z]{32,44}' | head -1)
      if [ -z "${SYNTHETIC_MINT}" ]; then
        SYNTHETIC_MINT=$(echo "${MINT_QUERY_OUT}" \
          | grep -oE 'mint:[[:space:]]+[1-9A-HJ-NP-Za-km-z]{32,44}' \
          | grep -oE '[1-9A-HJ-NP-Za-km-z]{32,44}' | head -1)
      fi
    fi
    if [ -z "${SYNTHETIC_MINT}" ]; then
      echo "ERROR: could not resolve synthetic mint for ${WARP_REMOTE_CHAIN} (program ${SYNTHETIC_WARP_PROGRAM:-<none>})"
      echo "Query output:"
      echo "${MINT_QUERY_OUT:-<no output>}"
      exit 1
    fi
    export SYNTHETIC_MINT
    echo "Synthetic token mint (${WARP_REMOTE_CHAIN}): ${SYNTHETIC_MINT}"
  fi

  # -------------------------------------------------------
  # Build token-config.json output
  # -------------------------------------------------------
  echo ""
  echo "=== Building token-config.json ==="

  # Wrap the rendered (chain-keyed) token-config with the route name, and record
  # the synthetic mint under its chain's entry when one was resolved.
  jq --arg name "${WARP_ROUTE_NAME}" --arg rc "${WARP_REMOTE_CHAIN}" --arg mint "${SYNTHETIC_MINT:-}" \
    '{warpRoute: ({name:$name} + .)}
     | if $mint != "" then .warpRoute[$rc] += {mint:$mint} else . end' \
    "${WORK_DIR}/token-config.json" > "${WORK_DIR}/output/token-config.json"

  # -------------------------------------------------------
  # Write deployment artifacts to k8s ConfigMaps
  # -------------------------------------------------------
  echo ""
  echo "=== Writing warp route artifacts to ${ROUTE_STATE_DIR} ==="

  cp "${WORK_DIR}/output/token-config.json" "${ROUTE_STATE_DIR}/token-config.json"

  if [ -d "${WARP_OUTPUT_DIR}" ]; then
    rm -rf "${ROUTE_STATE_DIR}/warp-deploy-outputs"
    mkdir -p "${ROUTE_STATE_DIR}/warp-deploy-outputs"
    cp -a "${WARP_OUTPUT_DIR}/." "${ROUTE_STATE_DIR}/warp-deploy-outputs/" 2>/dev/null || true
    # Drop the program-id and buffer keypairs the sealevel-client emits.
    # Buffer keypairs are dead post-deploy; program-id keypairs lock in the
    # deployment identity but no consumer reads them and the upgrade-authority
    # key (Ledger in prod) already covers any real recovery scenario.
    rm -rf "${ROUTE_STATE_DIR}/warp-deploy-outputs/keys"
  fi

  # -------------------------------------------------------
  # Preflight: verify expected outputs
  # -------------------------------------------------------
  if [ ! -s "${ROUTE_STATE_DIR}/token-config.json" ]; then
    echo "ERROR: warp-deployer preflight failed: ${ROUTE_STATE_DIR}/token-config.json missing or empty"
    exit 1
  fi

  echo ""
  echo "=== Warp route deployment complete ==="
  echo "Origin chain: ${WARP_ORIGIN_CHAIN}"
  echo "Remote chain: ${WARP_REMOTE_CHAIN}"
  echo ""
  echo "Artifacts written to ${ROUTE_STATE_DIR}:"
  echo "  - token-config.json"
  [ -d "${ROUTE_STATE_DIR}/warp-deploy-outputs" ] && echo "  - warp-deploy-outputs/"
}

: "${WARP_ROUTES:?WARP_ROUTES must be set to a comma-separated list of route names}"
echo "=== Deploying warp routes: ${WARP_ROUTES} ==="
for route in $(echo "${WARP_ROUTES}" | tr ',' ' '); do
  cfg="/config/warp-routes/${route}.json"
  if [ ! -s "$cfg" ]; then
    echo "ERROR: route config $cfg not found for selected route '${route}'"
    exit 1
  fi
  ( deploy_route "$cfg" )   # subshell: per-route exec redirect + vars stay scoped
done
echo "=== All selected warp routes processed ==="

# Build the warp-UI route config from the per-route artifacts written above (single
# source of the WarpCoreConfig transform; publish/conftest only distribute the result).
echo ""
echo "=== Building warp-UI route config ==="
STATE_DIR="${STATE_DIR}" bash /opt/scripts/build-warp-ui-config.sh

# -------------------------------------------------------
# Clean up deployer keypair
# -------------------------------------------------------
rm -f "${DEPLOYER_KEY_FILE}"
