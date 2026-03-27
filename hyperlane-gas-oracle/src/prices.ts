/**
 * Fetch token prices from CoinGecko (or compatible API).
 */

import type { OracleConfig } from "./config.js";

export interface TokenPrices {
  gorchainPriceUsd: number;
  solanaPriceUsd: number;
}

export async function fetchTokenPrices(
  config: OracleConfig,
): Promise<TokenPrices> {
  const { priceFeedUrl, gorchainTokenId, solanaTokenId } = config;
  const ids = [...new Set([gorchainTokenId, solanaTokenId])].join(",");
  const url = `${priceFeedUrl}/simple/price?ids=${ids}&vs_currencies=usd`;

  console.log(`Fetching prices from ${url}`);

  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(
      `Price feed returned ${resp.status}: ${await resp.text()}`,
    );
  }

  const data = (await resp.json()) as Record<
    string,
    { usd?: number } | undefined
  >;
  const gorchainPrice = data[gorchainTokenId]?.usd;
  const solanaPrice = data[solanaTokenId]?.usd;

  if (!gorchainPrice || !solanaPrice) {
    throw new Error(
      `Missing prices from feed: gorchain=${gorchainPrice}, solana=${solanaPrice}`,
    );
  }

  return { gorchainPriceUsd: gorchainPrice, solanaPriceUsd: solanaPrice };
}

/**
 * Sanity-check a new exchange rate against a previous value.
 * Returns true if within allowed deviation.
 */
export function isRateReasonable(
  newRate: string,
  previousRate: string | undefined,
  maxDeviation: number,
): boolean {
  if (!previousRate || previousRate === "0") return true;
  const newNum = Number(newRate);
  const prevNum = Number(previousRate);
  if (prevNum === 0) return true;

  const deviation = Math.abs(newNum - prevNum) / prevNum;
  if (deviation > maxDeviation) {
    console.error(
      `Exchange rate deviation ${(deviation * 100).toFixed(1)}% exceeds max ${(maxDeviation * 100).toFixed(1)}%: ` +
        `previous=${previousRate}, new=${newRate}`,
    );
    return false;
  }
  return true;
}
