"""Mock Privy server for e2e validator tests.

Implements the Privy wallet RPC endpoint that the KMS proxy calls:
  POST /v1/wallets/{wallet_id}/rpc  (method: secp256k1_sign)

Each wallet_id maps to a different secp256k1 private key, mirroring
production where each validator chain has its own Privy wallet.

Usage:
    server = PrivyMockServer(wallet_keys, port=19876)
    server.start()   # starts in a background thread
    ...
    server.stop()
"""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from eth_keys import keys as eth_keys

log = logging.getLogger(__name__)

# Default wallet IDs used in test fixtures
GORCHAIN_WALLET_ID = "wallet-gorchain-001"
SOLANA_WALLET_ID = "wallet-solana-001"


def generate_wallet_keys() -> dict[str, str]:
    """Generate fresh secp256k1 keys for each test wallet.

    Returns dict of wallet_id -> private_key_hex (without 0x prefix).
    """
    keys: dict[str, str] = {}
    for wallet_id in (GORCHAIN_WALLET_ID, SOLANA_WALLET_ID):
        pk = eth_keys.PrivateKey(os.urandom(32))
        keys[wallet_id] = pk.to_bytes().hex()
    return keys


def derive_h160_address(private_key_hex: str) -> str:
    """Derive the H160 (Ethereum) address from a secp256k1 private key."""
    pk = eth_keys.PrivateKey(bytes.fromhex(private_key_hex))
    return pk.public_key.to_checksum_address()


def _sign_digest(private_key_hex: str, digest: bytes) -> str:
    """Sign a raw 32-byte digest. Returns 0x-prefixed 65-byte hex (r||s||v)."""
    pk = eth_keys.PrivateKey(bytes.fromhex(private_key_hex))
    sig = pk.sign_msg_hash(digest)
    r_bytes = sig.r.to_bytes(32, "big")
    s_bytes = sig.s.to_bytes(32, "big")
    v_byte = sig.v.to_bytes(1, "big")
    return "0x" + (r_bytes + s_bytes + v_byte).hex()


class _PrivyHandler(BaseHTTPRequestHandler):
    """HTTP handler for the mock Privy server."""

    wallet_keys: dict[str, str]  # Set by PrivyMockServer

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("privy-mock: %s", format % args)

    def do_POST(self) -> None:
        # Parse /v1/wallets/{wallet_id}/rpc
        parts = self.path.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "v1" or parts[1] != "wallets" or parts[3] != "rpc":
            self._error(404, f"unknown path: {self.path}")
            return

        wallet_id = parts[2]
        private_key_hex = self.wallet_keys.get(wallet_id)
        if private_key_hex is None:
            self._error(404, f"unknown wallet: {wallet_id}")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))

        method = body.get("method", "")
        if method != "secp256k1_sign":
            self._error(400, f"unsupported method: {method}")
            return

        hash_hex = body.get("params", {}).get("hash", "")
        if hash_hex.startswith("0x"):
            hash_hex = hash_hex[2:]

        digest = bytes.fromhex(hash_hex)
        sig_hex = _sign_digest(private_key_hex, digest)

        response = {
            "method": "secp256k1_sign",
            "data": {
                "signature": sig_hex,
                "encoding": "hex",
            },
        }

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self._error(404, "not found")

    def _error(self, status: int, msg: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode())


class PrivyMockServer:
    """Mock Privy server that runs in a background thread."""

    def __init__(self, wallet_keys: dict[str, str], port: int = 19876):
        self.wallet_keys = wallet_keys
        self.port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler_class = type(
            "_BoundPrivyHandler",
            (_PrivyHandler,),
            {"wallet_keys": self.wallet_keys},
        )
        self._server = HTTPServer(("0.0.0.0", self.port), handler_class)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info(
            "Mock Privy server started on :%d with %d wallets",
            self.port, len(self.wallet_keys),
        )

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("Mock Privy server stopped")
