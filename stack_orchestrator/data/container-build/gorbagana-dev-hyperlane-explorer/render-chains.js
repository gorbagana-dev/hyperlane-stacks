// Renders $PUBLIC_DIR/gorbagana-chains.json so the frontend treats gorchain +
// solana as fully-configured (non-PI) chains. Without metadata + a mailbox the
// explorer hides them from the feed, shows the destination as "unknown", and
// reports "PI chains require a config" instead of resolving delivery.
//
// Each chain's core addresses (mailbox, IGP, ISM, validatorAnnounce,
// merkleTreeHook) + domain/chain ids live only in the deployer's
// agent-config.json (AGENT_CONFIG), so the whole chain object is taken from
// there. Only the browser-facing fields are overridden from env: rpcUrls
// (agent-config carries placeholder rpcUrls — generated state is secret-free —
// so the real gorchain RPC must come from GORCHAIN_RPC_URL), plus nativeToken/
// blocks (completed for the schema) and displayName/isTestnet. The JSON object
// keys must match the scraper's domain.name values.
//
// Also renders $PUBLIC_DIR/gorbagana-warp-routes.json (see the warp-routes block at
// the end) so the frontend can resolve warp transfers on our chains — the public
// registry doesn't carry them.
//
// Invoked by entrypoint.sh with AGENT_CONFIG + PUBLIC_DIR set.
const fs = require("fs");
const e = process.env;

const cfg = JSON.parse(fs.readFileSync(e.AGENT_CONFIG, "utf8"));
const chains = cfg.chains || cfg;

// Keep every field agent-config provides (mailbox + all core addresses + domain
// ids + protocol), overriding only the browser-facing presentation fields.
function render(name, defaultDisplay, rpcUrl, prefix, defaultLogo) {
  const c = chains[name];
  if (!c) {
    console.error("agent-config.json has no chain:", name);
    process.exit(1);
  }
  if (!c.mailbox) {
    console.error("agent-config.json missing mailbox for chain:", name);
    process.exit(1);
  }
  return {
    ...c,
    name,
    displayName: e[prefix + "_DISPLAY_NAME"] || defaultDisplay,
    logoURI: e[prefix + "_LOGO_URI"] || defaultLogo,
    isTestnet: (e[prefix + "_IS_TESTNET"] || "true") === "true",
    rpcUrls: [{ http: rpcUrl }],
    nativeToken: {
      name: e[prefix + "_NATIVE_TOKEN_NAME"] || e[prefix + "_NATIVE_TOKEN_SYMBOL"] || defaultDisplay,
      symbol: e[prefix + "_NATIVE_TOKEN_SYMBOL"] || name.slice(0, 3).toUpperCase(),
      decimals: Number(e[prefix + "_NATIVE_TOKEN_DECIMALS"] || 9),
    },
    blocks: { confirmations: 1, estimateBlockTime: 1, reorgPeriod: 0 },
  };
}

const gorchain = e.GORCHAIN_CHAIN_NAME || "gorchain";
const solana = e.SOLANA_CHAIN_NAME || "solana";

const out = {};
// gorchain RPC is a non-secret public endpoint, safe to serve to the browser.
out[gorchain] = render(gorchain, "Gorbagana", e.GORCHAIN_RPC_URL, "GORCHAIN", "/gorbagana-logo.jpg");
// The browser never calls Solana RPC (data flows through /api/graphql; delivery
// status comes from the scraper DB), so ship a placeholder rather than wiring a
// real (possibly key-bearing) Solana endpoint into browser-served metadata.
out[solana] = render(solana, "Solana", "http://rpc-placeholder.invalid", "SOLANA", "/solana-logo.png");

fs.writeFileSync(e.PUBLIC_DIR + "/gorbagana-chains.json", JSON.stringify(out, null, 2));
console.log("Rendered gorbagana-chains.json for:", Object.keys(out).join(", "));

// Warp routes: the deployer writes a WarpCoreConfig ({tokens, options}) to
// warpRoutes.yaml, distributed into the agent-config ConfigMap (mounted at /config).
// The frontend expects a WarpRouteConfigs record ({ <id>: WarpCoreConfig }), so wrap
// it under a single id and write it to /public. Absent only when the image runs without
// our deployment (upstream/dev) — then we skip and the explorer relies on the registry.
// A malformed file is logged and skipped rather than crashing the container.
const warpSrc = e.WARP_ROUTES_SRC || "/config/warpRoutes.yaml";
if (fs.existsSync(warpSrc)) {
  try {
    // The deployer emits JSON (which is valid YAML), so JSON.parse handles it.
    const warpCoreConfig = JSON.parse(fs.readFileSync(warpSrc, "utf8"));
    const warpRouteId = e.WARP_ROUTE_ID || "gorbagana";
    fs.writeFileSync(
      e.PUBLIC_DIR + "/gorbagana-warp-routes.json",
      JSON.stringify({ [warpRouteId]: warpCoreConfig }, null, 2),
    );
    console.log("Rendered gorbagana-warp-routes.json:", (warpCoreConfig.tokens || []).length, "tokens");
  } catch (err) {
    console.error("Skipping warp-route injection — could not parse", warpSrc + ":", err.message);
  }
} else {
  console.log("No warpRoutes.yaml at", warpSrc, "— relying on the registry for warp routes.");
}
