/**
 * Configuration loaded from environment variables.
 */

function requireEnv(name) {
  const val = process.env[name];
  if (!val) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return val;
}

function optionalEnv(name, fallback) {
  return process.env[name] || fallback;
}

export function loadConfig() {
  return {
    // Privy credentials
    privyAppId: requireEnv("PRIVY_APP_ID"),
    privyAppSecret: requireEnv("PRIVY_APP_SECRET"),
    privyOracleWalletId: requireEnv("PRIVY_ORACLE_WALLET_ID"),

    // Chain RPCs
    gorchainRpcUrl: requireEnv("GORCHAIN_RPC_URL"),
    solanaRpcUrl: requireEnv("SOLANA_RPC_URL"),

    // IGP program IDs (base58)
    gorchainIgpProgramId: requireEnv("GORCHAIN_IGP_PROGRAM_ID"),
    solanaIgpProgramId: requireEnv("SOLANA_IGP_PROGRAM_ID"),

    // Domain IDs
    gorchainDomainId: parseInt(requireEnv("GORCHAIN_DOMAIN_ID"), 10),
    solanaDomainId: parseInt(requireEnv("SOLANA_DOMAIN_ID"), 10),

    // Price feed
    priceFeedUrl: optionalEnv(
      "PRICE_FEED_URL",
      "https://api.coingecko.com/api/v3"
    ),
    gorchainTokenId: optionalEnv("GORCHAIN_TOKEN_ID", "solana"),
    solanaTokenId: optionalEnv("SOLANA_TOKEN_ID", "solana"),

    // Sanity check: max allowed deviation from previous value (fraction, e.g. 0.5 = 50%)
    maxPriceDeviation: parseFloat(optionalEnv("MAX_PRICE_DEVIATION", "0.5")),

    // Static fallback values (used if price feed unavailable)
    fallbackGorchainPriceUsd: parseFloat(
      optionalEnv("FALLBACK_GORCHAIN_PRICE_USD", "0")
    ),
    fallbackSolanaPriceUsd: parseFloat(
      optionalEnv("FALLBACK_SOLANA_PRICE_USD", "0")
    ),

    // Gas price estimates (lamports per compute unit)
    gorchainGasPrice: parseInt(optionalEnv("GORCHAIN_GAS_PRICE", "1"), 10),
    solanaGasPrice: parseInt(optionalEnv("SOLANA_GAS_PRICE", "1"), 10),

    // Destination gas overhead (extra gas units charged on destination)
    gorchainGasOverhead: parseInt(
      optionalEnv("GORCHAIN_GAS_OVERHEAD", "0"),
      10
    ),
    solanaGasOverhead: parseInt(optionalEnv("SOLANA_GAS_OVERHEAD", "0"), 10),
  };
}
