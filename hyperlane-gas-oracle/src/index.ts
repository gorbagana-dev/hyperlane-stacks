#!/usr/bin/env node

/**
 * Hyperlane Gas Oracle Service
 *
 * Fetches token prices, computes gas oracle configs using @hyperlane-xyz/sdk,
 * and submits SetGasOracleConfigs transactions to the Sealevel IGP program
 * on both chains (Gorchain and Solana).
 *
 * Signing is done via Privy server wallet (production) or a local keypair
 * (testing), controlled by SIGNER_MODE env var.
 *
 * Designed to run as a k8s CronJob (one-shot) or as a long-running
 * service with RUN_LOOP=true (for stack-orchestrator deployments).
 */

import { Connection } from "@solana/web3.js";

import { loadConfig, type OracleConfig } from "./config.js";
import { computeOracleConfigs } from "./oracle-config.js";
import { fetchTokenPrices, isRateReasonable } from "./prices.js";
import { buildSetGasOracleConfigsTx, type GasOracleUpdate } from "./igp.js";
import { createPrivyClient } from "./privy.js";
import {
  PrivySigner,
  KeypairSigner,
  type OracleSigner,
} from "./signer.js";

function createSigner(config: OracleConfig): OracleSigner {
  if (config.signerMode === "keypair") {
    console.log("Using local keypair signer");
    return new KeypairSigner(config.oracleKeypair);
  }

  console.log(`Using Privy signer (API: ${config.privyApiUrl})`);
  const privy = createPrivyClient(config.privyAppId, config.privyAppSecret, config.privyApiUrl);
  return new PrivySigner(privy, config.privyOracleWalletId);
}

// Track previous exchange rates for deviation checks
let previousRates: Record<string, string> = {};

async function main(): Promise<void> {
  console.log("Starting Hyperlane gas oracle update...");

  const config = loadConfig();

  if (!config.priceFeedUrl) {
    console.log("PRICE_FEED_URL not set — skipping oracle update.");
    return;
  }

  const signer = createSigner(config);
  const oracleAddress = await signer.getAddress();
  console.log(`Oracle wallet address: ${oracleAddress}`);

  // 1. Fetch token prices
  console.log("Fetching token prices...");
  const prices = await fetchTokenPrices(config);
  console.log(
    `Prices: sGOR=$${prices.gorchainPriceUsd} USD, SOL=$${prices.solanaPriceUsd} USD`,
  );

  // Convert sGOR price to gGOR price (Gorchain native token)
  const gGorPriceUsd =
    prices.gorchainPriceUsd * config.gorchainNativeTokenMultiplier;
  console.log(
    `gGOR price (×${config.gorchainNativeTokenMultiplier}): $${gGorPriceUsd} USD`,
  );

  // 2. Compute oracle configs using SDK
  // Uses getLocalStorageGasOracleConfig() with a min USD cost floor modifier
  // matching Hyperlane's production behavior.
  const { gorchainToSolana, solanaToGorchain } = computeOracleConfigs({
    gorchainPriceUsd: gGorPriceUsd,
    solanaPriceUsd: prices.solanaPriceUsd,
    gasPrice: config.gasPrice,
    exchangeRateMarginPct: config.exchangeRateMarginPct,
    overhead: config.gasOverhead,
    minUsdCost: config.minUsdCost,
  });

  console.log(
    `Gorchain→Solana: exchangeRate=${gorchainToSolana.tokenExchangeRate}, gasPrice=${gorchainToSolana.gasPrice}`,
  );
  console.log(
    `Solana→Gorchain: exchangeRate=${solanaToGorchain.tokenExchangeRate}, gasPrice=${solanaToGorchain.gasPrice}`,
  );

  // 3. Sanity check exchange rates
  const gorKey = `gorchain→solana`;
  const solKey = `solana→gorchain`;
  if (
    !isRateReasonable(
      gorchainToSolana.tokenExchangeRate,
      previousRates[gorKey],
      config.maxPriceDeviation,
    )
  ) {
    throw new Error(
      `${gorKey} exchange rate deviation too large, skipping update`,
    );
  }
  if (
    !isRateReasonable(
      solanaToGorchain.tokenExchangeRate,
      previousRates[solKey],
      config.maxPriceDeviation,
    )
  ) {
    throw new Error(
      `${solKey} exchange rate deviation too large, skipping update`,
    );
  }

  // 4. Build and submit transactions

  // --- Gorchain IGP: set remote gas config for Solana ---
  await submitUpdate({
    chainName: "Gorchain",
    rpcUrl: config.gorchainRpcUrl,
    igpProgramId: config.gorchainIgpProgramId,
    remoteDomain: config.solanaDomainId,
    oracleConfig: gorchainToSolana,
    oracleAddress,
    signer,
  });

  // --- Solana IGP: set remote gas config for Gorchain ---
  await submitUpdate({
    chainName: "Solana",
    rpcUrl: config.solanaRpcUrl,
    igpProgramId: config.solanaIgpProgramId,
    remoteDomain: config.gorchainDomainId,
    oracleConfig: solanaToGorchain,
    oracleAddress,
    signer,
  });

  // Store rates for next iteration's deviation check
  previousRates[gorKey] = gorchainToSolana.tokenExchangeRate;
  previousRates[solKey] = solanaToGorchain.tokenExchangeRate;

  console.log("Gas oracle update complete.");
}

interface SubmitParams {
  chainName: string;
  rpcUrl: string;
  igpProgramId: string;
  remoteDomain: number;
  oracleConfig: {
    tokenExchangeRate: string;
    gasPrice: string;
    tokenDecimals?: number;
  };
  oracleAddress: string;
  signer: OracleSigner;
}

async function submitUpdate(params: SubmitParams): Promise<void> {
  const {
    chainName,
    rpcUrl,
    igpProgramId,
    remoteDomain,
    oracleConfig,
    oracleAddress,
    signer,
  } = params;

  console.log(`\n--- ${chainName} IGP update ---`);
  console.log(
    `  Remote domain: ${remoteDomain}, exchangeRate: ${oracleConfig.tokenExchangeRate}, gasPrice: ${oracleConfig.gasPrice}`,
  );

  const connection = new Connection(rpcUrl, "confirmed");

  const update: GasOracleUpdate = {
    remoteDomain,
    tokenExchangeRate: oracleConfig.tokenExchangeRate,
    gasPrice: oracleConfig.gasPrice,
    tokenDecimals: oracleConfig.tokenDecimals ?? 9,
  };

  const tx = await buildSetGasOracleConfigsTx(
    connection,
    igpProgramId,
    oracleAddress,
    [update],
  );

  console.log(`  Signing and submitting...`);
  try {
    const signature = await signer.signAndSend(connection, tx);
    console.log(`  Transaction submitted: ${signature}`);
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(
      `  Failed to submit ${chainName} gas oracle update: ${message}`,
    );
    throw err;
  }
}

// --- Entry point ---

const config = loadConfig();

if (config.runLoop) {
  console.log(
    `Loop mode enabled (interval: ${config.intervalMs}ms / ${(config.intervalMs / 60000).toFixed(1)}min)`,
  );
  const loop = async (): Promise<void> => {
    while (true) {
      try {
        await main();
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        console.error("Error in gas oracle update (will retry):", message);
      }
      await new Promise((r) => setTimeout(r, config.intervalMs));
    }
  };
  loop();
} else {
  main().catch((err) => {
    console.error("Fatal error:", err);
    process.exit(1);
  });
}
