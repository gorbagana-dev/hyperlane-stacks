/**
 * Shared oracle config computation used by both the gas oracle service
 * and the compute-config CLI script.
 *
 * Wraps the SDK's getLocalStorageGasOracleConfig() with a gasPriceModifier
 * that enforces a minimum USD cost floor per message — matching Hyperlane's
 * production behavior (hyperlane-monorepo typescript/infra/src/config/gas-oracle.ts).
 */

import { ProtocolType } from "@hyperlane-xyz/utils";
import { getLocalStorageGasOracleConfig } from "@hyperlane-xyz/sdk";

/**
 * Default handle gas for the min-cost modifier's typical gas estimate.
 * Uses the EVM-default collateral gasAmount (68,000) since that's what the
 * Rust client hardcodes in warp_route.rs gas_overhead_default().
 */
const DEFAULT_HANDLE_GAS = 68_000;

const SEALEVEL_SCALE = 10n ** 19n;

export interface OracleComputeParams {
  /** gGOR price in USD */
  gorchainPriceUsd: number;
  /** SOL price in USD */
  solanaPriceUsd: number;
  /** Gas price decimal amount (e.g. "0.000000001") */
  gasPrice: string;
  /** Gas price decimals (default: 9 for SOL-family) */
  gasPriceDecimals?: number;
  /** Exchange rate margin percentage (e.g. 10 = 10%) */
  exchangeRateMarginPct: number;
  /** On-chain overhead IGP overhead in compute units */
  overhead: number;
  /** Minimum USD cost floor per message (0 = disabled) */
  minUsdCost: number;
}

export interface DirectionalOracleConfig {
  tokenExchangeRate: string;
  gasPrice: string;
  tokenDecimals: number;
}

export interface ComputedOracleConfigs {
  gorchainToSolana: DirectionalOracleConfig;
  solanaToGorchain: DirectionalOracleConfig;
}

/**
 * Compute gas oracle configs for both directions using the Hyperlane SDK.
 *
 * Handles:
 * - 1e19 Sealevel exchange rate scaling
 * - Configurable margin
 * - adjustForPrecisionLoss (SDK built-in)
 * - Minimum USD cost floor (matches Hyperlane production behavior)
 */
export function computeOracleConfigs(
  params: OracleComputeParams,
): ComputedOracleConfigs {
  const {
    gorchainPriceUsd,
    solanaPriceUsd,
    gasPrice,
    gasPriceDecimals = 9,
    exchangeRateMarginPct,
    overhead,
    minUsdCost,
  } = params;

  const tokenPrices: Record<string, string> = {
    gorchain: gorchainPriceUsd.toString(),
    solana: solanaPriceUsd.toString(),
  };

  const gasOracleParams = {
    gorchain: {
      gasPrice: { amount: gasPrice, decimals: gasPriceDecimals },
      nativeToken: { price: gorchainPriceUsd.toString(), decimals: 9 },
    },
    solana: {
      gasPrice: { amount: gasPrice, decimals: gasPriceDecimals },
      nativeToken: { price: solanaPriceUsd.toString(), decimals: 9 },
    },
  };

  const gasPriceModifier =
    minUsdCost > 0
      ? createMinCostModifier(tokenPrices, overhead, minUsdCost)
      : undefined;

  const gorchainConfigs = getLocalStorageGasOracleConfig({
    local: "gorchain",
    localProtocolType: ProtocolType.Sealevel,
    gasOracleParams,
    exchangeRateMarginPct,
    gasPriceModifier,
  });

  const solanaConfigs = getLocalStorageGasOracleConfig({
    local: "solana",
    localProtocolType: ProtocolType.Sealevel,
    gasOracleParams,
    exchangeRateMarginPct,
    gasPriceModifier,
  });

  return {
    gorchainToSolana: normalize(gorchainConfigs["solana"]),
    solanaToGorchain: normalize(solanaConfigs["gorchain"]),
  };
}

function normalize(config: {
  tokenExchangeRate: string;
  gasPrice: string;
  tokenDecimals?: number;
}): DirectionalOracleConfig {
  return {
    tokenExchangeRate: config.tokenExchangeRate,
    gasPrice: config.gasPrice,
    tokenDecimals: config.tokenDecimals ?? 9,
  };
}

/**
 * Create a gasPriceModifier that enforces a minimum USD cost per message.
 *
 * When the natural IGP quote for a typical message is below minUsdCost,
 * the modifier back-computes a gasPrice that produces exactly that floor.
 *
 * The typical on-chain gas amount = overhead + DEFAULT_HANDLE_GAS (68k,
 * EVM-default collateral gasAmount). The floor is approximate since actual
 * gasAmount varies by warp route token type (44k native, 64k synthetic, 68k
 * collateral).
 */
function createMinCostModifier(
  tokenPrices: Record<string, string>,
  overhead: number,
  minUsdCost: number,
) {
  const typicalGas = BigInt(overhead + DEFAULT_HANDLE_GAS);

  return (
    local: string,
    _remote: string,
    config: {
      tokenExchangeRate: string;
      gasPrice: string;
      tokenDecimals?: number;
    },
  ): string => {
    const localPriceUsd = parseFloat(tokenPrices[local]);
    const localDecimals = 9;
    const remoteDecimals = config.tokenDecimals ?? 9;

    const exchangeRate = BigInt(config.tokenExchangeRate);
    const gasPrice = BigInt(config.gasPrice);

    // Compute what the current config would quote (in local lamports)
    let quoteWei = (gasPrice * exchangeRate * typicalGas) / SEALEVEL_SCALE;

    // Decimal conversion (remote → local) if chains have different decimals
    if (remoteDecimals !== localDecimals) {
      const shift = BigInt(Math.abs(localDecimals - remoteDecimals));
      quoteWei =
        localDecimals > remoteDecimals
          ? quoteWei * 10n ** shift
          : quoteWei / 10n ** shift;
    }

    const quoteUsd = (Number(quoteWei) / 10 ** localDecimals) * localPriceUsd;

    if (quoteUsd >= minUsdCost) {
      return config.gasPrice; // already above floor
    }

    // Back-compute gasPrice to hit the floor
    const minQuoteWei = BigInt(
      Math.ceil((minUsdCost / localPriceUsd) * 10 ** localDecimals),
    );
    let newGasPrice =
      (minQuoteWei * SEALEVEL_SCALE) / (exchangeRate * typicalGas);

    // Decimal conversion (local → remote)
    if (remoteDecimals !== localDecimals) {
      const shift = BigInt(Math.abs(remoteDecimals - localDecimals));
      newGasPrice =
        remoteDecimals > localDecimals
          ? newGasPrice * 10n ** shift
          : newGasPrice / 10n ** shift;
    }

    return (newGasPrice > 0n ? newGasPrice : 1n).toString();
  };
}
