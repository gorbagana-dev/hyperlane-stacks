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
