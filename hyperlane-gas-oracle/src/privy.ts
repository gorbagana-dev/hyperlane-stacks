/**
 * Privy server wallet integration for signing Solana transactions (Ed25519).
 *
 * Uses Privy's REST API signTransaction (sign-only) endpoint so that the
 * caller controls which RPC the signed transaction is submitted to.
 * See: https://docs.privy.io/api-reference/wallets/solana/sign-transaction
 */

interface PrivyWallet {
  id: string;
  address: string;
  chain_type: string;
}

export interface PrivyClient {
  getWallet(walletId: string): Promise<PrivyWallet>;
  /** Sign a transaction without submitting it. Returns base64-encoded signed tx. */
  signTransaction(
    walletId: string,
    txBase64: string,
  ): Promise<string>;
}

export function createPrivyClient(
  appId: string,
  appSecret: string,
  apiUrl: string = "https://auth.privy.io/api/v1",
): PrivyClient {
  const authHeader =
    "Basic " + Buffer.from(`${appId}:${appSecret}`).toString("base64");

  async function request(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<any> {
    const url = `${apiUrl}${path}`;
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

    async signTransaction(
      walletId: string,
      txBase64: string,
    ): Promise<string> {
      const result = await request("POST", `/wallets/${walletId}/rpc`, {
        method: "signTransaction",
        params: {
          transaction: txBase64,
          encoding: "base64",
        },
      });
      return result.data.signed_transaction;
    },
  };
}
