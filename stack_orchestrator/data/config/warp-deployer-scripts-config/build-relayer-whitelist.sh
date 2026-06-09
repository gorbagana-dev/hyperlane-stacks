#!/bin/bash
# Build the relayer message whitelist (relayer-whitelist.json) from the per-route
# warp program addresses the warp-deployer wrote under ${STATE_DIR}/warp-routes/<name>/.
# Emits a Hyperlane MatchingList — one {recipientaddress: 0x<hex>} rule per warp
# program (both chain sides) across exactly the routes named in WARP_ROUTES. The
# relayer loads this as HYP_WHITELIST and relays only messages delivered to a known
# warp program, defeating rogue-route relaying.
#
# Recipient-only is sufficient: on-chain enrollment already rejects spoofed senders.
# Empty result -> a deny-all sentinel (recipient = 32 zero bytes), NOT [] — an empty
# MatchingList deserializes to "no filter" (relay everything) in the agent.
set -euo pipefail

STATE_DIR="${STATE_DIR:-${STATE_OUTPUT_DIR:-/state}}"
WARP_ROUTES_DIR="${WARP_ROUTES_DIR:-/config/warp-routes}"  # must match the warp-routes mount in the warp-deployer compose
: "${WARP_ROUTES:?WARP_ROUTES must be set to a comma-separated list of route names}"

DENY_ALL='[{"recipientaddress":"0x0000000000000000000000000000000000000000000000000000000000000000"}]'

rules="[]"
for route in $(echo "${WARP_ROUTES}" | tr ',' ' '); do
  cfg="${WARP_ROUTES_DIR}/${route}.json"
  [ -s "$cfg" ] || { echo "ERROR: relayer whitelist: menu $cfg not found for route '${route}'" >&2; exit 1; }
  name=$(jq -r '.name' "$cfg")
  wpids="${STATE_DIR}/warp-routes/${name}/warp-deploy-outputs/program-ids.json"
  [ -s "$wpids" ] || { echo "ERROR: relayer whitelist: missing ${wpids} for route '${name}'" >&2; exit 1; }

  for chain in $(jq -r 'keys[]' "$wpids"); do
    hex=$(jq -r --arg c "$chain" '.[$c].hex // ""' "$wpids")
    [ -n "$hex" ] || { echo "ERROR: relayer whitelist: no hex address for ${chain} in ${wpids}" >&2; exit 1; }
    case "$hex" in 0x*) ;; *) hex="0x${hex}" ;; esac
    rules=$(jq -c --arg r "$hex" '. + [{recipientaddress:$r}]' <<<"$rules")
  done
done

whitelist=$(jq -c 'unique' <<<"$rules")
if [ "$whitelist" = "[]" ]; then
  whitelist="$DENY_ALL"
fi

echo "$whitelist" > "${STATE_DIR}/relayer-whitelist.json"
echo "Wrote ${STATE_DIR}/relayer-whitelist.json ($(jq 'length' <<<"$whitelist") rule(s))"
