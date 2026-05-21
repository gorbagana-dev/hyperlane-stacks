import base64
from pathlib import Path

import pytest
import yaml

from tests.e2e.lib.cluster import write_caddy_cert_backup


def test_write_caddy_cert_backup_writes_one_secret_per_hostname(tmp_path: Path):
    cert = tmp_path / "test.crt"
    key = tmp_path / "test.key"
    cert.write_bytes(b"CERT-CONTENT")
    key.write_bytes(b"KEY-CONTENT")
    out = tmp_path / "out" / "caddy-secrets.yaml"

    write_caddy_cert_backup(out, cert, key, ["a.test", "b.test"])

    assert out.is_file()
    docs = list(yaml.safe_load_all(out.read_text()))
    docs = [d for d in docs if d]  # drop trailing None from --- separators
    assert len(docs) == 2
    names = {d["metadata"]["name"] for d in docs}
    assert names == {
        "caddy.ingress--certificates.acme-v02.api.letsencrypt.org-directory--a.test",
        "caddy.ingress--certificates.acme-v02.api.letsencrypt.org-directory--b.test",
    }
    for d in docs:
        assert d["metadata"]["namespace"] == "caddy-system"
        assert d["type"] == "Opaque"
        assert base64.b64decode(d["data"]["tls.crt"]) == b"CERT-CONTENT"
        assert base64.b64decode(d["data"]["tls.key"]) == b"KEY-CONTENT"


def test_write_caddy_cert_backup_creates_parent_dirs(tmp_path: Path):
    cert = tmp_path / "c.crt"
    key = tmp_path / "c.key"
    cert.write_bytes(b"x")
    key.write_bytes(b"y")
    out = tmp_path / "deep" / "nested" / "caddy-secrets.yaml"

    write_caddy_cert_backup(out, cert, key, ["only.test"])

    assert out.is_file()
