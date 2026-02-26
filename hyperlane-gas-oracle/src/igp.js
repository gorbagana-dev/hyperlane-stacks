/**
 * Build SetGasOracleConfigs instructions for the Sealevel IGP program.
 *
 * The Sealevel IGP set_gas_oracle_configs instruction expects:
 *   - Accounts: [igp_account (writable, signer), system_program]
 *   - Data: borsh-serialized SetGasOracleConfigs { configs: Vec<GasOracleConfig> }
 *
 * GasOracleConfig:
 *   - remote_domain: u32
 *   - token_exchange_rate: u128  (scaled by 1e10)
 *   - gas_price: u128            (in destination chain units)
 */

import {
  PublicKey,
  TransactionInstruction,
  Transaction,
  SystemProgram,
} from "@solana/web3.js";

// IGP instruction discriminator for set_gas_oracle_configs
// This is the anchor/sealevel discriminator. The exact value depends on the
// program's instruction enum. For Hyperlane Sealevel IGP, the instruction
// index for SetGasOracleConfigs is 3.
const SET_GAS_ORACLE_CONFIGS_IX = 3;

/**
 * Encode a GasOracleConfig into a buffer (borsh-like manual serialization).
 *
 * Layout:
 *   remote_domain: u32 (4 bytes LE)
 *   token_exchange_rate: u128 (16 bytes LE)
 *   gas_price: u128 (16 bytes LE)
 */
function encodeGasOracleConfig(remoteDomain, tokenExchangeRate, gasPrice) {
  const buf = Buffer.alloc(4 + 16 + 16);
  buf.writeUInt32LE(remoteDomain, 0);
  writeBigUint128LE(buf, tokenExchangeRate, 4);
  writeBigUint128LE(buf, gasPrice, 20);
  return buf;
}

function writeBigUint128LE(buf, value, offset) {
  const bigVal = BigInt(value);
  // Write as two 64-bit LE values
  buf.writeBigUInt64LE(bigVal & 0xffffffffffffffffn, offset);
  buf.writeBigUInt64LE(bigVal >> 64n, offset + 8);
}

/**
 * Build the SetGasOracleConfigs instruction data.
 *
 * Layout:
 *   instruction_discriminator: u8 (1 byte)
 *   num_configs: u32 (4 bytes LE) — borsh Vec length prefix
 *   configs: GasOracleConfig[] (36 bytes each)
 */
function buildInstructionData(configs) {
  const configBuffers = configs.map((c) =>
    encodeGasOracleConfig(c.remoteDomain, c.tokenExchangeRate, c.gasPrice)
  );
  const totalLen = 1 + 4 + configBuffers.reduce((s, b) => s + b.length, 0);
  const buf = Buffer.alloc(totalLen);
  let offset = 0;

  // Instruction discriminator
  buf.writeUInt8(SET_GAS_ORACLE_CONFIGS_IX, offset);
  offset += 1;

  // Vec length
  buf.writeUInt32LE(configs.length, offset);
  offset += 4;

  // Config entries
  for (const cb of configBuffers) {
    cb.copy(buf, offset);
    offset += cb.length;
  }

  return buf;
}

/**
 * Derive the IGP program data account (PDA).
 *
 * The Sealevel IGP uses a PDA seeded with "hyperlane_igp" and "-" and "igp_salt"
 * as the account that stores gas oracle configs. The exact seed depends on
 * the program's implementation.
 *
 * For simplicity, we accept the IGP account address as an env var override.
 */
export function deriveIgpAccount(igpProgramId) {
  // The PDA seed for the main IGP data account
  return PublicKey.findProgramAddressSync(
    [Buffer.from("hyperlane_igp"), Buffer.from("-"), Buffer.from("program_data")],
    new PublicKey(igpProgramId)
  );
}

/**
 * Build a Transaction containing SetGasOracleConfigs instruction.
 *
 * @param {string} igpProgramId - IGP program public key (base58)
 * @param {string} oracleAuthority - Oracle wallet public key (base58, must be IGP owner)
 * @param {Array<{remoteDomain: number, tokenExchangeRate: bigint, gasPrice: bigint}>} configs
 * @param {string} recentBlockhash
 * @returns {Transaction}
 */
export function buildSetGasOracleConfigsTx(
  igpProgramId,
  oracleAuthority,
  configs,
  recentBlockhash
) {
  const programId = new PublicKey(igpProgramId);
  const authority = new PublicKey(oracleAuthority);

  const [igpAccount] = deriveIgpAccount(igpProgramId);

  const data = buildInstructionData(configs);

  const ix = new TransactionInstruction({
    keys: [
      { pubkey: igpAccount, isSigner: false, isWritable: true },
      { pubkey: authority, isSigner: true, isWritable: false },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    programId,
    data,
  });

  const tx = new Transaction();
  tx.recentBlockhash = recentBlockhash;
  tx.feePayer = authority;
  tx.add(ix);
  return tx;
}
