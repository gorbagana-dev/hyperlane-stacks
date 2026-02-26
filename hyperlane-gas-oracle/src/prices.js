/**
 * Fetch token prices from CoinGecko (or compatible API).
 */

export async function fetchTokenPrices(config) {
  const { priceFeedUrl, gorchainTokenId, solanaTokenId } = config;
  const ids = [gorchainTokenId, solanaTokenId]
    .filter((v, i, a) => a.indexOf(v) === i) // dedupe
    .join(",");
  const url = `${priceFeedUrl}/simple/price?ids=${ids}&vs_currencies=usd`;

  console.log(`Fetching prices from ${url}`);
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`Price feed returned ${resp.status}: ${await resp.text()}`);
  }

  const data = await resp.json();
  const gorchainPrice = data[gorchainTokenId]?.usd;
  const solanaPrice = data[solanaTokenId]?.usd;

  if (!gorchainPrice || !solanaPrice) {
    // Try fallbacks
    if (config.fallbackGorchainPriceUsd > 0 && config.fallbackSolanaPriceUsd > 0) {
      console.warn("Price feed incomplete, using fallback prices");
      return {
        gorchainPriceUsd: config.fallbackGorchainPriceUsd,
        solanaPriceUsd: config.fallbackSolanaPriceUsd,
      };
    }
    throw new Error(
      `Missing prices in response: gorchain=${gorchainPrice}, solana=${solanaPrice}`
    );
  }

  return { gorchainPriceUsd: gorchainPrice, solanaPriceUsd: solanaPrice };
}

/**
 * Compute token exchange rate for the IGP gas oracle.
 *
 * The exchange rate represents: how many destination-chain native tokens
 * does 1 origin-chain native token buy, scaled by 1e10.
 *
 * exchange_rate = (origin_price_usd / dest_price_usd) * 1e10
 */
export function computeExchangeRate(originPriceUsd, destPriceUsd) {
  if (destPriceUsd <= 0) {
    throw new Error("Destination price must be positive");
  }
  const rate = (originPriceUsd / destPriceUsd) * 1e10;
  return BigInt(Math.round(rate));
}

/**
 * Sanity-check a new exchange rate against a previous value.
 * Returns true if within allowed deviation.
 */
export function isRateReasonable(newRate, previousRate, maxDeviation) {
  if (!previousRate || previousRate === 0n) return true;
  const newNum = Number(newRate);
  const prevNum = Number(previousRate);
  const deviation = Math.abs(newNum - prevNum) / prevNum;
  if (deviation > maxDeviation) {
    console.error(
      `Exchange rate deviation ${(deviation * 100).toFixed(1)}% exceeds max ${(maxDeviation * 100).toFixed(1)}%: ` +
        `previous=${previousRate}, new=${newRate}`
    );
    return false;
  }
  return true;
}
