"""Mock Privy server for e2e tests.

Implements the Privy wallet RPC endpoint:
  POST /v1/wallets/{wallet_id}/rpc
  GET  /v1/wallets/{wallet_id}

Supported RPC methods:
  - secp256k1_sign: used by KMS proxy for validator signing (secp256k1)
  - signTransaction: used by gas oracle for Solana tx signing (Ed25519)

Each wallet_id maps to a cryptographic key:
  - Validator wallets → secp256k1 private keys
  - Oracle wallet → Solana Ed25519 keypair (signs full transactions)

Usage (in-process):
    server = PrivyMockServer(wallet_keys, port=19876)
    server.start()   # starts in a background thread
    ...
    server.stop()

Usage (standalone subprocess — survives pytest exit):
    python -m lib.privy_mock --keys-dir .keys/ --port 19876
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eth_keys import keys as eth_keys
from solders.keypair import Keypair as SoldersKeypair
from solders.transaction import Transaction as SoldersTransaction

if TYPE_CHECKING:
    from lib.keygen import KeypairSet

log = logging.getLogger(__name__)

# Default wallet IDs used in test fixtures
GORCHAIN_WALLET_ID = "wallet-gorchain-001"
SOLANA_WALLET_ID = "wallet-solana-001"
ORACLE_WALLET_ID = "wallet-oracle-001"


def generate_wallet_keys(keypair_set: KeypairSet) -> dict[str, str]:
    """Map test wallet IDs to the secp256k1 private keys from the keypair set.

    Uses the same keys that were passed to the deployer as validator addresses,
    so the ISM validator set matches the keys the Privy mock signs with.

    Returns dict of wallet_id -> private_key_hex (without 0x prefix).
    """
    return {
        GORCHAIN_WALLET_ID: keypair_set.gorchain_validator_private_key,
        SOLANA_WALLET_ID: keypair_set.solana_validator_private_key,
    }


def load_oracle_keypair(keys_dir: str | Path) -> tuple[str, list[int]]:
    """Load the IGP oracle Solana keypair for the mock.

    Returns (base58_address, keypair_bytes_list).
    """
    oracle_path = Path(keys_dir) / "igp-oracle.json"
    keypair_bytes = json.loads(oracle_path.read_text())
    kp = SoldersKeypair.from_bytes(bytes(keypair_bytes))
    return str(kp.pubkey()), keypair_bytes


def derive_h160_address(private_key_hex: str) -> str:
    """Derive the H160 (Ethereum) address from a secp256k1 private key."""
    pk = eth_keys.PrivateKey(bytes.fromhex(private_key_hex))
    return pk.public_key.to_checksum_address()


def is_privy_mock_running(port: int = 19876) -> bool:
    """Check if privy-mock is already running on the given port."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        return resp.status == 200
    except Exception:
        return False


def load_wallet_keys_from_dir(keys_dir: str | Path) -> dict[str, str]:
    """Load wallet keys from persisted keypair files in the keys directory.

    Reads gorchain-validator.json and solana-validator.json (cast wallet new
    format) and maps wallet IDs to private keys.
    """
    keys_dir = Path(keys_dir)
    gorchain_data = json.loads((keys_dir / "gorchain-validator.json").read_text())
    solana_data = json.loads((keys_dir / "solana-validator.json").read_text())
    # cast wallet new --json may return a list
    if isinstance(gorchain_data, list):
        gorchain_data = gorchain_data[0]
    if isinstance(solana_data, list):
        solana_data = solana_data[0]
    return {
        GORCHAIN_WALLET_ID: gorchain_data["private_key"].removeprefix("0x"),
        SOLANA_WALLET_ID: solana_data["private_key"].removeprefix("0x"),
    }


def _sign_digest(private_key_hex: str, digest: bytes) -> str:
    """Sign a raw 32-byte digest. Returns 0x-prefixed 65-byte hex (r||s||v)."""
    pk = eth_keys.PrivateKey(bytes.fromhex(private_key_hex))
    sig = pk.sign_msg_hash(digest)
    r_bytes = sig.r.to_bytes(32, "big")
    s_bytes = sig.s.to_bytes(32, "big")
    v_byte = sig.v.to_bytes(1, "big")
    return "0x" + (r_bytes + s_bytes + v_byte).hex()


def _sign_solana_transaction(keypair_bytes: list[int], tx_base64: str) -> str:
    """Sign a Solana transaction with an Ed25519 keypair.

    Deserializes the unsigned transaction, signs it with the keypair,
    and returns the signed transaction as base64.
    """
    kp = SoldersKeypair.from_bytes(bytes(keypair_bytes))
    tx_bytes = base64.b64decode(tx_base64)
    tx = SoldersTransaction.from_bytes(tx_bytes)

    # Sign the serialized message and reconstruct via populate()
    msg = tx.message
    sig = kp.sign_message(bytes(msg))
    signed_tx = SoldersTransaction.populate(msg, [sig])
    return base64.b64encode(bytes(signed_tx)).decode()


class _PrivyHandler(BaseHTTPRequestHandler):
    """HTTP handler for the mock Privy server."""

    wallet_keys: dict[str, str]  # Set by PrivyMockServer
    oracle_keypair: list[int] | None  # Set by PrivyMockServer
    oracle_address: str | None  # Set by PrivyMockServer

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("privy-mock: %s", format % args)

    def do_POST(self) -> None:
        # Parse /v1/wallets/{wallet_id}/rpc
        parts = self.path.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "v1" or parts[1] != "wallets" or parts[3] != "rpc":
            self._error(404, f"unknown path: {self.path}")
            return

        wallet_id = parts[2]

        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        method = body.get("method", "")

        if method == "secp256k1_sign":
            self._handle_secp256k1_sign(wallet_id, body)
        elif method == "signTransaction":
            self._handle_sign_transaction(wallet_id, body)
        else:
            self._error(400, f"unsupported method: {method}")

    def _handle_secp256k1_sign(self, wallet_id: str, body: dict) -> None:
        private_key_hex = self.wallet_keys.get(wallet_id)
        if private_key_hex is None:
            self._error(404, f"unknown wallet: {wallet_id}")
            return

        params = body.get("params", {})
        if self._reject_unknown_params(params, {"hash"}):
            return

        hash_hex = params.get("hash", "")
        if hash_hex.startswith("0x"):
            hash_hex = hash_hex[2:]

        digest = bytes.fromhex(hash_hex)
        sig_hex = _sign_digest(private_key_hex, digest)

        self._json_response({
            "method": "secp256k1_sign",
            "data": {
                "signature": sig_hex,
                "encoding": "hex",
            },
        })

    def _handle_sign_transaction(self, wallet_id: str, body: dict) -> None:
        if wallet_id != ORACLE_WALLET_ID or self.oracle_keypair is None:
            self._error(404, f"unknown oracle wallet: {wallet_id}")
            return

        params = body.get("params", {})
        if self._reject_unknown_params(params, {"transaction", "encoding"}):
            return

        tx_base64 = params.get("transaction", "")
        if not tx_base64:
            self._error(400, "missing params.transaction")
            return

        try:
            signed_base64 = _sign_solana_transaction(self.oracle_keypair, tx_base64)
        except Exception as e:
            self._error(500, f"signing failed: {e}")
            return

        self._json_response({
            "method": "signTransaction",
            "data": {
                "signed_transaction": signed_base64,
                "encoding": "base64",
            },
        })

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        # GET /v1/wallets/{wallet_id} — return wallet info
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "v1" and parts[1] == "wallets":
            wallet_id = parts[2]
            if wallet_id == ORACLE_WALLET_ID and self.oracle_address:
                self._json_response({
                    "id": wallet_id,
                    "address": self.oracle_address,
                    "chain_type": "solana",
                })
            elif wallet_id in self.wallet_keys:
                # Validator wallets — derive Ethereum address
                address = derive_h160_address(self.wallet_keys[wallet_id])
                self._json_response({
                    "id": wallet_id,
                    "address": address,
                    "chain_type": "ethereum",
                })
            else:
                self._error(404, f"unknown wallet: {wallet_id}")
            return

        self._error(404, "not found")

    def _reject_unknown_params(self, params: dict, allowed: set[str]) -> bool:
        """Reject params with keys outside `allowed`, mirroring real Privy's
        strict schema. Returns True (and sends a 400) if extras were found.

        Real Privy rejects unrecognized params keys with invalid_data — e.g.
        secp256k1_sign accepts only 'hash'. Enforcing this here stops a client
        from passing schema validation against the mock but failing live Privy.
        """
        extra = sorted(k for k in params if k not in allowed)
        if extra:
            keys = ", ".join(f"'{k}'" for k in extra)
            self._error(
                400,
                f"[Input error] `params`: Unrecognized key(s) in object: {keys}",
                code="invalid_data",
            )
            return True
        return False

    def _json_response(self, data: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _error(self, status: int, msg: str, code: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        payload: dict[str, str] = {"error": msg}
        if code:
            payload["code"] = code
        self.wfile.write(json.dumps(payload).encode())


class PrivyMockServer:
    """Mock Privy server that runs in a background thread."""

    def __init__(
        self,
        wallet_keys: dict[str, str],
        port: int = 19876,
        oracle_keypair: list[int] | None = None,
        oracle_address: str | None = None,
    ):
        self.wallet_keys = wallet_keys
        self.port = port
        self.oracle_keypair = oracle_keypair
        self.oracle_address = oracle_address
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _make_handler(self) -> type:
        return type(
            "_BoundPrivyHandler",
            (_PrivyHandler,),
            {
                "wallet_keys": self.wallet_keys,
                "oracle_keypair": self.oracle_keypair,
                "oracle_address": self.oracle_address,
            },
        )

    def start(self) -> None:
        handler_class = self._make_handler()
        self._server = HTTPServer(("0.0.0.0", self.port), handler_class)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info(
            "Mock Privy server started on :%d with %d wallets (oracle: %s)",
            self.port, len(self.wallet_keys),
            self.oracle_address or "none",
        )

    def run_forever(self) -> None:
        """Run the server in the foreground (blocking). For standalone use."""
        handler_class = self._make_handler()
        self._server = HTTPServer(("0.0.0.0", self.port), handler_class)
        log.info(
            "Mock Privy server listening on :%d with %d wallets (oracle: %s, foreground)",
            self.port, len(self.wallet_keys),
            self.oracle_address or "none",
        )
        self._server.serve_forever()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("Mock Privy server stopped")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Standalone mock Privy server")
    parser.add_argument("--keys-dir", required=True, help="Path to .keys/ directory")
    parser.add_argument("--port", type=int, default=19876)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    wallet_keys = load_wallet_keys_from_dir(args.keys_dir)

    # Load oracle keypair if available
    oracle_address = None
    oracle_keypair = None
    oracle_path = Path(args.keys_dir) / "igp-oracle.json"
    if oracle_path.is_file():
        oracle_address, oracle_keypair = load_oracle_keypair(args.keys_dir)
        log.info("Oracle wallet loaded: %s -> %s", ORACLE_WALLET_ID, oracle_address)

    server = PrivyMockServer(
        wallet_keys, port=args.port,
        oracle_keypair=oracle_keypair, oracle_address=oracle_address,
    )
    server.run_forever()
