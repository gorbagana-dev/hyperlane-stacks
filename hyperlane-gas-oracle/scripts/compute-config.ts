#!/usr/bin/env -S yarn tsx
/**
 * Compute gas-oracle-configs.json values from current market prices.
 *
 * Fetches sGOR and SOL prices from CoinGecko, computes exchange rates
 * using @hyperlane-xyz/sdk, and outputs the JSON in the format expected
 * by stack_orchestrator/data/config/deployer-gas-oracle-config/gas-oracle-configs.json
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
 *   --gas-price 0.000005             Gas price in SOL (default: 0.000005)
 *   --overhead 200000                Gas overhead in CU (default: 200000)
 *   --multiplier 100                 sGOR→gGOR multiplier (default: 100)
 *   --feed-url https://...           CoinGecko API base URL
 *   --gorchain-token-id gorbagana    CoinGecko token ID for sGOR
 *   --solana-token-id solana         CoinGecko token ID for SOL
 */

import { writeFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { ProtocolType } from "@hyperlane-xyz/utils";
import { getLocalStorageGasOracleConfig } from "@hyperlane-xyz/sdk";

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
const GAS_PRICE = getArg("gas-price") ?? "0.000005";
const OVERHEAD = parseInt(getArg("overhead") ?? "200000", 10);
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
  console.error(`  gasPrice: ${GAS_PRICE} SOL`);
  console.error(`  overhead: ${OVERHEAD} CU`);
  console.error(`  margin:   ${MARGIN}%`);

  const gasOracleParams = {
    gorchain: {
      gasPrice: { amount: GAS_PRICE, decimals: 9 },
      nativeToken: { price: ggorPrice.toString(), decimals: 9 },
    },
    solana: {
      gasPrice: { amount: GAS_PRICE, decimals: 9 },
      nativeToken: { price: solPrice.toString(), decimals: 9 },
    },
  };

  const gorchainConfigs = getLocalStorageGasOracleConfig({
    local: "gorchain",
    localProtocolType: ProtocolType.Sealevel,
    gasOracleParams,
    exchangeRateMarginPct: MARGIN,
  });

  const solanaConfigs = getLocalStorageGasOracleConfig({
    local: "solana",
    localProtocolType: ProtocolType.Sealevel,
    gasOracleParams,
    exchangeRateMarginPct: MARGIN,
  });

  const gorchainToSolana = gorchainConfigs["solana"];
  const solanaToGorchain = solanaConfigs["gorchain"];

  console.error(`\nComputed exchange rates (1e19 Sealevel scale):`);
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
          tokenDecimals: gorchainToSolana.tokenDecimals ?? 9,
        },
        overhead: OVERHEAD,
      },
    },
    solana: {
      gorchain: {
        oracleConfig: {
          tokenExchangeRate: solanaToGorchain.tokenExchangeRate,
          gasPrice: solanaToGorchain.gasPrice,
          tokenDecimals: solanaToGorchain.tokenDecimals ?? 9,
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
