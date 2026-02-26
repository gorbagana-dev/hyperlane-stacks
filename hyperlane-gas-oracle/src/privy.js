/**
 * Privy server wallet integration for signing Solana transactions (Ed25519).
 *
 * Uses Privy's REST API to sign transactions with a server-managed Solana wallet.
 * See: https://docs.privy.io/guide/server-wallets/usage/solana
 */

import bs58 from "bs58";

const PRIVY_API_BASE = "https://auth.privy.io/api/v1";

/**
 * Create a Privy API client.
 */
export function createPrivyClient(appId, appSecret) {
  const authHeader =
    "Basic " + Buffer.from(`${appId}:${appSecret}`).toString("base64");

  async function request(method, path, body) {
    const url = `${PRIVY_API_BASE}${path}`;
    const resp = await fetch(url, {
      method,
      headers: {
        Authorization: authHeader,
        "Content-Type": "application/json",
        "privy-app-id": appId,
      },
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Privy API ${method} ${path} returned ${resp.status}: ${text}`);
    }
    return resp.json();
  }

  return {
    /**
     * Get wallet info including the Solana public key.
     */
    async getWallet(walletId) {
      return request("GET", `/wallets/${walletId}`);
    },

    /**
     * Sign a Solana transaction.
     *
     * @param {string} walletId - Privy wallet ID
     * @param {Buffer|Uint8Array} serializedTx - Serialized transaction message
     * @returns {string} Base58-encoded signature
     */
    async signTransaction(walletId, serializedTx) {
      const encoded = bs58.encode(
        serializedTx instanceof Uint8Array
          ? serializedTx
          : Uint8Array.from(serializedTx)
      );
      const result = await request(
        "POST",
        `/wallets/${walletId}/rpc`,
        {
          method: "signTransaction",
          params: {
            encoding: "base58",
            transaction: encoded,
          },
        }
      );
      return result;
    },

    /**
     * Sign and send a Solana transaction.
     *
     * @param {string} walletId - Privy wallet ID
     * @param {Buffer|Uint8Array} serializedTx - Serialized transaction message
     * @param {string} caip2 - Chain identifier (e.g., "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
     * @returns {object} Transaction result with signature
     */
    async signAndSendTransaction(walletId, serializedTx, caip2) {
      const encoded = bs58.encode(
        serializedTx instanceof Uint8Array
          ? serializedTx
          : Uint8Array.from(serializedTx)
      );
      const result = await request(
        "POST",
        `/wallets/${walletId}/rpc`,
        {
          method: "signAndSendTransaction",
          caip2,
          params: {
            encoding: "base58",
            transaction: encoded,
          },
        }
      );
      return result;
    },
  };
}
