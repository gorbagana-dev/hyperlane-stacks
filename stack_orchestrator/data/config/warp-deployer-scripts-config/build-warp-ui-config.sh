#!/bin/bash
# Build the warp-UI route config (warp-routes/warpRoutes.yaml) from the per-route artifacts
# the warp-deployer already wrote under ${STATE_DIR}/warp-routes/<name>/. Emits a Hyperlane
# WarpCoreConfig ({tokens, options}) covering exactly the routes named in WARP_ROUTES —
# one token entry per chain side. The warp-UI loads this file at runtime.
#
# Written at the root of the existing warp-routes/ tree so state_distribute can scope the
# warp-ui ConfigMap to just this file. Single source of the type->standard / connections
# transform; ops (publish-bridge-state) and e2e (conftest) only distribute the result.
set -euo pipefail

STATE_DIR="${STATE_DIR:-${STATE_OUTPUT_DIR:-/state}}"
WARP_ROUTES_DIR="${WARP_ROUTES_DIR:-/config/warp-routes}"  # must match the warp-routes mount in the warp-deployer compose
PROGRAM_IDS_FILE="${PROGRAM_IDS_FILE:-${STATE_DIR}/program-ids.json}"
: "${WARP_ROUTES:?WARP_ROUTES must be set to a comma-separated list of route names}"

warp_ui_standard() {  # $1=type -> Sealevel token standard
  case "$1" in
    collateral) echo "SealevelHypCollateral" ;;
    synthetic)  echo "SealevelHypSynthetic" ;;
    native)     echo "SealevelHypNative" ;;
    *) echo "ERROR: warp-UI config: unknown token type '$1'" >&2; exit 1 ;;
  esac
}

tokens="[]"
for route in $(echo "${WARP_ROUTES}" | tr ',' ' '); do
  cfg="${WARP_ROUTES_DIR}/${route}.json"
  [ -s "$cfg" ] || { echo "ERROR: warp-UI config: menu $cfg not found for route '${route}'" >&2; exit 1; }
  name=$(jq -r '.name' "$cfg")
  route_dir="${STATE_DIR}/warp-routes/${name}"
  tcfg="${route_dir}/token-config.json"
  wpids="${route_dir}/warp-deploy-outputs/program-ids.json"
  for f in "$tcfg" "$wpids"; do
    [ -s "$f" ] || { echo "ERROR: warp-UI config: missing ${f} for route '${name}'" >&2; exit 1; }
  done

  # The two chain sides come from the menu's origin/remote (robust to chain names
  # with whitespace and to a malformed token-config; matches deploy.sh).
  side_a=$(jq -r '.origin.chain' "$cfg")
  side_b=$(jq -r '.remote.chain' "$cfg")

  for pair in "${side_a}|${side_b}" "${side_b}|${side_a}"; do
    self="${pair%%|*}"; other="${pair##*|}"
    side=$(jq -c --arg c "$self" '.warpRoute[$c]' "$tcfg")
    standard=$(warp_ui_standard "$(printf '%s' "$side" | jq -r '.type')")
    self_prog=$(jq -r --arg c "$self" '.[$c].base58 // ""' "$wpids")
    [ -n "$self_prog" ] || { echo "ERROR: warp-UI config: no warp program for ${self} in ${wpids}" >&2; exit 1; }
    other_prog=$(jq -r --arg c "$other" '.[$c].base58 // ""' "$wpids")
    [ -n "$other_prog" ] || { echo "ERROR: warp-UI config: no warp program for ${other} in ${wpids}" >&2; exit 1; }
    mailbox=$(jq -r --arg c "$self" '.[$c].mailbox // ""' "${PROGRAM_IDS_FILE}")
    [ -n "$mailbox" ] || { echo "ERROR: warp-UI config: no mailbox for ${self} in ${PROGRAM_IDS_FILE}" >&2; exit 1; }

    entry=$(printf '%s' "$side" | jq \
      --arg chainName "$self" --arg standard "$standard" \
      --arg addr "$self_prog" --arg mailbox "$mailbox" \
      --arg conn "sealevel|${other}|${other_prog}" \
      '{chainName:$chainName, standard:$standard, name:.name, symbol:.symbol,
        decimals:.decimals, addressOrDenom:$addr, mailbox:$mailbox,
        connections:[{token:$conn}]}
       + (if .type=="collateral" then {collateralAddressOrDenom:.token}
          elif .type=="synthetic" then {collateralAddressOrDenom:.mint}
          else {} end)')
    tokens=$(jq -c --argjson e "$entry" '. + [$e]' <<<"$tokens")
  done
done

# JSON is valid YAML and the warp-UI loader parses either (tryParseJsonOrYaml); emitting
# JSON keeps decimals numeric without needing a YAML emitter in the image.
mkdir -p "${STATE_DIR}/warp-routes"
jq -n --argjson tokens "$tokens" '{tokens:$tokens, options:{}}' > "${STATE_DIR}/warp-routes/warpRoutes.yaml"
echo "Wrote ${STATE_DIR}/warp-routes/warpRoutes.yaml ($(jq '.tokens | length' "${STATE_DIR}/warp-routes/warpRoutes.yaml") token entries)"
