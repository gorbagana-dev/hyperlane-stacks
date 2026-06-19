// Close the bridge's upgradeable programs on one chain, reclaiming each program's
// rent to a treasury via a Privy-signed BPFLoaderUpgradeable Close. Driven by
// decommission.yml — see it and runbooks/funding-estimate.md for the rationale.
//
// Env:
//   CHAIN                         gorchain | solana (selects the program-ids slice)
//   RPC_URL                       chain RPC
//   TREASURY_ADDRESS              base58 — receives the reclaimed rent
//   BRIDGE_OWNER_PUBKEY           base58 — upgrade authority + fee payer
//   PRIVY_APP_ID / PRIVY_APP_SECRET / PRIVY_BRIDGE_OWNER_WALLET_ID
//   PRIVY_API_URL                 default https://api.privy.io/v1
//   STATE_DIR                     default /state — deployer's generated/ (read-only)
//   DRY_RUN                       "false" to actually close; anything else simulates

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import {
  Connection,
  PublicKey,
  Transaction,
  TransactionInstruction,
} from "@solana/web3.js";

const BPF_LOADER_UPGRADEABLE = new PublicKey(
  "BPFLoaderUpgradeab1e11111111111111111111111",
);
// UpgradeableLoaderInstruction::Close is enum variant 5 (u32 LE).
const CLOSE_IX_DATA = Buffer.from([5, 0, 0, 0]);
// The program-ids keys deploy.sh hands upgrade authority to (the rest are PDAs).
const CORE_PROGRAM_KEYS = [
  "mailbox",
  "validator_announce",
  "multisig_ism_message_id",
  "igp_program_id",
];

const env = (k, dflt) => {
  const v = process.env[k];
  if (v === undefined || v === "") {
    if (dflt !== undefined) return dflt;
    throw new Error(`Missing required env ${k}`);
  }
  return v;
};

const CHAIN = env("CHAIN");
const RPC_URL = env("RPC_URL");
const TREASURY = new PublicKey(env("TREASURY_ADDRESS"));
const BRIDGE_OWNER = new PublicKey(env("BRIDGE_OWNER_PUBKEY"));
const APP_ID = env("PRIVY_APP_ID");
const APP_SECRET = env("PRIVY_APP_SECRET");
const WALLET_ID = env("PRIVY_BRIDGE_OWNER_WALLET_ID");
const PRIVY_API_URL = env("PRIVY_API_URL", "https://api.privy.io/v1");
const STATE_DIR = env("STATE_DIR", "/state");
const DRY_RUN = env("DRY_RUN", "true") !== "false";

function collectPrograms() {
  const out = [];
  const core = JSON.parse(
    readFileSync(join(STATE_DIR, "program-ids.json"), "utf8"),
  );
  const chainCore = core[CHAIN];
  if (!chainCore) throw new Error(`program-ids.json has no '${CHAIN}' slice`);
  for (const key of CORE_PROGRAM_KEYS) {
    if (chainCore[key]) out.push({ label: key, id: chainCore[key] });
  }
  let routes = [];
  try {
    routes = readdirSync(join(STATE_DIR, "warp-routes"), { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name);
  } catch {
    /* no warp routes deployed */
  }
  for (const name of routes) {
    const f = join(STATE_DIR, "warp-routes", name, "warp-deploy-outputs", "program-ids.json");
    let warp;
    try {
      warp = JSON.parse(readFileSync(f, "utf8"));
    } catch {
      continue;
    }
    const id = warp?.[CHAIN]?.base58;
    if (id) out.push({ label: `warp:${name}`, id });
  }
  return out;
}

async function privySignTransaction(txBase64) {
  const auth = "Basic " + Buffer.from(`${APP_ID}:${APP_SECRET}`).toString("base64");
  const res = await fetch(`${PRIVY_API_URL}/wallets/${WALLET_ID}/rpc`, {
    method: "POST",
    headers: {
      Authorization: auth,
      "Content-Type": "application/json",
      "privy-app-id": APP_ID,
    },
    body: JSON.stringify({
      method: "signTransaction",
      params: { transaction: txBase64, encoding: "base64" },
    }),
  });
  if (!res.ok) {
    throw new Error(`Privy signTransaction ${res.status}: ${await res.text()}`);
  }
  const signed = (await res.json())?.data?.signed_transaction;
  if (!signed) throw new Error("Privy signTransaction returned no signed_transaction");
  return signed;
}

// HTTP-only submit + poll (no WebSocket; the k8s RPCs don't expose the WS port).
// searchTransactionHistory so a tx that ages out of the status cache near the
// deadline isn't misreported as failed for an irreversible close.
async function sendAndConfirm(connection, raw) {
  const sig = await connection.sendRawTransaction(raw, {
    skipPreflight: false,
    preflightCommitment: "confirmed",
  });
  const start = Date.now();
  while (Date.now() - start < 60000) {
    const { value } = await connection.getSignatureStatuses([sig], {
      searchTransactionHistory: true,
    });
    const st = value[0];
    if (st) {
      if (st.err) throw new Error(`tx ${sig} failed: ${JSON.stringify(st.err)}`);
      if (st.confirmationStatus === "confirmed" || st.confirmationStatus === "finalized") {
        return sig;
      }
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  // Final authoritative check before declaring failure.
  const { value } = await connection.getSignatureStatuses([sig], {
    searchTransactionHistory: true,
  });
  const st = value[0];
  if (st && !st.err && (st.confirmationStatus === "confirmed" || st.confirmationStatus === "finalized")) {
    return sig;
  }
  throw new Error(`tx not confirmed in 60s; verify ${sig} on-chain before re-running`);
}

// ProgramData layout: u32 tag(=3) | u64 slot | Option<Pubkey> upgrade authority.
function parseUpgradeAuthority(data) {
  if (data.length < 13) return null;
  const hasAuthority = data[12] === 1;
  if (!hasAuthority) return null;
  return new PublicKey(data.subarray(13, 45));
}

async function closeProgram(connection, { label, id }) {
  const program = new PublicKey(id);
  const [programData] = PublicKey.findProgramAddressSync(
    [program.toBuffer()],
    BPF_LOADER_UPGRADEABLE,
  );
  const info = await connection.getAccountInfo(programData, "confirmed");
  if (!info) {
    console.log(`  SKIP ${label} (${id}): programData absent — already closed`);
    return { label, id, status: "skipped" };
  }
  if (!info.owner.equals(BPF_LOADER_UPGRADEABLE)) {
    console.log(`  SKIP ${label} (${id}): programData not owned by the upgradeable loader`);
    return { label, id, status: "skipped" };
  }
  const authority = parseUpgradeAuthority(info.data);
  const reclaimSol = (info.lamports / 1e9).toFixed(4);
  console.log(`  ${label} (${id}): reclaim ~${reclaimSol} SOL → treasury; authority ${authority?.toBase58() ?? "none"}`);
  if (!authority || !authority.equals(BRIDGE_OWNER)) {
    console.log(`  SKIP ${label}: upgrade authority is not BRIDGE_OWNER_PUBKEY — Privy can't sign its close`);
    return { label, id, status: "wrong-authority" };
  }

  const ix = new TransactionInstruction({
    programId: BPF_LOADER_UPGRADEABLE,
    keys: [
      { pubkey: programData, isSigner: false, isWritable: true },
      { pubkey: TREASURY, isSigner: false, isWritable: true },
      { pubkey: BRIDGE_OWNER, isSigner: true, isWritable: false },
      { pubkey: program, isSigner: false, isWritable: true },
    ],
    data: CLOSE_IX_DATA,
  });
  const tx = new Transaction().add(ix);
  tx.feePayer = BRIDGE_OWNER;
  tx.recentBlockhash = (await connection.getLatestBlockhash("confirmed")).blockhash;

  if (DRY_RUN) {
    let simOk = false;
    try {
      const sim = await connection.simulateTransaction(tx, undefined, false);
      simOk = !sim.value.err;
      console.log(`    DRY-RUN simulate: ${simOk ? "OK" : "ERR " + JSON.stringify(sim.value.err)}`);
    } catch (e) {
      console.log(`    DRY-RUN simulate could not run (treat as unverified): ${e.message}`);
    }
    return { label, id, status: "dry-run", reclaimSol, simOk };
  }

  const signed = await privySignTransaction(
    tx.serialize({ requireAllSignatures: false }).toString("base64"),
  );
  const sig = await sendAndConfirm(connection, Buffer.from(signed, "base64"));
  console.log(`    CLOSED ${label}: ${sig} (reclaimed ~${reclaimSol} SOL)`);
  return { label, id, status: "closed", reclaimSol, signature: sig };
}

async function main() {
  console.log(
    `${DRY_RUN ? "[DRY-RUN] " : ""}Decommission on ${CHAIN} → treasury ${TREASURY.toBase58()}`,
  );
  const programs = collectPrograms();
  // Both chains always carry the 4 core programs, so an empty set means the state
  // dir is wrong/missing, not a finished decommission — fail rather than no-op.
  if (programs.length === 0) {
    throw new Error(`No programs found in ${STATE_DIR}/program-ids.json for '${CHAIN}'`);
  }
  const connection = new Connection(RPC_URL, "confirmed");
  // The bridge owner signs and pays each close; an unfunded fee payer makes both
  // simulate and the real close fail AccountNotFound.
  if ((await connection.getBalance(BRIDGE_OWNER, "confirmed")) === 0) {
    console.log(
      `  WARNING: fee payer ${BRIDGE_OWNER.toBase58()} (bridge owner) has 0 balance on ${CHAIN} — ` +
        "fund it before a real close, or every tx fails AccountNotFound.",
    );
  }
  const results = [];
  for (const p of programs) {
    try {
      results.push(await closeProgram(connection, p));
    } catch (e) {
      console.log(`  ERROR ${p.label} (${p.id}): ${e.message}`);
      results.push({ label: p.label, id: p.id, status: "error", error: e.message });
    }
  }

  const by = (s) => results.filter((r) => r.status === s);
  const reclaimed = results
    .filter((r) => r.reclaimSol)
    .reduce((s, r) => s + Number(r.reclaimSol), 0);
  const simUnverified = DRY_RUN && by("dry-run").some((r) => !r.simOk);
  console.log(
    `\nSummary (${CHAIN}): ${by("closed").length} closed, ${by("dry-run").length} dry-run, ` +
      `${by("skipped").length} skipped, ${by("wrong-authority").length} wrong-authority, ` +
      `${by("error").length} error; ~${reclaimed.toFixed(4)} SOL ` +
      `${DRY_RUN ? "reclaimable" : "reclaimed"}${simUnverified ? " (some closes unsimulated)" : ""}.`,
  );
  if (by("error").length > 0 || by("wrong-authority").length > 0) {
    process.exitCode = 1;
  }
}

main().catch((e) => {
  console.error(`FATAL: ${e.message}`);
  process.exit(1);
});
