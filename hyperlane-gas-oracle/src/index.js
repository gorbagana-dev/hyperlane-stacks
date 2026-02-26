#!/usr/bin/env node

/**
 * Hyperlane Gas Oracle Service
 *
 * Fetches token prices, computes gas oracle configs, and submits
 * SetGasOracleConfigs transactions to the Sealevel IGP program on
 * both chains (Gorchain and Solana) via Privy server wallet signing.
 *
 * Designed to run as a k8s CronJob (one-shot) or as a long-running
 * service with RUN_LOOP=true (for stack-orchestrator deployments).
 */

import { Connection } from "@solana/web3.js";
import { loadConfig } from "./config.js";
import { fetchTokenPrices, computeExchangeRate, isRateReasonable } from "./prices.js";
import { buildSetGasOracleConfigsTx } from "./igp.js";
import { createPrivyClient } from "./privy.js";

async function main() {
  console.log("Starting Hyperlane gas oracle update...");

  const config = loadConfig();
  const privy = createPrivyClient(config.privyAppId, config.privyAppSecret);

  // 1. Fetch token prices
  console.log("Fetching token prices...");
  const prices = await fetchTokenPrices(config);
  console.log(
    `Prices: gorchain=${prices.gorchainPriceUsd} USD, solana=${prices.solanaPriceUsd} USD`
  );

  // 2. Compute exchange rates
  // For Gorchain IGP: remote=Solana → exchange rate = gorchain_price / solana_price
  // (how much Solana native token 1 Gorchain native token buys)
  const gorchainToSolanaRate = computeExchangeRate(
    prices.gorchainPriceUsd,
    prices.solanaPriceUsd
  );
  // For Solana IGP: remote=Gorchain → exchange rate = solana_price / gorchain_price
  const solanaToGorchainRate = computeExchangeRate(
    prices.solanaPriceUsd,
    prices.gorchainPriceUsd
  );

  console.log(
    `Exchange rates (scaled 1e10): gorchain→solana=${gorchainToSolanaRate}, solana→gorchain=${solanaToGorchainRate}`
  );

  // 3. Get oracle wallet info
  console.log("Fetching oracle wallet info from Privy...");
  const wallet = await privy.getWallet(config.privyOracleWalletId);
  const oracleAddress = wallet.address;
  console.log(`Oracle wallet address: ${oracleAddress}`);

  // 4. Build and submit transactions for both chains

  // --- Gorchain IGP: set remote gas config for Solana ---
  await submitGasOracleUpdate({
    chainName: "Gorchain",
    rpcUrl: config.gorchainRpcUrl,
    igpProgramId: config.gorchainIgpProgramId,
    remoteDomain: config.solanaDomainId,
    tokenExchangeRate: gorchainToSolanaRate,
    gasPrice: BigInt(config.solanaGasPrice),
    oracleAddress,
    privy,
    walletId: config.privyOracleWalletId,
    maxDeviation: config.maxPriceDeviation,
  });

  // --- Solana IGP: set remote gas config for Gorchain ---
  await submitGasOracleUpdate({
    chainName: "Solana",
    rpcUrl: config.solanaRpcUrl,
    igpProgramId: config.solanaIgpProgramId,
    remoteDomain: config.gorchainDomainId,
    tokenExchangeRate: solanaToGorchainRate,
    gasPrice: BigInt(config.gorchainGasPrice),
    oracleAddress,
    privy,
    walletId: config.privyOracleWalletId,
    maxDeviation: config.maxPriceDeviation,
  });

  console.log("Gas oracle update complete.");
}

async function submitGasOracleUpdate({
  chainName,
  rpcUrl,
  igpProgramId,
  remoteDomain,
  tokenExchangeRate,
  gasPrice,
  oracleAddress,
  privy,
  walletId,
  maxDeviation,
}) {
  console.log(`\n--- ${chainName} IGP update ---`);
  console.log(
    `  Remote domain: ${remoteDomain}, exchange rate: ${tokenExchangeRate}, gas price: ${gasPrice}`
  );

  // TODO: Read current on-chain gas oracle config and compare for sanity check
  // For now, skip deviation check against on-chain state (would require reading IGP account data)

  const connection = new Connection(rpcUrl, "confirmed");
  const { blockhash } = await connection.getLatestBlockhash();

  const tx = buildSetGasOracleConfigsTx(
    igpProgramId,
    oracleAddress,
    [{ remoteDomain, tokenExchangeRate, gasPrice }],
    blockhash
  );

  // Serialize for Privy signing
  const serializedMsg = tx.serializeMessage();

  console.log(`  Signing and submitting via Privy...`);
  try {
    const result = await privy.signAndSendTransaction(
      walletId,
      serializedMsg,
      // CAIP-2 identifier — for custom SVM chains, this may need to be adjusted
      `solana:${chainName.toLowerCase()}`
    );
    console.log(`  Transaction submitted: ${JSON.stringify(result)}`);
  } catch (err) {
    console.error(`  Failed to submit ${chainName} gas oracle update: ${err.message}`);
    throw err;
  }
}

if (process.env.RUN_LOOP === "true") {
  const interval = parseInt(process.env.GAS_ORACLE_INTERVAL_MS || "900000", 10);
  console.log(`Loop mode enabled (interval: ${interval}ms / ${interval / 60000}min)`);
  const loop = async () => {
    while (true) {
      try {
        await main();
      } catch (err) {
        console.error("Error in gas oracle update (will retry):", err.message);
      }
      await new Promise((r) => setTimeout(r, interval));
    }
  };
  loop();
} else {
  main().catch((err) => {
    console.error("Fatal error:", err);
    process.exit(1);
  });
}
