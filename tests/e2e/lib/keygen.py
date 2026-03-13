"""Keypair generation and funding for Hyperlane e2e tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .common import E2E_DIR, log_info, run_cmd

KEYS_DIR = E2E_DIR / ".keys"


@dataclass
class KeypairSet:
    keys_dir: Path

    # Ed25519 (Solana) paths
    deployer_path: Path
    hardware_wallet_path: Path
    igp_oracle_path: Path

    # Ed25519 derived values
    deployer_pubkey: str
    deployer_keypair: str
    hardware_wallet_pubkey: str
    igp_oracle_pubkey: str

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
    hw_path = keys_dir / "hardware-wallet.json"
    oracle_path = keys_dir / "igp-oracle.json"
    gorchain_val_path = keys_dir / "gorchain-validator.json"
    solana_val_path = keys_dir / "solana-validator.json"

    ed25519_files = [deployer_path, hw_path, oracle_path]
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
        hardware_wallet_path=hw_path,
        igp_oracle_path=oracle_path,
        deployer_pubkey=_solana_pubkey(deployer_path),
        deployer_keypair=deployer_path.read_text().strip(),
        hardware_wallet_pubkey=_solana_pubkey(hw_path),
        igp_oracle_pubkey=_solana_pubkey(oracle_path),
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
        log_info(f"  Hardware wallet pubkey:     {existing.hardware_wallet_pubkey}")
        log_info(f"  IGP oracle pubkey:          {existing.igp_oracle_pubkey}")
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

    hw_path = keys_dir / "hardware-wallet.json"
    log_info("  Generating hardware wallet keypair...")
    _solana_keygen(hw_path)
    hw_pubkey = _solana_pubkey(hw_path)

    oracle_path = keys_dir / "igp-oracle.json"
    log_info("  Generating IGP oracle keypair...")
    _solana_keygen(oracle_path)
    oracle_pubkey = _solana_pubkey(oracle_path)

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
        hardware_wallet_path=hw_path,
        igp_oracle_path=oracle_path,
        deployer_pubkey=deployer_pubkey,
        deployer_keypair=deployer_keypair,
        hardware_wallet_pubkey=hw_pubkey,
        igp_oracle_pubkey=oracle_pubkey,
        gorchain_validator_path=gorchain_val_path,
        solana_validator_path=solana_val_path,
        gorchain_validator_address=gorchain_address,
        solana_validator_address=solana_address,
        gorchain_validator_private_key=gorchain_private_key,
        solana_validator_private_key=solana_private_key,
    )

    log_info("Test keypairs generated:")
    log_info(f"  Deployer pubkey:            {deployer_pubkey}")
    log_info(f"  Hardware wallet pubkey:     {hw_pubkey}")
    log_info(f"  IGP oracle pubkey:          {oracle_pubkey}")
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
    hw_pubkey = _solana_pubkey(keys_dir / "hardware-wallet.json")
    oracle_pubkey = _solana_pubkey(keys_dir / "igp-oracle.json")

    for rpc, chain_name in [(solana_rpc, "Solana"), (gorchain_rpc, "Gorchain")]:
        log_info(f"  Funding wallets on {chain_name} ({rpc})...")
        _airdrop(100, deployer_pubkey, rpc, "deployer")
        _airdrop(1, hw_pubkey, rpc, "hardware wallet")
        _airdrop(1, oracle_pubkey, rpc, "IGP oracle")

    log_info("Wallet funding complete")


def create_deployer_secrets(namespace: str, keypair_set: KeypairSet) -> None:
    log_info(f"Creating deployer secrets in namespace {namespace}...")

    # Generate yaml via dry-run, then apply (idempotent)
    gen = run_cmd(
        [
            "kubectl",
            "create",
            "secret",
            "generic",
            "hyperlane-deployer-secrets",
            "-n",
            namespace,
            f"--from-literal=DEPLOYER_KEYPAIR={keypair_set.deployer_keypair}",
            f"--from-literal=HARDWARE_WALLET_PUBKEY={keypair_set.hardware_wallet_pubkey}",
            f"--from-literal=IGP_ORACLE_PUBKEY={keypair_set.igp_oracle_pubkey}",
            f"--from-literal=GORCHAIN_VALIDATOR_ADDRESS={keypair_set.gorchain_validator_address}",
            f"--from-literal=SOLANA_VALIDATOR_ADDRESS={keypair_set.solana_validator_address}",
            "--dry-run=client",
            "-o",
            "yaml",
        ]
    )

    run_cmd(["kubectl", "apply", "-f", "-"], input_text=gen.stdout)
    log_info("Deployer secrets created")


def create_minio_secrets(namespace: str, user: str, password: str) -> None:
    """Create the hyperlane-minio-secrets k8s Secret (idempotent)."""
    log_info(f"Creating minio secrets in namespace {namespace}...")
    gen = run_cmd(
        [
            "kubectl", "create", "secret", "generic", "hyperlane-minio-secrets",
            "-n", namespace,
            f"--from-literal=MINIO_ROOT_USER={user}",
            f"--from-literal=MINIO_ROOT_PASSWORD={password}",
            "--dry-run=client", "-o", "yaml",
        ]
    )
    run_cmd(["kubectl", "apply", "-f", "-"], input_text=gen.stdout)
    log_info("Minio secrets created")


def create_validator_secrets(
    namespace: str,
    chain: str,
    minio_user: str,
    minio_password: str,
    chain_signer_key: str = "",
) -> None:
    """Create the hyperlane-validator-{chain}-secrets k8s Secret (idempotent)."""
    secret_name = f"hyperlane-validator-{chain}-secrets"
    log_info(f"Creating {secret_name} in namespace {namespace}...")
    cmd = [
        "kubectl", "create", "secret", "generic", secret_name,
        "-n", namespace,
        "--from-literal=PRIVY_APP_ID=test-app-id",
        "--from-literal=PRIVY_APP_SECRET=test-app-secret",
        f"--from-literal=AWS_ACCESS_KEY_ID={minio_user}",
        f"--from-literal=AWS_SECRET_ACCESS_KEY={minio_password}",
    ]
    if chain_signer_key:
        cmd.append(f"--from-literal=HYP_DEFAULTSIGNER_KEY={chain_signer_key}")
    cmd.extend(["--dry-run=client", "-o", "yaml"])
    gen = run_cmd(cmd)
    run_cmd(["kubectl", "apply", "-f", "-"], input_text=gen.stdout)
    log_info(f"{secret_name} created")


def create_relayer_secrets(
    namespace: str,
    gorchain_signer_key: str,
    solana_signer_key: str,
    minio_user: str,
    minio_password: str,
    relayer_keypair_json: str,
) -> None:
    """Create the hyperlane-relayer-secrets k8s Secret (idempotent)."""
    secret_name = "hyperlane-relayer-secrets"
    log_info(f"Creating {secret_name} in namespace {namespace}...")
    gen = run_cmd(
        [
            "kubectl", "create", "secret", "generic", secret_name,
            "-n", namespace,
            f"--from-literal=HYP_CHAINS_GORCHAIN_SIGNER_KEY={gorchain_signer_key}",
            f"--from-literal=HYP_CHAINS_SOLANA_SIGNER_KEY={solana_signer_key}",
            f"--from-literal=AWS_ACCESS_KEY_ID={minio_user}",
            f"--from-literal=AWS_SECRET_ACCESS_KEY={minio_password}",
            f"--from-literal=RELAYER_KEYPAIR_JSON={relayer_keypair_json}",
            "--dry-run=client", "-o", "yaml",
        ]
    )
    run_cmd(["kubectl", "apply", "-f", "-"], input_text=gen.stdout)
    log_info(f"{secret_name} created")


def create_warp_deployer_secrets(namespace: str, keypair_set: KeypairSet) -> None:
    log_info(f"Creating warp deployer secrets in namespace {namespace}...")

    gen = run_cmd(
        [
            "kubectl",
            "create",
            "secret",
            "generic",
            "hyperlane-warp-deployer-secrets",
            "-n",
            namespace,
            f"--from-literal=DEPLOYER_KEYPAIR={keypair_set.deployer_keypair}",
            f"--from-literal=HARDWARE_WALLET_PUBKEY={keypair_set.hardware_wallet_pubkey}",
            "--dry-run=client",
            "-o",
            "yaml",
        ]
    )

    run_cmd(["kubectl", "apply", "-f", "-"], input_text=gen.stdout)
    log_info("Warp deployer secrets created")
