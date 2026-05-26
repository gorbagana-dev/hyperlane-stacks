import base64
from pathlib import Path

import yaml

from lib.cluster import write_caddy_cert_backup


def test_write_caddy_cert_backup_writes_one_secret_per_hostname(tmp_path: Path):
    cert = tmp_path / "test.crt"
    key = tmp_path / "test.key"
    cert.write_bytes(b"CERT-CONTENT")
    key.write_bytes(b"KEY-CONTENT")
    out = tmp_path / "out" / "caddy-secrets.yaml"

    write_caddy_cert_backup(out, cert, key, ["a.test", "b.test"])

    assert out.is_file()
    doc = yaml.safe_load(out.read_text())
    assert doc["kind"] == "List"
    items = doc["items"]
    # 3 secrets per hostname (crt, key, json) × 2 hostnames = 6
    assert len(items) == 6

    prefix = "caddy.ingress--certificates.acme-v02.api.letsencrypt.org-directory"
    issuer = "certificates/acme-v02.api.letsencrypt.org-directory"
    by_name = {d["metadata"]["name"]: d for d in items}

    for host in ("a.test", "b.test"):
        for ext in ("crt", "key", "json"):
            name = f"{prefix}.{host}.{host}.{ext}"
            assert name in by_name, f"missing secret {name}"
            d = by_name[name]
            assert d["metadata"]["namespace"] == "caddy-system"
            assert d["metadata"]["labels"] == {"manager": "caddy"}
            assert d["metadata"]["annotations"]["certmagic.io/storage-key"] == (
                f"{issuer}/{host}/{host}.{ext}"
            )
            assert d["type"] == "Opaque"
            assert set(d["data"].keys()) == {"value"}

    assert base64.b64decode(by_name[f"{prefix}.a.test.a.test.crt"]["data"]["value"]) == b"CERT-CONTENT"
    assert base64.b64decode(by_name[f"{prefix}.a.test.a.test.key"]["data"]["value"]) == b"KEY-CONTENT"
    assert base64.b64decode(by_name[f"{prefix}.a.test.a.test.json"]["data"]["value"]) == b"{}"


def test_write_caddy_cert_backup_creates_parent_dirs(tmp_path: Path):
    cert = tmp_path / "c.crt"
    key = tmp_path / "c.key"
    cert.write_bytes(b"x")
    key.write_bytes(b"y")
    out = tmp_path / "deep" / "nested" / "caddy-secrets.yaml"

    write_caddy_cert_backup(out, cert, key, ["only.test"])

    assert out.is_file()


from lib.state_loader import write_mkcert_root_to_configmap


def test_write_mkcert_root_to_configmap_copies_cert(tmp_path: Path, monkeypatch):
    """Helper copies CAROOT/rootCA.pem into {deploy_dir}/configmaps/minio-ca-config/."""
    fake_caroot = tmp_path / "fake-caroot"
    fake_caroot.mkdir()
    (fake_caroot / "rootCA.pem").write_bytes(b"--MKCERT-CA--")
    monkeypatch.setenv("CAROOT", str(fake_caroot))

    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    write_mkcert_root_to_configmap(deploy_dir)

    dest = deploy_dir / "configmaps" / "minio-ca-config" / "rootCA.pem"
    assert dest.is_file()
    assert dest.read_bytes() == b"--MKCERT-CA--"


def test_write_mkcert_root_to_configmap_creates_parent_dir(tmp_path: Path, monkeypatch):
    """Helper creates configmaps/minio-ca-config/ if it doesn't exist yet."""
    fake_caroot = tmp_path / "fake-caroot"
    fake_caroot.mkdir()
    (fake_caroot / "rootCA.pem").write_bytes(b"X")
    monkeypatch.setenv("CAROOT", str(fake_caroot))

    deploy_dir = tmp_path / "fresh-deploy"
    deploy_dir.mkdir()

    write_mkcert_root_to_configmap(deploy_dir)

    assert (deploy_dir / "configmaps" / "minio-ca-config").is_dir()


def test_write_mkcert_root_to_configmap_raises_when_caroot_missing(tmp_path: Path, monkeypatch):
    """Helper raises FileNotFoundError if mkcert root is absent."""
    monkeypatch.setenv("CAROOT", str(tmp_path / "nope"))
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    import pytest as _pytest
    with _pytest.raises(FileNotFoundError):
        write_mkcert_root_to_configmap(deploy_dir)
