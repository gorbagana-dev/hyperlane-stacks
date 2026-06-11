"""Keypair generation and funding for Hyperlane e2e tests."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .common import E2E_DIR, log_info, run_cmd

KEYS_DIR = E2E_DIR / ".keys"


@dataclass
class KeypairSet:
    keys_dir: Path

    # Ed25519 (Solana) paths
    deployer_path: Path
    owner_path: Path
    igp_oracle_path: Path
    igp_beneficiary_path: Path

    # Ed25519 derived values
    deployer_pubkey: str
    deployer_keypair: str
    owner_pubkey: str
    igp_oracle_pubkey: str
    igp_beneficiary_pubkey: str

    # secp256k1 paths
    gorchain_validator_path: Path
    solana_validator_path: Path

    # secp256k1 derived values
    gorchain_validator_address: str
    solana_validator_address: str
    gorchain_validator_private_key: str  # hex without 0x prefix
    solana_validator_private_key: str    # hex without 0x prefix


def generate_chain_signer(keys_dir: Path, name: str = "chain-signer") -> tuple[str, str]:
    """Generate a chain signer key for the validator announce transaction.

    Returns (hex_key, solana_address):
      - hex_key: 0x-prefixed 32-byte hex seed for HYP_DEFAULTSIGNER_KEY
      - solana_address: base58 Solana address derived from the seed (for funding)

    The key is persisted to {keys_dir}/{name}.json in Solana keypair format
    so it can be reused across test runs (important for --skip-validator-deploy).
    """
    keypair_path = keys_dir / f"{name}.json"

    if keypair_path.is_file():
        log_info(f"Reusing existing chain signer key: {keypair_path}")
    else:
        log_info(f"Generating chain signer key: {keypair_path}")
        _solana_keygen(keypair_path)

    # Solana keypair JSON is a 64-byte array: [seed(32) || pubkey(32)]
    keypair_bytes = json.loads(keypair_path.read_text())
    seed_bytes = bytes(keypair_bytes[:32])
    hex_key = "0x" + seed_bytes.hex()

    solana_address = _solana_pubkey(keypair_path)
    log_info(f"  Chain signer hex:    {hex_key[:10]}...{hex_key[-6:]}")
    log_info(f"  Chain signer addr:   {solana_address}")

    return hex_key, solana_address


def _solana_keygen(output: Path) -> None:
    run_cmd(
        [
            "solana-keygen",
            "new",
            "--no-bip39-passphrase",
            "-o",
            str(output),
            "--force",
        ]
    )


def _solana_pubkey(keypair_path: Path) -> str:
    result = run_cmd(["solana-keygen", "pubkey", str(keypair_path)], quiet=True)
    return result.stdout.strip()


def _cast_wallet_new(output: Path) -> dict[str, str]:
    result = run_cmd(["cast", "wallet", "new", "--json"], quiet=True)
    data = json.loads(result.stdout)
    # cast wallet new --json returns a list of wallets; take the first one
    if isinstance(data, list):
        data = data[0]
    output.write_text(result.stdout)
    return data


def _load_existing_keypairs(keys_dir: Path) -> KeypairSet | None:
    """Try to load previously generated keypairs. Returns None if any are missing."""
    deployer_path = keys_dir / "deployer.json"
    owner_path = keys_dir / "owner.json"
    oracle_path = keys_dir / "igp-oracle.json"
    beneficiary_path = keys_dir / "igp-beneficiary.json"
    gorchain_val_path = keys_dir / "gorchain-validator.json"
    solana_val_path = keys_dir / "solana-validator.json"

    ed25519_files = [deployer_path, owner_path, oracle_path, beneficiary_path]
    secp_files = [gorchain_val_path, solana_val_path]

    if not all(f.is_file() for f in ed25519_files + secp_files):
        return None

    gorchain_data = json.loads(gorchain_val_path.read_text())
    solana_data = json.loads(solana_val_path.read_text())
    if isinstance(gorchain_data, list):
        gorchain_data = gorchain_data[0]
    if isinstance(solana_data, list):
        solana_data = solana_data[0]

    return KeypairSet(
        keys_dir=keys_dir,
        deployer_path=deployer_path,
        owner_path=owner_path,
        igp_oracle_path=oracle_path,
        igp_beneficiary_path=beneficiary_path,
        deployer_pubkey=_solana_pubkey(deployer_path),
        deployer_keypair=deployer_path.read_text().strip(),
        owner_pubkey=_solana_pubkey(owner_path),
        igp_oracle_pubkey=_solana_pubkey(oracle_path),
        igp_beneficiary_pubkey=_solana_pubkey(beneficiary_path),
        gorchain_validator_path=gorchain_val_path,
        solana_validator_path=solana_val_path,
        gorchain_validator_address=gorchain_data["address"],
        solana_validator_address=solana_data["address"],
        gorchain_validator_private_key=gorchain_data["private_key"].removeprefix("0x"),
        solana_validator_private_key=solana_data["private_key"].removeprefix("0x"),
    )


def generate_test_keypairs(keys_dir: Path | None = None) -> KeypairSet:
    if keys_dir is None:
        keys_dir = KEYS_DIR

    # Reuse existing keypairs if all files are present (important for
    # --skip-core-deploy where k8s secrets already reference the old keys)
    existing = _load_existing_keypairs(keys_dir)
    if existing:
        log_info(f"Reusing existing keypairs from {keys_dir}")
        log_info(f"  Deployer pubkey:            {existing.deployer_pubkey}")
        log_info(f"  Bridge owner pubkey:        {existing.owner_pubkey}")
        log_info(f"  IGP oracle pubkey:          {existing.igp_oracle_pubkey}")
        log_info(f"  IGP beneficiary pubkey:     {existing.igp_beneficiary_pubkey}")
        log_info(f"  Gorchain validator (H160):  {existing.gorchain_validator_address}")
        log_info(f"  Solana validator (H160):    {existing.solana_validator_address}")
        return existing

    log_info("Generating test keypairs...")
    keys_dir.mkdir(parents=True, exist_ok=True)

    # --- Ed25519 keypairs (Solana format) ---

    deployer_path = keys_dir / "deployer.json"
    log_info("  Generating deployer keypair...")
    _solana_keygen(deployer_path)
    deployer_pubkey = _solana_pubkey(deployer_path)
    deployer_keypair = deployer_path.read_text().strip()

    owner_path = keys_dir / "owner.json"
    log_info("  Generating bridge-owner keypair...")
    _solana_keygen(owner_path)
    owner_pubkey_val = _solana_pubkey(owner_path)

    oracle_path = keys_dir / "igp-oracle.json"
    log_info("  Generating IGP oracle keypair...")
    _solana_keygen(oracle_path)
    oracle_pubkey = _solana_pubkey(oracle_path)

    beneficiary_path = keys_dir / "igp-beneficiary.json"
    log_info("  Generating IGP beneficiary keypair...")
    _solana_keygen(beneficiary_path)
    beneficiary_pubkey = _solana_pubkey(beneficiary_path)

    # --- secp256k1 keypairs (for validator signing) ---

    gorchain_val_path = keys_dir / "gorchain-validator.json"
    log_info("  Generating Gorchain validator secp256k1 key...")
    gorchain_data = _cast_wallet_new(gorchain_val_path)
    gorchain_address = gorchain_data["address"]
    gorchain_private_key = gorchain_data["private_key"].removeprefix("0x")

    solana_val_path = keys_dir / "solana-validator.json"
    log_info("  Generating Solana validator secp256k1 key...")
    solana_data = _cast_wallet_new(solana_val_path)
    solana_address = solana_data["address"]
    solana_private_key = solana_data["private_key"].removeprefix("0x")

    keypair_set = KeypairSet(
        keys_dir=keys_dir,
        deployer_path=deployer_path,
        owner_path=owner_path,
        igp_oracle_path=oracle_path,
        igp_beneficiary_path=beneficiary_path,
        deployer_pubkey=deployer_pubkey,
        deployer_keypair=deployer_keypair,
        owner_pubkey=owner_pubkey_val,
        igp_oracle_pubkey=oracle_pubkey,
        igp_beneficiary_pubkey=beneficiary_pubkey,
        gorchain_validator_path=gorchain_val_path,
        solana_validator_path=solana_val_path,
        gorchain_validator_address=gorchain_address,
        solana_validator_address=solana_address,
        gorchain_validator_private_key=gorchain_private_key,
        solana_validator_private_key=solana_private_key,
    )

    log_info("Test keypairs generated:")
    log_info(f"  Deployer pubkey:            {deployer_pubkey}")
    log_info(f"  Bridge owner pubkey:        {owner_pubkey_val}")
    log_info(f"  IGP oracle pubkey:          {oracle_pubkey}")
    log_info(f"  IGP beneficiary pubkey:     {beneficiary_pubkey}")
    log_info(f"  Gorchain validator (H160):  {gorchain_address}")
    log_info(f"  Solana validator (H160):    {solana_address}")

    return keypair_set


def _airdrop(amount_sol: int, pubkey: str, rpc: str, label: str) -> None:
    """Airdrop SOL in chunks of 10 (gorchain faucet --per-request-cap 10)."""
    remaining = amount_sol
    chunk = 10
    while remaining > 0:
        this_drop = min(chunk, remaining)
        run_cmd(["solana", "airdrop", str(this_drop), pubkey, "--url", rpc])
        remaining -= this_drop
    log_info(f"    {label}: funded {amount_sol} SOL")


def _sol_balance(pubkey: str, rpc: str) -> float:
    """Native SOL balance of an address; 0.0 if the query fails."""
    result = run_cmd(
        ["solana", "balance", pubkey, "--url", rpc], check=False, quiet=True,
    )
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip().split()[0])
    except (ValueError, IndexError):
        return 0.0


def ensure_sol_balance(pubkey: str, rpc: str, target_sol: int, label: str) -> None:
    """Top up an address to at least target_sol SOL, 10 at a time (faucet cap),
    re-checking after each drop so a silently-dropped airdrop is retried.

    No-op if already funded. Raises RuntimeError if it can't reach the target
    within a bounded number of attempts (so it fails fast with a clear message
    instead of looping or letting the test fail later on insufficient lamports).
    """
    needed_drops = math.ceil(target_sol / 10)
    max_attempts = needed_drops + 3  # a few spare retries for drops that don't land
    balance = _sol_balance(pubkey, rpc)
    attempts = 0
    while balance < target_sol:
        if attempts >= max_attempts:
            raise RuntimeError(
                f"{label}: only reached {balance}/{target_sol} SOL after "
                f"{attempts} airdrops (faucet not funding?)"
            )
        attempts += 1
        _airdrop(10, pubkey, rpc, label)
        balance = _sol_balance(pubkey, rpc)
    log_info(f"    {label}: balance {balance} SOL (target {target_sol})")


def fund_wallets(
    keypair_set: KeypairSet | None = None,
    keys_dir: Path | None = None,
    gorchain_rpc: str = "http://localhost:8899",
    solana_rpc: str = "http://localhost:18899",
) -> None:
    log_info("Funding test wallets...")

    if keys_dir is None:
        keys_dir = KEYS_DIR

    deployer_pubkey = _solana_pubkey(keys_dir / "deployer.json")
    owner_pubkey = _solana_pubkey(keys_dir / "owner.json")
    oracle_pubkey = _solana_pubkey(keys_dir / "igp-oracle.json")
    beneficiary_pubkey = _solana_pubkey(keys_dir / "igp-beneficiary.json")

    deployer_funding = {
        gorchain_rpc: 100,
        solana_rpc: 100,
    }
    chain_names = {gorchain_rpc: "Gorchain", solana_rpc: "Solana"}

    for rpc in [solana_rpc, gorchain_rpc]:
        chain_name = chain_names[rpc]
        log_info(f"  Funding wallets on {chain_name} ({rpc})...")
        _airdrop(deployer_funding[rpc], deployer_pubkey, rpc, "deployer")
        _airdrop(1, owner_pubkey, rpc, "bridge owner")
        _airdrop(1, oracle_pubkey, rpc, "IGP oracle")
        _airdrop(1, beneficiary_pubkey, rpc, "IGP beneficiary")

    log_info("Wallet funding complete")

