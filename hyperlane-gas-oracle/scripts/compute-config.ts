#!/usr/bin/env -S yarn tsx
/**
 * Compute gas-oracle-configs.json values from current market prices.
 *
 * Fetches sGOR and SOL prices from CoinGecko, computes exchange rates
 * using @hyperlane-xyz/sdk, and outputs the JSON in the format expected
 * by stack_orchestrator/data/config/deployer-gas-oracle-config/gas-oracle-configs.json
 *
 * Uses the same computeOracleConfigs() function as the gas oracle service,
 * including the min USD cost floor modifier.
 *
 * Usage:
 *   cd hyperlane-gas-oracle
 *   npm install           # if not done
 *   npx tsx scripts/compute-config.ts
 *
 * To write directly to the deployer config:
 *   npx tsx scripts/compute-config.ts --write
 *
 * Override prices manually (skip CoinGecko fetch):
 *   npx tsx scripts/compute-config.ts --sgor-price 0.10 --sol-price 150
 *
 * Override parameters:
 *   --margin 10                      Exchange rate margin % (default: 10)
 *   --gas-price 0.000000001          Gas price in SOL (default: 0.000000001)
 *   --overhead 200000                Gas overhead in CU (default: 200000)
 *   --min-usd-cost 0.50             Min USD cost floor (default: 0.50, 0 = disabled)
 *   --multiplier 100                 sGOR→gGOR multiplier (default: 100)
 *   --feed-url https://...           CoinGecko API base URL
 *   --gorchain-token-id gorbagana    CoinGecko token ID for sGOR
 *   --solana-token-id solana         CoinGecko token ID for SOL
 */

import { writeFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { computeOracleConfigs } from "../src/oracle-config.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = resolve(
  __dirname,
  "../../stack_orchestrator/data/config/deployer-gas-oracle-config/gas-oracle-configs.json",
);

// --- CLI argument parsing ---

function getArg(name: string): string | undefined {
  const idx = process.argv.indexOf(`--${name}`);
  if (idx === -1 || idx + 1 >= process.argv.length) return undefined;
  return process.argv[idx + 1];
}

function hasFlag(name: string): boolean {
  return process.argv.includes(`--${name}`);
}

const MARGIN = parseFloat(getArg("margin") ?? "10");
const GAS_PRICE = getArg("gas-price") ?? "0.000000001";
const OVERHEAD = parseInt(getArg("overhead") ?? "200000", 10);
const MIN_USD_COST = parseFloat(getArg("min-usd-cost") ?? "0.50");
const MULTIPLIER = parseFloat(getArg("multiplier") ?? "100");
const FEED_URL = getArg("feed-url") ?? "https://api.coingecko.com/api/v3";
const GORCHAIN_TOKEN_ID = getArg("gorchain-token-id") ?? "gorbagana";
const SOLANA_TOKEN_ID = getArg("solana-token-id") ?? "solana";
const WRITE = hasFlag("write");

// --- Fetch prices ---

async function fetchPrices(): Promise<{ sgorPrice: number; solPrice: number }> {
  const manualSgor = getArg("sgor-price");
  const manualSol = getArg("sol-price");

  if (manualSgor && manualSol) {
    return {
      sgorPrice: parseFloat(manualSgor),
      solPrice: parseFloat(manualSol),
    };
  }

  const ids = [...new Set([GORCHAIN_TOKEN_ID, SOLANA_TOKEN_ID])].join(",");
  const url = `${FEED_URL}/simple/price?ids=${ids}&vs_currencies=usd`;
  console.error(`Fetching prices from ${url} ...`);

  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`Price feed returned ${resp.status}: ${await resp.text()}`);
  }

  const data = (await resp.json()) as Record<string, { usd?: number }>;
  const sgorPrice = data[GORCHAIN_TOKEN_ID]?.usd;
  const solPrice = data[SOLANA_TOKEN_ID]?.usd;

  if (!sgorPrice || !solPrice) {
    throw new Error(
      `Missing prices: ${GORCHAIN_TOKEN_ID}=${sgorPrice}, ${SOLANA_TOKEN_ID}=${solPrice}`,
    );
  }

  return { sgorPrice, solPrice };
}

// --- Main ---

async function main(): Promise<void> {
  const { sgorPrice, solPrice } = await fetchPrices();
  const ggorPrice = sgorPrice * MULTIPLIER;

  console.error(`\nPrices:`);
  console.error(`  sGOR: $${sgorPrice} USD`);
  console.error(`  SOL:  $${solPrice} USD`);
  console.error(`  gGOR: $${ggorPrice} USD (sGOR × ${MULTIPLIER})`);
  console.error(`\nParameters:`);
  console.error(`  gasPrice:    ${GAS_PRICE} SOL`);
  console.error(`  overhead:    ${OVERHEAD} CU`);
  console.error(`  margin:      ${MARGIN}%`);
  console.error(`  minUsdCost:  $${MIN_USD_COST}`);

  const { gorchainToSolana, solanaToGorchain } = computeOracleConfigs({
    gorchainPriceUsd: ggorPrice,
    solanaPriceUsd: solPrice,
    gasPrice: GAS_PRICE,
    exchangeRateMarginPct: MARGIN,
    overhead: OVERHEAD,
    minUsdCost: MIN_USD_COST,
  });

  console.error(`\nComputed oracle configs:`);
  console.error(
    `  gorchain→solana: exchangeRate=${gorchainToSolana.tokenExchangeRate}, gasPrice=${gorchainToSolana.gasPrice}`,
  );
  console.error(
    `  solana→gorchain: exchangeRate=${solanaToGorchain.tokenExchangeRate}, gasPrice=${solanaToGorchain.gasPrice}`,
  );

  const config = {
    gorchain: {
      solana: {
        oracleConfig: {
          tokenExchangeRate: gorchainToSolana.tokenExchangeRate,
          gasPrice: gorchainToSolana.gasPrice,
          tokenDecimals: gorchainToSolana.tokenDecimals,
        },
        overhead: OVERHEAD,
      },
    },
    solana: {
      gorchain: {
        oracleConfig: {
          tokenExchangeRate: solanaToGorchain.tokenExchangeRate,
          gasPrice: solanaToGorchain.gasPrice,
          tokenDecimals: solanaToGorchain.tokenDecimals,
        },
        overhead: OVERHEAD,
      },
    },
  };

  const json = JSON.stringify(config, null, 2) + "\n";

  if (WRITE) {
    writeFileSync(CONFIG_PATH, json);
    console.error(`\nWritten to ${CONFIG_PATH}`);
  } else {
    // Output JSON to stdout (diagnostics go to stderr)
    process.stdout.write(json);
    console.error(`\nTo write to deployer config, re-run with --write`);
  }
}

main().catch((err) => {
  console.error("Error:", err instanceof Error ? err.message : err);
  process.exit(1);
});
