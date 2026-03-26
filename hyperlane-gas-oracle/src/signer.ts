/**
 * Abstract signer interface for oracle transactions.
 *
 * Two implementations:
 * - PrivySigner: production (calls Privy signAndSendTransaction)
 * - KeypairSigner: testing (signs with local Solana keypair)
 */

import {
  Connection,
  Keypair,
  Transaction,
  sendAndConfirmTransaction,
} from "@solana/web3.js";
import type { PrivyClient } from "./privy.js";

export interface OracleSigner {
  /** Get the signer's Solana public key (base58). */
  getAddress(): Promise<string>;

  /**
   * Sign and send a transaction.
   * @returns Transaction signature (base58).
   */
  signAndSend(
    connection: Connection,
    tx: Transaction,
    chainName: string,
  ): Promise<string>;
}

/**
 * Signs transactions via Privy server wallet (production).
 */
export class PrivySigner implements OracleSigner {
  private address: string | null = null;

  constructor(
    private privy: PrivyClient,
    private walletId: string,
  ) {}

  async getAddress(): Promise<string> {
    if (!this.address) {
      const wallet = await this.privy.getWallet(this.walletId);
      this.address = wallet.address;
    }
    return this.address;
  }

  async signAndSend(
    connection: Connection,
    tx: Transaction,
    chainName: string,
  ): Promise<string> {
    const serializedMsg = tx.serializeMessage();
    // CAIP-2 identifier for Solana chains
    const caip2 = `solana:${chainName.toLowerCase()}`;
    const result = await this.privy.signAndSendTransaction(
      this.walletId,
      serializedMsg,
      caip2,
    );
    return result.hash || result.signature || JSON.stringify(result);
  }
}

/**
 * Signs transactions with a local Solana keypair (testing).
 */
export class KeypairSigner implements OracleSigner {
  private keypair: Keypair;

  constructor(keypairJson: number[]) {
    this.keypair = Keypair.fromSecretKey(Uint8Array.from(keypairJson));
  }

  async getAddress(): Promise<string> {
    return this.keypair.publicKey.toBase58();
  }

  async signAndSend(
    connection: Connection,
    tx: Transaction,
  ): Promise<string> {
    const signature = await sendAndConfirmTransaction(connection, tx, [
      this.keypair,
    ]);
    return signature;
  }
}
