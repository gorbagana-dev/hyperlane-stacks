/**
 * Privy server wallet integration for signing Solana transactions (Ed25519).
 *
 * Uses Privy's REST API to sign transactions with a server-managed Solana wallet.
 * See: https://docs.privy.io/guide/server-wallets/usage/solana
 */

import bs58 from "bs58";

const PRIVY_API_BASE = "https://auth.privy.io/api/v1";

interface PrivyWallet {
  id: string;
  address: string;
  chain_type: string;
}

interface PrivyTxResult {
  hash?: string;
  signature?: string;
}

export interface PrivyClient {
  getWallet(walletId: string): Promise<PrivyWallet>;
  signAndSendTransaction(
    walletId: string,
    serializedTx: Uint8Array,
    caip2: string,
  ): Promise<PrivyTxResult>;
}

export function createPrivyClient(
  appId: string,
  appSecret: string,
): PrivyClient {
  const authHeader =
    "Basic " + Buffer.from(`${appId}:${appSecret}`).toString("base64");

  async function request(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<any> {
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
      throw new Error(
        `Privy API ${method} ${path} returned ${resp.status}: ${text}`,
      );
    }
    return resp.json();
  }

  return {
    async getWallet(walletId: string): Promise<PrivyWallet> {
      return request("GET", `/wallets/${walletId}`);
    },

    async signAndSendTransaction(
      walletId: string,
      serializedTx: Uint8Array,
      caip2: string,
    ): Promise<PrivyTxResult> {
      const encoded = bs58.encode(serializedTx);
      return request("POST", `/wallets/${walletId}/rpc`, {
        method: "signAndSendTransaction",
        caip2,
        params: {
          encoding: "base58",
          transaction: encoded,
        },
      });
    },
  };
}
