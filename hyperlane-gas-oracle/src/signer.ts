/**
 * Abstract signer interface for oracle transactions.
 *
 * Two implementations:
 * - PrivySigner: uses Privy signTransaction (sign-only) then submits locally
 * - KeypairSigner: signs with a local Solana keypair (lightweight fallback)
 *
 * Both implementations submit the signed tx to the caller-provided Connection,
 * so the RPC target is always controlled by the oracle service, not by Privy.
 */

import {
  Connection,
  Keypair,
  Transaction,
  sendAndConfirmTransaction,
  sendAndConfirmRawTransaction,
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
  ): Promise<string>;
}

/**
 * Signs via Privy server wallet, submits to our own RPC.
 *
 * Uses Privy's signTransaction (sign-only) endpoint, then sends the
 * signed transaction to the Connection provided by the caller.
 * This gives us full control over which RPC the tx is submitted to.
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
  ): Promise<string> {
    // Serialize the unsigned transaction as base64 for the Privy API
    const txBase64 = tx
      .serialize({ requireAllSignatures: false })
      .toString("base64");

    // Privy signs and returns the signed transaction (does NOT submit)
    const signedBase64 = await this.privy.signTransaction(
      this.walletId,
      txBase64,
    );

    // Submit the signed transaction to our own RPC
    const signedBuffer = Buffer.from(signedBase64, "base64");
    const signature = await sendAndConfirmRawTransaction(
      connection,
      signedBuffer,
      { commitment: "confirmed" },
    );
    return signature;
  }
}

/**
 * Signs transactions with a local Solana keypair (lightweight fallback).
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
