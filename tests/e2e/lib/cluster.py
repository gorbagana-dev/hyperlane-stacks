"""Kind cluster lifecycle for Hyperlane e2e tests."""

from __future__ import annotations

import time
from pathlib import Path

from .common import E2E_DIR, fail_exit, log_info, run_cmd

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


def create_kind_cluster(config_path: Path | None = None) -> None:
    log_info(f"Creating kind cluster '{KIND_CLUSTER_NAME}'...")

    result = run_cmd(["kind", "get", "clusters"], check=False, quiet=True)
    if result.returncode == 0:
        existing = result.stdout.strip().splitlines()
        if KIND_CLUSTER_NAME in existing:
            log_info(f"Kind cluster '{KIND_CLUSTER_NAME}' already exists, reusing")
            return

    if config_path is None:
        config_path = E2E_DIR / "fixtures" / "kind-config.yaml"

    run_cmd(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            KIND_CLUSTER_NAME,
            "--config",
            str(config_path),
            "--wait",
            "120s",
        ]
    )
    log_info(f"Kind cluster '{KIND_CLUSTER_NAME}' created")

    run_cmd(
        [
            "kubectl",
            "wait",
            "--for=condition=Ready",
            "pods",
            "--all",
            "-n",
            "kube-system",
            "--timeout=120s",
        ]
    )
    log_info("Kind cluster system pods are ready")


def install_cert_manager() -> None:
    log_info("Installing cert-manager...")

    run_cmd(
        [
            "kubectl",
            "apply",
            "-f",
            "https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml",
        ]
    )

    log_info("Waiting for cert-manager pods to be ready...")
    for deployment in ("cert-manager", "cert-manager-webhook", "cert-manager-cainjector"):
        run_cmd(
            [
                "kubectl",
                "wait",
                "--for=condition=Available",
                f"deployment/{deployment}",
                "-n",
                "cert-manager",
                "--timeout=120s",
            ]
        )

    # Give webhooks a moment to register
    time.sleep(5)
    log_info("cert-manager is ready")


def create_selfsigned_issuer(fixture_path: Path | None = None) -> None:
    log_info("Creating self-signed ClusterIssuer...")
    if fixture_path is None:
        fixture_path = E2E_DIR / "fixtures" / "cert-manager-issuer.yaml"
    run_cmd(["kubectl", "apply", "-f", str(fixture_path)])
    log_info("Self-signed ClusterIssuer created")


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


def install_ingress_nginx() -> None:
    """Install the nginx ingress controller for Kind clusters.

    Uses the Kind-specific manifest from the ingress-nginx project which
    includes hostNetwork and NodePort configuration that works with Kind's
    extraPortMappings (ports 80/443 mapped to the host).
    """
    log_info("Installing nginx ingress controller for Kind...")
    run_cmd([
        "kubectl", "apply", "-f",
        "https://raw.githubusercontent.com/kubernetes/ingress-nginx/"
        "controller-v1.12.0/deploy/static/provider/kind/deploy.yaml",
    ])

    log_info("Waiting for nginx ingress controller to be ready...")
    run_cmd([
        "kubectl", "wait", "--namespace", "ingress-nginx",
        "--for=condition=Ready", "pod",
        "-l", "app.kubernetes.io/component=controller",
        "--timeout=120s",
    ])

    log_info("nginx ingress controller is ready")


def create_namespace(namespace: str) -> None:
    log_info(f"Creating namespace {namespace}...")
    result = run_cmd(["kubectl", "create", "namespace", namespace], check=False)
    if result.returncode == 0:
        log_info(f"Namespace {namespace} created")
    elif "AlreadyExists" in (result.stderr or ""):
        log_info(f"Namespace {namespace} already exists")
    else:
        from .common import fail_exit
        fail_exit(f"Failed to create namespace {namespace}: {result.stderr}")


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


import base64

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
    """
    cert_b64 = base64.b64encode(cert_path.read_bytes()).decode("ascii")
    key_b64 = base64.b64encode(key_path.read_bytes()).decode("ascii")

    docs = []
    for host in hostnames:
        docs.append({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": f"{CADDY_SECRET_PREFIX}--{host}",
                "namespace": CADDY_NAMESPACE,
            },
            "type": "Opaque",
            "data": {"tls.crt": cert_b64, "tls.key": key_b64},
        })

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    import yaml as _yaml
    backup_path.write_text(_yaml.safe_dump_all(docs))


def ensure_mkcert_installed() -> None:
    """Run `mkcert -install` idempotently. Skip if CAROOT already contains a
    rootCA. Raise with a pointer to README if mkcert isn't on PATH.
    """
    check = run_cmd(["which", "mkcert"], check=False, quiet=True)
    if check.returncode != 0:
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
