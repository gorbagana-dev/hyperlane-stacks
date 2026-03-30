/**
 * Configuration loaded from environment variables.
 */

export interface OracleConfig {
  // Privy credentials (required when signerMode=privy)
  privyAppId: string;
  privyAppSecret: string;
  privyOracleWalletId: string;
  privyApiUrl: string; // override for testing (mock server)

  // Signer mode
  signerMode: "privy" | "keypair";
  oracleKeypair: number[]; // JSON keypair array (for keypair mode)

  // Chain RPCs
  gorchainRpcUrl: string;
  solanaRpcUrl: string;

  // IGP program IDs (base58)
  gorchainIgpProgramId: string;
  solanaIgpProgramId: string;

  // Domain IDs
  gorchainDomainId: number;
  solanaDomainId: number;

  // Price feed (empty = skip oracle updates)
  priceFeedUrl: string;
  gorchainTokenId: string;
  solanaTokenId: string;

  // Sanity check: max allowed deviation (fraction, e.g. 0.5 = 50%)
  maxPriceDeviation: number;

  // Gas oracle parameters
  gasPrice: string; // decimal SOL (e.g. "0.000000001" → 1 lamport per gas unit)
  gasOverhead: number; // compute units (on-chain overhead IGP setting)
  exchangeRateMarginPct: number; // percentage
  minUsdCost: number; // minimum USD cost floor per message (0 = disabled)

  // sGOR → gGOR conversion factor (1 gGOR = N sGOR)
  gorchainNativeTokenMultiplier: number;

  // Loop mode
  runLoop: boolean;
  intervalMs: number;
}

function requireEnv(name: string): string {
  const val = process.env[name];
  if (!val) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return val;
}

function optionalEnv(name: string, fallback: string): string {
  return process.env[name] || fallback;
}

export function loadConfig(): OracleConfig {
  const signerMode = optionalEnv("SIGNER_MODE", "privy") as
    | "privy"
    | "keypair";

  let oracleKeypair: number[] = [];
  if (signerMode === "keypair") {
    const raw = requireEnv("ORACLE_KEYPAIR");
    oracleKeypair = JSON.parse(raw);
  }

  return {
    // Privy (required in privy mode, optional in keypair mode)
    privyAppId: signerMode === "privy" ? requireEnv("PRIVY_APP_ID") : "",
    privyAppSecret:
      signerMode === "privy" ? requireEnv("PRIVY_APP_SECRET") : "",
    privyOracleWalletId:
      signerMode === "privy" ? requireEnv("PRIVY_ORACLE_WALLET_ID") : "",
    privyApiUrl: optionalEnv("PRIVY_API_URL", "https://auth.privy.io/api/v1"),

    signerMode,
    oracleKeypair,

    // Chain RPCs
    gorchainRpcUrl: requireEnv("GORCHAIN_RPC_URL"),
    solanaRpcUrl: requireEnv("SOLANA_RPC_URL"),

    // IGP program IDs
    gorchainIgpProgramId: requireEnv("GORCHAIN_IGP_PROGRAM_ID"),
    solanaIgpProgramId: requireEnv("SOLANA_IGP_PROGRAM_ID"),

    // Domain IDs
    gorchainDomainId: parseInt(requireEnv("GORCHAIN_DOMAIN_ID"), 10),
    solanaDomainId: parseInt(requireEnv("SOLANA_DOMAIN_ID"), 10),

    // Price feed (empty string = skip oracle updates)
    priceFeedUrl: optionalEnv("PRICE_FEED_URL", ""),
    gorchainTokenId: optionalEnv("GORCHAIN_TOKEN_ID", "gorbagana"),
    solanaTokenId: optionalEnv("SOLANA_TOKEN_ID", "solana"),

    maxPriceDeviation: parseFloat(optionalEnv("MAX_PRICE_DEVIATION", "0.5")),

    // Gas oracle parameters
    gasPrice: optionalEnv("GAS_PRICE", "0.000000001"),
    gasOverhead: parseInt(optionalEnv("GAS_OVERHEAD", "200000"), 10),
    exchangeRateMarginPct: parseFloat(
      optionalEnv("EXCHANGE_RATE_MARGIN_PCT", "10"),
    ),
    minUsdCost: parseFloat(optionalEnv("MIN_USD_COST", "0.50")),
    gorchainNativeTokenMultiplier: parseFloat(
      optionalEnv("GORCHAIN_NATIVE_TOKEN_MULTIPLIER", "100"),
    ),

    // Loop mode
    runLoop: optionalEnv("RUN_LOOP", "false") === "true",
    intervalMs: parseInt(optionalEnv("GAS_ORACLE_INTERVAL_MS", "900000"), 10),
  };
}
