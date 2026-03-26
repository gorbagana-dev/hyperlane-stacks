/**
 * Build SetGasOracleConfigs transactions using @hyperlane-xyz/sdk.
 *
 * The SDK handles correct Borsh serialization, instruction discriminator,
 * account key ordering, and Option/enum wrappers — all of which the previous
 * manual JS implementation got wrong.
 */

import {
  Connection,
  PublicKey,
  SystemProgram,
  Transaction,
  TransactionInstruction,
} from "@solana/web3.js";
import { serialize } from "borsh";
import {
  SealevelGasOracle,
  SealevelGasOracleConfig,
  SealevelGasOracleType,
  SealevelInstructionWrapper,
  SealevelIgpInstruction,
  SealevelRemoteGasData,
  SealevelSetGasOracleConfigsInstruction,
  SealevelSetGasOracleConfigsInstructionSchema,
} from "@hyperlane-xyz/sdk";

export interface GasOracleUpdate {
  remoteDomain: number;
  tokenExchangeRate: string;
  gasPrice: string;
  tokenDecimals: number;
}

/**
 * Derive the IGP account PDA.
 *
 * Seeds: ["hyperlane_igp", "-", "igp", "-", salt] where salt is H256::zero() by default.
 * Must match: rust/sealevel/programs/hyperlane-sealevel-igp/src/pda_seeds.rs
 */
function deriveIgpAccountPda(igpProgramId: PublicKey): PublicKey {
  const salt = Buffer.alloc(32); // H256::zero()
  const [pda] = PublicKey.findProgramAddressSync(
    [
      Buffer.from("hyperlane_igp"),
      Buffer.from("-"),
      Buffer.from("igp"),
      Buffer.from("-"),
      salt,
    ],
    igpProgramId,
  );
  return pda;
}

/**
 * Build the SetGasOracleConfigs instruction using SDK Borsh schemas.
 */
function createSetGasOracleConfigsInstruction(
  igpProgramId: PublicKey,
  igpAccount: PublicKey,
  owner: PublicKey,
  configs: SealevelGasOracleConfig[],
): TransactionInstruction {
  const value = new SealevelInstructionWrapper({
    instruction: SealevelIgpInstruction.SetGasOracleConfigs,
    data: new SealevelSetGasOracleConfigsInstruction(configs),
  });

  const data = Buffer.from(
    serialize(SealevelSetGasOracleConfigsInstructionSchema, value),
  );

  // Account ordering matches the on-chain program:
  // [system_program, igp_account (writable), owner (signer, writable)]
  return new TransactionInstruction({
    keys: [
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
      { pubkey: igpAccount, isSigner: false, isWritable: true },
      { pubkey: owner, isSigner: true, isWritable: true },
    ],
    programId: igpProgramId,
    data,
  });
}

/**
 * Build a Transaction containing SetGasOracleConfigs for one or more remote domains.
 */
export async function buildSetGasOracleConfigsTx(
  connection: Connection,
  igpProgramIdStr: string,
  ownerAddress: string,
  updates: GasOracleUpdate[],
): Promise<Transaction> {
  const igpProgramId = new PublicKey(igpProgramIdStr);
  const owner = new PublicKey(ownerAddress);
  const igpAccount = deriveIgpAccountPda(igpProgramId);

  const configs = updates.map((update) => {
    const remoteGasData = new SealevelRemoteGasData({
      token_exchange_rate: BigInt(update.tokenExchangeRate),
      gas_price: BigInt(update.gasPrice),
      token_decimals: update.tokenDecimals,
    });

    const gasOracle = new SealevelGasOracle({
      type: SealevelGasOracleType.RemoteGasData,
      data: remoteGasData,
    });

    return new SealevelGasOracleConfig(update.remoteDomain, gasOracle);
  });

  const instruction = createSetGasOracleConfigsInstruction(
    igpProgramId,
    igpAccount,
    owner,
    configs,
  );

  const { blockhash } = await connection.getLatestBlockhash();
  const tx = new Transaction();
  tx.recentBlockhash = blockhash;
  tx.feePayer = owner;
  tx.add(instruction);

  return tx;
}
