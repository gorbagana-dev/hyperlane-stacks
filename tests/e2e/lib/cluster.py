"""Kind cluster lifecycle for Hyperlane e2e tests."""

from __future__ import annotations

import base64
from pathlib import Path

from .common import fail_exit, log_info, run_cmd

KIND_CLUSTER_NAME = "hyperlane"
TEST_HOSTNAMES: tuple[str, ...] = (
    "bridge.test",
    "grafana.test",
    "prometheus.test",
    "validator-gorchain.test",
    "validator-solana.test",
    "relayer.test",
    "minio-console.test",
)


def ensure_hosts_entry(hostname: str, ip: str = "127.0.0.1") -> None:
    """Ensure a /etc/hosts entry exists for the given hostname.

    Idempotent — skips if the entry already exists.
    Uses sudo since /etc/hosts requires root access.
    """
    import re as _re

    hosts_path = "/etc/hosts"
    with open(hosts_path) as f:
        content = f.read()

    # Check if the exact hostname is already mapped
    pattern = rf"^\s*\S+\s+.*\b{_re.escape(hostname)}\b"
    if _re.search(pattern, content, _re.MULTILINE):
        log_info(f"/etc/hosts already has entry for {hostname}")
        return

    log_info(f"Adding {hostname} -> {ip} to /etc/hosts...")
    entry = f"{ip} {hostname}\n"
    run_cmd(["sudo", "tee", "-a", hosts_path], input_text=entry, quiet=True)
    log_info(f"Added /etc/hosts entry: {ip} {hostname}")


def ensure_kind_network() -> None:
    """Ensure the `kind` Docker network exists. SO's later `kind create cluster`
    reuses a pre-existing network with this name, so we can pre-create it here
    to populate REPLACE_HOST_IP before any deployer runs.

    Idempotent — silent skip if already present.
    """
    probe = run_cmd(
        ["docker", "network", "inspect", "kind"],
        check=False, quiet=True,
    )
    if probe.returncode == 0:
        log_info("Docker network 'kind' already exists")
        return

    log_info("Creating Docker network 'kind' (kind will reuse it)")
    run_cmd(["docker", "network", "create", "kind"])


def get_host_ip() -> str:
    """Detect the Kind network gateway IP (host IP from inside the cluster)."""
    log_info("Detecting host IP for kind network...")
    result = run_cmd(
        [
            "docker",
            "network",
            "inspect",
            "kind",
            "-f",
            '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}}\n{{end}}{{end}}',
        ]
    )
    # Pick the first IPv4 address (skip IPv6 lines containing ':')
    host_ip = ""
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line and ":" not in line:
            host_ip = line
            break

    if not host_ip:
        fail_exit(f"Could not detect IPv4 host IP from kind network. Raw output: {result.stdout.strip()}")

    log_info(f"Host IP: {host_ip}")
    return host_ip


def destroy_kind_cluster() -> None:
    log_info(f"Destroying kind cluster '{KIND_CLUSTER_NAME}'...")
    run_cmd(
        ["kind", "delete", "cluster", "--name", KIND_CLUSTER_NAME],
        check=False,
    )
    log_info("Kind cluster destroyed")


CADDY_SECRET_PREFIX = (
    "caddy.ingress--certificates.acme-v02.api.letsencrypt.org-directory"
)
CADDY_NAMESPACE = "caddy-system"


def write_caddy_cert_backup(
    backup_path: Path, cert_path: Path, key_path: Path, hostnames: list[str]
) -> None:
    """Render caddy-secrets.yaml with one k8s Secret per hostname referencing
    the same cert+key, formatted for SO's _restore_caddy_certs to load before
    Caddy starts.

    SO's _restore_caddy_certs reads this with yaml.safe_load and pulls items
    from a kind: List wrapper (matches the cert-backup CronJob's
    `kubectl get -o yaml` output). Multi-document YAML is silently ignored.
    """
    cert_b64 = base64.b64encode(cert_path.read_bytes()).decode("ascii")
    key_b64 = base64.b64encode(key_path.read_bytes()).decode("ascii")

    items = [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": f"{CADDY_SECRET_PREFIX}--{host}",
                "namespace": CADDY_NAMESPACE,
            },
            "type": "Opaque",
            "data": {"tls.crt": cert_b64, "tls.key": key_b64},
        }
        for host in hostnames
    ]

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    import yaml as _yaml
    backup_path.write_text(
        _yaml.safe_dump({"apiVersion": "v1", "kind": "List", "items": items})
    )


def ensure_mkcert_installed() -> None:
    """Run `mkcert -install` idempotently. Skip if CAROOT already contains a
    rootCA. Raise with a pointer to README if mkcert isn't on PATH.
    """
    probe = run_cmd(["which", "mkcert"], check=False, quiet=True)
    if probe.returncode != 0:
        fail_exit(
            "mkcert not found on PATH. See tests/e2e/README.md "
            "for one-time setup instructions."
        )

    caroot = run_cmd(["mkcert", "-CAROOT"], quiet=True).stdout.strip()
    if (Path(caroot) / "rootCA.pem").is_file():
        log_info(f"mkcert CA already installed at {caroot}")
        return

    log_info("Running `mkcert -install` (installs root CA into system trust store)")
    run_cmd(["mkcert", "-install"])


def ensure_mkcert_cert(
    cert_dir: Path, hostnames: list[str]
) -> tuple[Path, Path]:
    """Generate (or reuse) a multi-SAN mkcert cert covering hostnames.
    Returns (cert_path, key_path). Idempotent — regenerates if the existing
    cert's SANs don't cover the requested hostnames.
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert = cert_dir / "hyperlane.test.crt"
    key = cert_dir / "hyperlane.test.key"

    if cert.is_file() and key.is_file():
        # Check existing cert covers all requested SANs
        result = run_cmd(
            ["openssl", "x509", "-in", str(cert), "-noout", "-ext",
             "subjectAltName"],
            check=False, quiet=True,
        )
        if result.returncode == 0 and all(h in result.stdout for h in hostnames):
            log_info(f"Reusing existing mkcert cert at {cert}")
            return cert, key
        log_info("Existing cert SANs out of date; regenerating")

    log_info(f"Generating mkcert cert for {len(hostnames)} hostnames")
    run_cmd(
        ["mkcert",
         "-cert-file", str(cert),
         "-key-file", str(key),
         *hostnames],
    )
    return cert, key
