import base64
import dataclasses
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from pathlib import Path

import pytest

from lib.chain import (
    is_solana_validator_running,
    start_gorchain_stack,
    start_solana_test_validator,
    stop_gorchain_stack,
    stop_solana_test_validator,
)
from lib.cluster import (
    TEST_HOSTNAMES,
    destroy_kind_cluster,
    ensure_hosts_entry,
    ensure_kind_network,
    ensure_mkcert_cert,
    ensure_mkcert_installed,
    get_host_ip,
    write_caddy_cert_backup,
)
from lib.common import (
    CHAINS,
    E2E_DIR,
    force_rmtree,
    run_deployer_cli,
    save_job_describe,
    save_job_logs,
    save_pod_describe,
    save_pod_logs,
    wait_for_job_complete,
    wait_for_pod_phase,
    wait_for_rpc_accounts_ready,
    wait_for_rpc_health,
)
from lib.deploy import (
    AGENT_IMAGE,
    AGENT_IMAGE_LOCAL,
    DEPLOY_DIR,
    DEPLOYER_IMAGE,
    GAS_ORACLE_IMAGE,
    KMS_PROXY_IMAGE,
    KMS_PROXY_IMAGE_LOCAL,
    WARP_UI_IMAGE,
    WARP_UI_IMAGE_LOCAL,
    DeploymentInfo,
    build_agent_image,
    build_warp_ui_image,
    deploy_prepare,
    deploy_start,
    deployment_exists,
    ensure_ghcr_pat,
    get_deployment_id,
    prefetch_agent_images,
    prefetch_deployer_image,
    prefetch_gas_oracle_image,
    prefetch_minio_images,
    prefetch_monitoring_images,
    prefetch_warp_ui_image,
    stop_stack,
)
from lib.keygen import (
    KEYS_DIR,
    KeypairSet,
    _airdrop,
    fund_wallets,
    generate_chain_signer,
    generate_test_keypairs,
)
from lib.privy_mock import (
    GORCHAIN_WALLET_ID,
    ORACLE_WALLET_ID,
    SOLANA_WALLET_ID,
    derive_h160_address,
    generate_wallet_keys,
    is_privy_mock_running,
    load_oracle_keypair,
)
from lib.state_loader import BridgeStateLoader

log = logging.getLogger(__name__)

FIXTURE_SPEC = E2E_DIR / "fixtures" / "test-spec-deployer.yml"
WARP_SPEC = E2E_DIR / "fixtures" / "test-spec-warp-deployer.yml"
MINIO_SPEC = E2E_DIR / "fixtures" / "test-spec-minio.yml"
VALIDATOR_GORCHAIN_SPEC = E2E_DIR / "fixtures" / "test-spec-validator-gorchain.yml"
VALIDATOR_SOLANA_SPEC = E2E_DIR / "fixtures" / "test-spec-validator-solana.yml"
RELAYER_SPEC = E2E_DIR / "fixtures" / "test-spec-relayer.yml"
GAS_ORACLE_SPEC = E2E_DIR / "fixtures" / "test-spec-gas-oracle.yml"
MONITORING_SPEC = E2E_DIR / "fixtures" / "test-spec-monitoring.yml"
WARP_UI_SPEC = E2E_DIR / "fixtures" / "test-spec-warp-ui.yml"

PRIVY_MOCK_PORT = 19876

# Spec placeholder replacements shared across e2e test specs. Each stack uses
# its own namespace (derived by SO as laconic-{stack_name}) but all share the
# same kind cluster.
SPEC_REPLACEMENTS = {}

# Fixed host path bound to /mnt inside the kind node. Every spec declares
# `kind-mount-root: /tmp/hyperlane-bridge-e2e`; SO generates the kind-config
# from that value and validates live-cluster binds via check_mounts_compatible().
BRIDGE_STATE_ROOT = Path("/tmp/hyperlane-bridge-e2e")


def _resolve_image_refs(build_from_source: bool) -> dict[str, str]:
    """Return REPLACE_*_IMAGE placeholder values for the image-overrides: spec key.

    SO preloads these images into the kind cluster at every
    deploy_start --perform-cluster-management (filtered to host-Docker-available).
    build_from_source switches to :local tags for stacks that have a local build path.
    """
    if build_from_source:
        return {
            "REPLACE_DEPLOYER_IMAGE": DEPLOYER_IMAGE,
            "REPLACE_AGENT_IMAGE": AGENT_IMAGE_LOCAL,
            "REPLACE_KMS_PROXY_IMAGE": KMS_PROXY_IMAGE_LOCAL,
            "REPLACE_WARP_UI_IMAGE": WARP_UI_IMAGE_LOCAL,
            "REPLACE_GAS_ORACLE_IMAGE": GAS_ORACLE_IMAGE,
        }
    return {
        "REPLACE_DEPLOYER_IMAGE": DEPLOYER_IMAGE,
        "REPLACE_AGENT_IMAGE": AGENT_IMAGE,
        "REPLACE_KMS_PROXY_IMAGE": KMS_PROXY_IMAGE,
        "REPLACE_WARP_UI_IMAGE": WARP_UI_IMAGE,
        "REPLACE_GAS_ORACLE_IMAGE": GAS_ORACLE_IMAGE,
    }


@pytest.fixture(scope="session")
def bridge_state_root(request: pytest.FixtureRequest) -> Generator[Path, None, None]:
    """Kind umbrella root. SO emits a single extraMount (hostPath=this →
    containerPath=/mnt). All stack data volumes are subdirs of this root:
      bridge/generated/  — deployer output (program-ids.json, etc.)
      bridge/logs/       — deployer job logs
      minio/data/        — MinIO object store
      validator-gorchain/data/
      validator-solana/data/
      relayer/data/
      monitoring/prometheus/
      monitoring/grafana/

    Lifecycle is paired with the kind cluster: removed at session teardown
    unless --skip-cleanup or --skip-cluster-setup is set, so a kept cluster
    keeps its state and a fresh cluster always starts with fresh state."""
    p = BRIDGE_STATE_ROOT
    p.mkdir(parents=True, exist_ok=True)
    for subdir in [
        "bridge/generated",
        "bridge/logs",
        "minio/data",
        "validator-gorchain/data",
        "validator-solana/data",
        "relayer/data",
        "monitoring/prometheus",
        "monitoring/grafana",
    ]:
        d = p / subdir
        d.mkdir(parents=True, exist_ok=True)
        # Tests: chmod 777 so any container UID can write (Prometheus runs as
        # nobody/65534, Grafana as UID 472).  World-writable is fine under /tmp/.
        # Prod: Ansible chowns each dir to the container's UID instead — see
        # docs/superpowers/specs/2026-05-27-host-path-volumes-design.md.
        d.chmod(0o777)
    log.info("Bridge state root for this session: %s", p)
    yield p
    skip_setup = request.config.getoption("--skip-cluster-setup")
    skip_cleanup = request.config.getoption("--skip-cleanup")
    if not skip_cleanup and not skip_setup:
        # Deployer containers run as root and write root-owned files into
        # this dir, so plain rmtree fails — force_rmtree falls back to sudo.
        log.info("Removing bridge state root: %s", p)
        force_rmtree(p)


@pytest.fixture(scope="session")
def bridge_state_dir(bridge_state_root: Path) -> Path:
    return bridge_state_root / "bridge" / "generated"


@pytest.fixture(scope="session")
def bridge_state_logs_dir(bridge_state_root: Path) -> Path:
    return bridge_state_root / "bridge" / "logs"


@pytest.fixture(scope="session")
def bridge_state_loader(bridge_state_dir: Path) -> BridgeStateLoader:
    return BridgeStateLoader(bridge_state_dir)


@dataclasses.dataclass
class MinioInfo:
    """Minio deployment info with credentials.

    user/password: MinIO root credentials (used for admin operations in tests).
    gorchain_key_id/gorchain_secret: IAM creds for the gorchain-primary validator.
    solana_key_id/solana_secret: IAM creds for the solana-primary validator.
    """
    deployment: DeploymentInfo
    user: str
    password: str
    gorchain_key_id: str
    gorchain_secret: str
    solana_key_id: str
    solana_secret: str

    # Delegate common fields for convenience
    @property
    def namespace(self) -> str:
        return self.deployment.namespace

    @property
    def deployment_id(self) -> str:
        return self.deployment.deployment_id

    @property
    def deploy_dir(self) -> Path:
        return self.deployment.deploy_dir


@dataclasses.dataclass
class ValidatorInfo:
    """Validator deployment info."""
    deployment: DeploymentInfo
    chain: str
    wallet_id: str

    @property
    def namespace(self) -> str:
        return self.deployment.namespace

    @property
    def deployment_id(self) -> str:
        return self.deployment.deployment_id

    @property
    def deploy_dir(self) -> Path:
        return self.deployment.deploy_dir


@dataclasses.dataclass
class RelayerInfo:
    """Relayer deployment info."""
    deployment: DeploymentInfo

    @property
    def namespace(self) -> str:
        return self.deployment.namespace

    @property
    def deployment_id(self) -> str:
        return self.deployment.deployment_id

    @property
    def deploy_dir(self) -> Path:
        return self.deployment.deploy_dir


# ---------------------------------------------------------------------------
# Custom CLI options
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--skip-cluster-setup", action="store_true", default=False, help="Skip kind cluster creation (reuse existing)"
    )
    parser.addoption(
        "--skip-chain-setup", action="store_true", default=False, help="Skip starting chain nodes (assume running)"
    )
    parser.addoption(
        "--build-from-source", action="store_true", default=False,
        help="Build container images from source instead of using published images"
    )
    parser.addoption("--skip-cleanup", action="store_true", default=False, help="Don't tear down after tests")
    parser.addoption(
        "--skip-core-deploy", action="store_true", default=False,
        help="Skip core deployer (reuse existing deployment from a previous --skip-cleanup run)"
    )
    parser.addoption(
        "--skip-warp-deploy", action="store_true", default=False,
        help="Skip warp deployer (reuse existing warp deployment from a previous --skip-cleanup run)"
    )
    parser.addoption(
        "--skip-minio-deploy", action="store_true", default=False,
        help="Skip minio deployment (reuse existing from a previous --skip-cleanup run)"
    )
    parser.addoption(
        "--skip-validator-deploy", action="store_true", default=False,
        help="Skip validator deployment (reuse existing from a previous --skip-cleanup run)"
    )
    parser.addoption(
        "--skip-relayer-deploy", action="store_true", default=False,
        help="Skip relayer deployment (reuse existing from a previous --skip-cleanup run)"
    )
    parser.addoption(
        "--skip-gas-oracle-deploy", action="store_true", default=False,
        help="Skip gas oracle deployment (reuse existing from a previous --skip-cleanup run)"
    )
    parser.addoption(
        "--skip-warp-ui-deploy", action="store_true", default=False,
        help="Skip warp-ui deployment (reuse existing from a previous --skip-cleanup run)"
    )
    parser.addoption(
        "--skip-monitoring-deploy", action="store_true", default=False,
        help="Skip monitoring deployment (reuse existing from a previous --skip-cleanup run)"
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Ensure GHCR_PAT is set before any fixtures run."""
    ensure_ghcr_pat()


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def host_prep(
    request: pytest.FixtureRequest,
    bridge_state_root: Path,
) -> Generator[None, None, None]:
    """Host-side prep: /etc/hosts entries + mkcert cert + Caddy cert-backup.
    Cluster creation happens via SO at the first `deploy_start
    --perform-cluster-management`.
    """
    SPEC_REPLACEMENTS["REPLACE_KIND_MOUNT_ROOT"] = str(bridge_state_root)
    skip_setup = request.config.getoption("--skip-cluster-setup")
    skip_cleanup = request.config.getoption("--skip-cleanup")

    if not skip_setup:
        log.info("Adding test hostnames to /etc/hosts...")
        for hostname in TEST_HOSTNAMES:
            ensure_hosts_entry(hostname)

        log.info("Ensuring mkcert is installed...")
        ensure_mkcert_installed()

        log.info("Generating mkcert cert covering test hostnames...")
        cert, key = ensure_mkcert_cert(
            bridge_state_root / "local-certs", list(TEST_HOSTNAMES)
        )

        log.info("Writing Caddy cert-backup for SO to pre-load...")
        write_caddy_cert_backup(
            bridge_state_root / "caddy-cert-backup" / "caddy-secrets.yaml",
            cert, key, list(TEST_HOSTNAMES),
        )
    else:
        log.info("Skipping host prep (--skip-cluster-setup)")

    ensure_kind_network()
    host_ip = get_host_ip()
    SPEC_REPLACEMENTS["REPLACE_HOST_IP"] = host_ip
    SPEC_REPLACEMENTS.update(_resolve_image_refs(request.config.getoption("--build-from-source")))

    yield

    if not skip_cleanup and not skip_setup:
        log.info("Destroying kind cluster...")
        destroy_kind_cluster()


@pytest.fixture(scope="session")
def chain_nodes(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    skip_setup = request.config.getoption("--skip-chain-setup")
    skip_cleanup = request.config.getoption("--skip-cleanup")

    gorchain_deploy_dir = E2E_DIR / ".deployments" / "gorchain"

    started_solana = False
    if not skip_setup:
        log.info("Starting Gorchain stack via laconic-so...")
        start_gorchain_stack(gorchain_deploy_dir)
        log.info("Starting Solana test validator (port 18899)...")
        start_solana_test_validator(port=18899, name="solana")
        started_solana = True
    else:
        log.info("Skipping chain setup (--skip-chain-setup)")
        if is_solana_validator_running(port=18899):
            log.info("Solana test validator already running on :18899")
        else:
            pytest.exit(
                "Solana test validator not running on :18899. "
                "Cannot use --skip-chain-setup without a running validator "
                "(it preserves state across pytest runs via start_new_session).",
                returncode=1,
            )
        # Gorchain may have restarted (crash, docker daemon restart, etc.).
        # /health returns OK during ledger replay; wait for accounts too.
        log.info("Waiting for gorchain RPC and accounts to be ready...")
        try:
            wait_for_rpc_health("http://localhost:8899", timeout=120)
            wait_for_rpc_accounts_ready("http://localhost:8899", timeout=120)
            log.info("Gorchain is ready")
        except TimeoutError as e:
            pytest.exit(
                f"Gorchain not ready on :8899 after 120s: {e}. "
                "Start gorchain before using --skip-chain-setup.",
                returncode=1,
            )

    yield

    if not skip_cleanup:
        if started_solana:
            log.info("Stopping Solana test validator...")
            stop_solana_test_validator(port=18899, name="solana")
        if not skip_setup:
            log.info("Stopping Gorchain stack...")
            stop_gorchain_stack(gorchain_deploy_dir)


@pytest.fixture(scope="session")
def deployer_image(request: pytest.FixtureRequest, host_prep: None) -> None:
    """Build or pre-fetch the deployer image to host Docker (SO preloads it via image-overrides at deploy_start)."""
    if request.config.getoption("--skip-core-deploy") and request.config.getoption("--skip-warp-deploy"):
        log.info("Skipping deployer image build (--skip-core-deploy + --skip-warp-deploy)")
        return
    if request.config.getoption("--build-from-source"):
        from lib.deploy import build_deployer_image
        log.info("Building deployer container image from source...")
        build_deployer_image()
    else:
        log.info("Pre-fetching published deployer image to host Docker...")
        prefetch_deployer_image()


@pytest.fixture(scope="session")
def validator_images(request: pytest.FixtureRequest, host_prep: None) -> None:
    """Build or pre-fetch agent + kms-proxy images to host Docker (SO preloads via image-overrides at deploy_start)."""
    if request.config.getoption("--skip-validator-deploy", default=False):
        log.info("Skipping validator image builds (--skip-validator-deploy)")
        return
    if request.config.getoption("--build-from-source"):
        log.info("Building patched agent and kms-proxy images via laconic-so...")
        build_agent_image()
    else:
        log.info("Pre-fetching published agent images to host Docker...")
        prefetch_agent_images()


@pytest.fixture(scope="session")
def gas_oracle_image(request: pytest.FixtureRequest, host_prep: None) -> None:
    """Pre-fetch the gas oracle image to host Docker (SO preloads it via image-overrides at deploy_start)."""
    if request.config.getoption("--skip-gas-oracle-deploy", default=False):
        log.info("Skipping gas oracle image prefetch (--skip-gas-oracle-deploy)")
        return
    log.info("Pre-fetching published gas oracle image to host Docker...")
    prefetch_gas_oracle_image()


@pytest.fixture(scope="session")
def monitoring_images(request: pytest.FixtureRequest, host_prep: None) -> None:
    """Pre-fetch monitoring stack images to host Docker (SO preloads them via image-overrides at deploy_start)."""
    if request.config.getoption("--skip-monitoring-deploy", default=False):
        log.info("Skipping monitoring image prefetch (--skip-monitoring-deploy)")
        return
    log.info("Pre-fetching monitoring images to host Docker...")
    prefetch_monitoring_images()


@pytest.fixture(scope="session")
def all_images(
    deployer_image: None,
    validator_images: None,
    warp_ui_image: None,
    gas_oracle_image: None,
    monitoring_images: None,
) -> None:
    """Build/fetch all container images up front before any deployment starts."""


@pytest.fixture(scope="session")
def keypairs() -> KeypairSet:
    log.info("Generating test keypairs...")
    return generate_test_keypairs()


# ---------------------------------------------------------------------------
# MinIO deployment
# ---------------------------------------------------------------------------


def _recover_minio_credentials(
    namespace: str,
) -> tuple[str, str, str, str, str, str]:
    """Read minio credentials from k8s secrets (for --skip-minio-deploy reuse).

    Returns: (root_user, root_password, gorchain_key_id, gorchain_secret,
               solana_key_id, solana_secret)

    Also re-exports all values to os.environ so subsequent SO deploy calls
    can find them as env vars.
    """
    def _read_secret(secret_name: str, field: str) -> str:
        result = subprocess.run(
            ["kubectl", "get", "secret", secret_name, "-n", namespace,
             "-o", f"jsonpath={{.data.{field}}}"],
            capture_output=True, text=True, check=True,
        )
        return base64.b64decode(result.stdout.strip()).decode()

    user = _read_secret("hyperlane-minio-secrets", "MINIO_ROOT_USER")
    password = _read_secret("hyperlane-minio-secrets", "MINIO_ROOT_PASSWORD")
    gorchain_key_id = _read_secret("minio-validator-secrets", "GORCHAIN_PRIMARY_KEY_ID")
    gorchain_secret = _read_secret("minio-validator-secrets", "GORCHAIN_PRIMARY_SECRET")
    solana_key_id = _read_secret("minio-validator-secrets", "SOLANA_PRIMARY_KEY_ID")
    solana_secret = _read_secret("minio-validator-secrets", "SOLANA_PRIMARY_SECRET")

    os.environ.update({
        "MINIO_ROOT_USER": user,
        "MINIO_ROOT_PASSWORD": password,
        "MINIO_USERS": "gorchain-primary,solana-primary",
        "GORCHAIN_PRIMARY_KEY_ID": gorchain_key_id,
        "GORCHAIN_PRIMARY_SECRET": gorchain_secret,
        "SOLANA_PRIMARY_KEY_ID": solana_key_id,
        "SOLANA_PRIMARY_SECRET": solana_secret,
    })

    log.info("Recovered minio credentials from k8s secrets")
    return user, password, gorchain_key_id, gorchain_secret, solana_key_id, solana_secret


@pytest.fixture(scope="session")
def minio_deployment(
    request: pytest.FixtureRequest,
    host_prep: None,
    bridge_state_loader: BridgeStateLoader,
) -> Generator[MinioInfo, None, None]:
    """Deploy the hyperlane-minio stack.

    Self-contained: only requires a Kind cluster. Creates its own namespace
    and secrets. Uses an independent deployment-id for unique resource names,
    with spec-level namespace override to share the e2e namespace.

    Generates per-validator IAM credentials for gorchain-primary and
    solana-primary. These are provisioned by the commands.py CronJob
    (minio-provision-initial) triggered during deploy start.
    """
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_minio = request.config.getoption("--skip-minio-deploy", default=False)

    if skip_minio:
        deploy_dir = DEPLOY_DIR / "hyperlane-minio"
        if deployment_exists(deploy_dir):
            deployment_id = get_deployment_id(deploy_dir)
            namespace = "laconic-hyperlane-minio"
            log.info("Reusing existing minio deployment (namespace: %s)", namespace)
            user, password, gorchain_key_id, gorchain_secret, solana_key_id, solana_secret = (
                _recover_minio_credentials(namespace)
            )
            yield MinioInfo(
                deployment=DeploymentInfo(
                    deploy_dir=deploy_dir, deployment_id=deployment_id, namespace=namespace
                ),
                user=user,
                password=password,
                gorchain_key_id=gorchain_key_id,
                gorchain_secret=gorchain_secret,
                solana_key_id=solana_key_id,
                solana_secret=solana_secret,
            )
            return
        log.info("--skip-minio-deploy set but %s missing — deploying fresh", deploy_dir)

    minio_user = f"minio-{secrets.token_hex(4)}"
    minio_password = secrets.token_hex(16)
    gorchain_key_id = f"gc-{secrets.token_hex(8)}"
    gorchain_secret = secrets.token_hex(24)
    solana_key_id = f"sol-{secrets.token_hex(8)}"
    solana_secret = secrets.token_hex(24)

    log.info("Pre-fetching MinIO images to host Docker...")
    prefetch_minio_images()

    log.info("Preparing minio stack...")
    deploy_info = deploy_prepare(
        "hyperlane-minio", MINIO_SPEC,
        spec_replacements=SPEC_REPLACEMENTS,
        deployment_id="minio",
    )
    namespace = deploy_info.namespace
    deployment_id = deploy_info.deployment_id

    bridge_state_loader.populate("hyperlane-minio", deploy_info.deploy_dir)

    os.environ.update({
        "MINIO_ROOT_USER":         minio_user,
        "MINIO_ROOT_PASSWORD":     minio_password,
        "MINIO_USERS":             "gorchain-primary,solana-primary",
        "GORCHAIN_PRIMARY_KEY_ID": gorchain_key_id,
        "GORCHAIN_PRIMARY_SECRET": gorchain_secret,
        "SOLANA_PRIMARY_KEY_ID":   solana_key_id,
        "SOLANA_PRIMARY_SECRET":   solana_secret,
    })

    log.info("Starting minio stack...")
    deploy_start(deploy_info.deploy_dir)

    try:
        log.info("Waiting for minio pod to be running...")
        wait_for_pod_phase(namespace, f"app={deployment_id}", "Running", timeout=120)

        log.info("Waiting for minio-provision-initial job to complete...")
        provision_job = "minio-provision-initial"
        wait_for_job_complete(namespace, provision_job, timeout=300)
        save_job_logs(namespace, provision_job)
        log.info("MinIO stack deployed and initialized")
    except Exception:
        save_job_logs(namespace, "minio-provision-initial")
        save_job_describe(namespace, "minio-provision-initial")
        save_pod_logs(namespace, f"app={deployment_id}", "minio")
        save_pod_describe(namespace, f"app={deployment_id}", "minio")
        raise

    yield MinioInfo(
        deployment=deploy_info,
        user=minio_user,
        password=minio_password,
        gorchain_key_id=gorchain_key_id,
        gorchain_secret=gorchain_secret,
        solana_key_id=solana_key_id,
        solana_secret=solana_secret,
    )

    save_pod_logs(namespace, f"app={deployment_id}", "minio")
    if not skip_cleanup:
        log.info("Stopping minio stack...")
        stop_stack("hyperlane-minio")


# ---------------------------------------------------------------------------
# Core deployer deployment
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def deployer_deployment(
    request: pytest.FixtureRequest,
    all_images: None,
    keypairs: KeypairSet,
    host_prep: None,
    chain_nodes: None,
    bridge_state_loader: BridgeStateLoader,
) -> Generator[DeploymentInfo, None, None]:
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_core_deploy = request.config.getoption("--skip-core-deploy")

    if skip_core_deploy:
        # Reuse existing deployment from a previous run (requires --skip-cleanup).
        # The Solana test validator runs detached (start_new_session) so it
        # survives Ctrl+C — deployed programs and funded wallets persist.
        deploy_dir = DEPLOY_DIR / "hyperlane-svm-deployer"
        if deployment_exists(deploy_dir):
            deployment_id = get_deployment_id(deploy_dir)
            namespace = "laconic-hyperlane-svm-deployer"
            log.info("Reusing existing core deployment (deployment-id: %s, namespace: %s)", deployment_id, namespace)
            yield DeploymentInfo(deploy_dir=deploy_dir, deployment_id=deployment_id, namespace=namespace)
            return
        log.info("--skip-core-deploy set but %s missing — deploying fresh", deploy_dir)

    log.info("Preparing deployer stack...")
    deploy_info = deploy_prepare(
        "hyperlane-svm-deployer", FIXTURE_SPEC,
        spec_replacements=SPEC_REPLACEMENTS,
        deployment_id="deployer",
    )
    namespace = deploy_info.namespace

    log.info("Funding wallets...")
    fund_wallets(keypair_set=keypairs, gorchain_rpc="http://localhost:8899", solana_rpc="http://localhost:18899")

    bridge_state_loader.populate("hyperlane-svm-deployer", deploy_info.deploy_dir)

    os.environ.update({
        "DEPLOYER_KEYPAIR":           keypairs.deployer_keypair,
        "HARDWARE_WALLET_PUBKEY":     keypairs.hardware_wallet_pubkey,
        "IGP_ORACLE_PUBKEY":          keypairs.igp_oracle_pubkey,
        "GORCHAIN_VALIDATOR_ADDRESS": keypairs.gorchain_validator_address,
        "SOLANA_VALIDATOR_ADDRESS":   keypairs.solana_validator_address,
    })

    log.info("Starting deployer stack...")
    deploy_start(deploy_info.deploy_dir)

    try:
        log.info("Waiting for deployer job to complete...")
        job_name = f"{deploy_info.deployment_id}-job-hyperlane-svm-deployer"
        wait_for_job_complete(namespace, job_name)
        save_job_logs(namespace, job_name)
        log.info("Core deployer job complete, artifacts available")
    except Exception:
        save_job_logs(namespace, f"{deploy_info.deployment_id}-job-hyperlane-svm-deployer")
        save_job_describe(namespace, f"{deploy_info.deployment_id}-job-hyperlane-svm-deployer")
        raise

    yield deploy_info

    if not skip_cleanup:
        log.info("Stopping deployer stack...")
        stop_stack("hyperlane-svm-deployer")


# ---------------------------------------------------------------------------
# Warp deployment helpers
# ---------------------------------------------------------------------------


def _write_solana_config(keypair_path: str, rpc_url: str) -> str:
    """Write a temporary Solana CLI config file. Returns its path."""
    config_path = Path(tempfile.gettempdir()) / "hyperlane-e2e-solana-config.yml"
    config_path.write_text(
        f'json_rpc_url: "{rpc_url}"\n'
        f'websocket_url: ""\n'
        f'keypair_path: "{keypair_path}"\n'
        f"commitment: finalized\n"
    )
    return str(config_path)


def _create_and_fund_spl_token(keypair_path: str, rpc_url: str = "http://localhost:18899") -> str:
    """Create a test SPL token with account and supply. Returns mint address."""
    cfg = _write_solana_config(keypair_path, rpc_url)
    cli_args = ["--config", cfg, "--url", rpc_url]

    # Create token with 6 decimals (USDC-like)
    result = subprocess.run(
        ["spl-token", *cli_args, "create-token", "--decimals", "6"],
        capture_output=True, text=True, check=True,
    )
    output = result.stdout + result.stderr
    match = re.search(r"Creating token (\w+)", output)
    if not match:
        match = re.search(r"Address:\s+(\w+)", output)
    if not match:
        raise RuntimeError(f"Failed to parse token mint from output: {output}")
    mint = match.group(1)
    log.info("Created SPL token mint: %s", mint)

    # Create token account
    subprocess.run(
        ["spl-token", *cli_args, "create-account", mint],
        capture_output=True, text=True, check=True,
    )
    log.info("Created token account for mint %s", mint)

    # Mint 1,000,000 tokens (6 decimals)
    subprocess.run(
        ["spl-token", *cli_args, "mint", mint, "1000000"],
        capture_output=True, text=True, check=True,
    )
    log.info("Minted 1,000,000 tokens")

    return mint


def _patch_warp_spec(token_mint: str) -> Path:
    """Substitute the token mint placeholder in the warp spec."""
    content = WARP_SPEC.read_text()
    patched = content.replace("REPLACE_AT_RUNTIME", token_mint)
    patched_path = E2E_DIR / ".warp-spec-patched.yml"
    patched_path.write_text(patched)
    return patched_path


@pytest.fixture(scope="session")
def warp_deployment(
    deployer_deployment: DeploymentInfo,
    bridge_state_loader: BridgeStateLoader,
    keypairs: KeypairSet,
    request: pytest.FixtureRequest,
) -> Generator[dict, None, None]:
    """Deploy the warp route stack once for the entire test session."""
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_warp_deploy = request.config.getoption("--skip-warp-deploy")

    if skip_warp_deploy:
        # Reuse existing warp deployment — recover token_mint from the
        # token-config.json state file written by the warp deployer job.
        deploy_dir = DEPLOY_DIR / "hyperlane-svm-warp-deployer"
        if deployment_exists(deploy_dir):
            namespace = "laconic-hyperlane-svm-warp-deployer"
            log.info("Reusing existing warp deployment (namespace: %s)", namespace)
            token_config = bridge_state_loader.read_json("token-config.json")
            token_mint = token_config.get("warpRoute", {}).get("tokenMint", "")
            assert token_mint, "Cannot recover token_mint from token-config.json (is warp deployed?)"
            log.info("Recovered token mint from state file: %s", token_mint)
            deployment_id = get_deployment_id(deploy_dir)
            yield {
                "deployment": DeploymentInfo(
                    deploy_dir=deploy_dir,
                    deployment_id=deployment_id,
                    namespace=namespace,
                ),
                "token_mint": token_mint,
                "namespace": namespace,
            }
            return
        log.info("--skip-warp-deploy set but %s missing — deploying fresh", deploy_dir)

    log.info("Creating and funding test SPL token on Solana...")
    deployer_keypair = str(KEYS_DIR / "deployer.json")
    token_mint = _create_and_fund_spl_token(keypair_path=deployer_keypair)
    log.info("Test SPL token mint: %s", token_mint)

    log.info("Patching warp deployer spec with token mint...")
    patched_spec = _patch_warp_spec(token_mint)

    log.info("Preparing warp deployer stack...")
    warp_info = deploy_prepare(
        "hyperlane-svm-warp-deployer",
        patched_spec,
        spec_replacements=SPEC_REPLACEMENTS,
        deployment_id="warp-deployer",
    )

    bridge_state_loader.populate("hyperlane-svm-warp-deployer", warp_info.deploy_dir)

    os.environ.update({
        "DEPLOYER_KEYPAIR":       keypairs.deployer_keypair,
        "HARDWARE_WALLET_PUBKEY": keypairs.hardware_wallet_pubkey,
    })

    log.info("Starting warp deployer stack...")
    deploy_start(warp_info.deploy_dir)

    try:
        log.info("Waiting for warp deployer job to complete...")
        job_name = f"{warp_info.deployment_id}-job-hyperlane-svm-warp-deployer"
        wait_for_job_complete(warp_info.namespace, job_name, timeout=1200)
        save_job_logs(warp_info.namespace, job_name)
        log.info("Warp deployer job complete, artifacts available")
    except Exception:
        save_job_logs(warp_info.namespace, f"{warp_info.deployment_id}-job-hyperlane-svm-warp-deployer")
        save_job_describe(warp_info.namespace, f"{warp_info.deployment_id}-job-hyperlane-svm-warp-deployer")
        raise

    ctx = {
        "deployment": warp_info,
        "token_mint": token_mint,
        "namespace": warp_info.namespace,
    }

    yield ctx

    patched_spec.unlink(missing_ok=True)
    if not skip_cleanup:
        log.info("Stopping warp deployer stack...")
        stop_stack("hyperlane-svm-warp-deployer")


# ---------------------------------------------------------------------------
# Mock Privy server
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def privy_mock(request: pytest.FixtureRequest, keypairs: KeypairSet) -> Generator[dict[str, str], None, None]:
    """Start the mock Privy server and yield wallet_keys dict.

    Runs as a detached subprocess (start_new_session=True) so it survives
    pytest exit — same pattern as chain nodes. On re-entry with
    --skip-cleanup, detects the already-running server and reuses it.

    wallet_keys maps wallet_id -> private_key_hex.
    """
    wallet_keys = generate_wallet_keys(keypairs)
    skip_cleanup = request.config.getoption("--skip-cleanup")

    # Load oracle address for logging
    oracle_address = None
    oracle_keypair_path = keypairs.keys_dir / "igp-oracle.json"
    if oracle_keypair_path.is_file():
        oracle_address, _ = load_oracle_keypair(keypairs.keys_dir)

    if is_privy_mock_running(PRIVY_MOCK_PORT):
        log.info("Privy-mock already running on :%d, reusing", PRIVY_MOCK_PORT)
        log.info(
            "Mock Privy wallets — gorchain H160: %s, solana H160: %s, oracle: %s",
            derive_h160_address(wallet_keys[GORCHAIN_WALLET_ID]),
            derive_h160_address(wallet_keys[SOLANA_WALLET_ID]),
            oracle_address or "none",
        )
        yield wallet_keys
    else:
        log.info("Starting mock Privy server on :%d (detached subprocess)...", PRIVY_MOCK_PORT)
        log_file = open("/tmp/privy-mock.log", "w")  # noqa: SIM115
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "lib.privy_mock",
                "--keys-dir", str(keypairs.keys_dir),
                "--port", str(PRIVY_MOCK_PORT),
            ],
            cwd=str(E2E_DIR),
            start_new_session=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        for _ in range(30):
            if is_privy_mock_running(PRIVY_MOCK_PORT):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(
                f"Privy-mock failed to start on :{PRIVY_MOCK_PORT} "
                f"(pid={proc.pid}). Check /tmp/privy-mock.log"
            )
        log.info("Mock Privy server started (pid=%d) on :%d", proc.pid, PRIVY_MOCK_PORT)
        log.info(
            "Mock Privy wallets — gorchain H160: %s, solana H160: %s, oracle: %s",
            derive_h160_address(wallet_keys[GORCHAIN_WALLET_ID]),
            derive_h160_address(wallet_keys[SOLANA_WALLET_ID]),
            oracle_address or "none",
        )
        yield wallet_keys

    if not skip_cleanup:
        log.info("Stopping mock Privy server on :%d...", PRIVY_MOCK_PORT)
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{PRIVY_MOCK_PORT}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            for pid in result.stdout.strip().split("\n"):
                log.info("Killing privy-mock process (PID %s)", pid)
                subprocess.run(["kill", pid], capture_output=True)


# ---------------------------------------------------------------------------
# Validator deployments
# ---------------------------------------------------------------------------


def _deploy_validator(
    chain: str,
    spec_file: Path,
    wallet_id: str,
    minio: MinioInfo,
    deployer: DeploymentInfo,
    bridge_state_loader: BridgeStateLoader,
    request: pytest.FixtureRequest,
) -> Generator[ValidatorInfo, None, None]:
    """Deploy a single validator stack for the given chain."""
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_validator = request.config.getoption("--skip-validator-deploy", default=False)
    # Two validator deployments share the hyperlane-validator stack, so SO
    # cannot derive distinct namespaces from the stack name alone. The test
    # specs set namespace: laconic-hyperlane-validator-{chain} explicitly and
    # we mirror that here.
    namespace = f"laconic-hyperlane-validator-{chain}"

    stack_name = f"hyperlane-validator-{chain}"

    if skip_validator:
        deploy_dir = DEPLOY_DIR / stack_name
        if deployment_exists(deploy_dir):
            deployment_id = get_deployment_id(deploy_dir)
            log.info("Reusing existing %s deployment (namespace: %s)", stack_name, namespace)
            yield ValidatorInfo(
                deployment=DeploymentInfo(deploy_dir=deploy_dir, deployment_id=deployment_id, namespace=namespace),
                chain=chain,
                wallet_id=wallet_id,
            )
            return
        log.info("--skip-validator-deploy set but %s missing — deploying fresh", deploy_dir)

    # Generate and fund a chain signer key for the announce transaction.
    # This is a hot ed25519 key separate from the KMS-backed validator key.
    chain_signer_key, chain_signer_addr = generate_chain_signer(
        KEYS_DIR, name=f"{chain}-chain-signer",
    )
    rpc = "http://localhost:8899" if chain == "gorchain" else "http://localhost:18899"
    log.info("Funding chain signer %s on %s...", chain_signer_addr, chain)
    _airdrop(1, chain_signer_addr, rpc, f"{chain} chain signer")

    validator_replacements = {
        **SPEC_REPLACEMENTS,
        "REPLACE_PRIVY_WALLET_ID": wallet_id,
    }

    log.info("Preparing %s stack...", stack_name)
    deploy_info = deploy_prepare(
        "hyperlane-validator",
        spec_file,
        deploy_dir=DEPLOY_DIR / stack_name,
        namespace=namespace,
        spec_replacements=validator_replacements,
        deployment_id=f"val-{chain}",
    )

    bridge_state_loader.populate("hyperlane-validator", deploy_info.deploy_dir)

    # Chain-specific MinIO IAM credentials.
    # Naming: "{chain}-primary" label → "GORCHAIN_PRIMARY_KEY_ID" / "GORCHAIN_PRIMARY_SECRET"
    # These map to AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY inside the validator container
    # via the spec's secrets: block (e.g. AWS_ACCESS_KEY_ID: { env: GORCHAIN_PRIMARY_KEY_ID }).
    chain_upper = chain.upper()
    label_upper = f"{chain_upper}_PRIMARY"
    validator_key_id = minio.gorchain_key_id if chain == "gorchain" else minio.solana_key_id
    validator_secret = minio.gorchain_secret if chain == "gorchain" else minio.solana_secret

    os.environ.update({
        "PRIVY_APP_ID":            "test-app-id",
        "PRIVY_APP_SECRET":        "test-app-secret",
        f"{label_upper}_KEY_ID":   validator_key_id,
        f"{label_upper}_SECRET":   validator_secret,
        "HYP_DEFAULTSIGNER_KEY":   chain_signer_key,
    })

    log.info("Starting %s stack...", stack_name)
    deploy_start(deploy_info.deploy_dir)

    try:
        log.info("Waiting for %s pod to be running...", stack_name)
        wait_for_pod_phase(namespace, f"app={deploy_info.deployment_id}", "Running", timeout=120)
        log.info("%s is running", stack_name)
    except Exception:
        save_pod_logs(namespace, f"app={deploy_info.deployment_id}", f"validator-{chain}")
        save_pod_describe(namespace, f"app={deploy_info.deployment_id}", f"validator-{chain}")
        raise

    yield ValidatorInfo(
        deployment=deploy_info,
        chain=chain,
        wallet_id=wallet_id,
    )

    save_pod_logs(namespace, f"app={deploy_info.deployment_id}", f"validator-{chain}")
    if not skip_cleanup:
        log.info("Stopping %s stack...", stack_name)
        stop_stack("hyperlane-validator", deploy_dir=DEPLOY_DIR / stack_name)


@pytest.fixture(scope="session")
def validator_gorchain(
    request: pytest.FixtureRequest,
    deployer_deployment: DeploymentInfo,
    minio_deployment: MinioInfo,
    privy_mock: dict[str, str],
    bridge_state_loader: BridgeStateLoader,
) -> Generator[ValidatorInfo, None, None]:
    """Deploy the gorchain validator."""
    yield from _deploy_validator(
        chain="gorchain",
        spec_file=VALIDATOR_GORCHAIN_SPEC,
        wallet_id=GORCHAIN_WALLET_ID,
        minio=minio_deployment,
        deployer=deployer_deployment,
        bridge_state_loader=bridge_state_loader,
        request=request,
    )


@pytest.fixture(scope="session")
def validator_solana(
    request: pytest.FixtureRequest,
    deployer_deployment: DeploymentInfo,
    minio_deployment: MinioInfo,
    privy_mock: dict[str, str],
    bridge_state_loader: BridgeStateLoader,
) -> Generator[ValidatorInfo, None, None]:
    """Deploy the solana validator."""
    yield from _deploy_validator(
        chain="solana",
        spec_file=VALIDATOR_SOLANA_SPEC,
        wallet_id=SOLANA_WALLET_ID,
        minio=minio_deployment,
        deployer=deployer_deployment,
        bridge_state_loader=bridge_state_loader,
        request=request,
    )


# ---------------------------------------------------------------------------
# Relayer deployment
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def relayer_deployment(
    deployer_deployment: DeploymentInfo,
    minio_deployment: MinioInfo,
    validator_images: None,
    host_prep: None,
    bridge_state_loader: BridgeStateLoader,
    request: pytest.FixtureRequest,
) -> Generator[RelayerInfo, None, None]:
    """Deploy the hyperlane-relayer stack."""
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_relayer = request.config.getoption("--skip-relayer-deploy", default=False)
    namespace = "laconic-hyperlane-relayer"

    if skip_relayer:
        deploy_dir = DEPLOY_DIR / "hyperlane-relayer"
        if deployment_exists(deploy_dir):
            deployment_id = get_deployment_id(deploy_dir)
            log.info("Reusing existing relayer deployment (namespace: %s)", namespace)
            yield RelayerInfo(
                deployment=DeploymentInfo(deploy_dir=deploy_dir, deployment_id=deployment_id, namespace=namespace),
            )
            return
        log.info("--skip-relayer-deploy set but %s missing — deploying fresh", deploy_dir)

    # Generate and fund chain signer keys for gorchain and solana
    gorchain_signer_key, gorchain_signer_addr = generate_chain_signer(
        KEYS_DIR, name="relayer-gorchain-signer",
    )
    log.info("Funding relayer gorchain signer %s...", gorchain_signer_addr)
    _airdrop(1, gorchain_signer_addr, "http://localhost:8899", "relayer gorchain signer")

    solana_signer_key, solana_signer_addr = generate_chain_signer(
        KEYS_DIR, name="relayer-solana-signer",
    )
    log.info("Funding relayer solana signer %s...", solana_signer_addr)
    _airdrop(1, solana_signer_addr, "http://localhost:18899", "relayer solana signer")

    # Generate and fund a relayer keypair for IGP fee claims
    _, fee_claim_addr = generate_chain_signer(
        KEYS_DIR, name="relayer-fee-claim",
    )
    log.info("Funding relayer fee claim key %s on both chains...", fee_claim_addr)
    _airdrop(1, fee_claim_addr, "http://localhost:8899", "relayer fee claim (gorchain)")
    _airdrop(1, fee_claim_addr, "http://localhost:18899", "relayer fee claim (solana)")
    relayer_keypair_json = (KEYS_DIR / "relayer-fee-claim.json").read_text().strip()

    # Read IGP program IDs and accounts from the deployer state files
    for chain in ("gorchain", "solana"):
        program_ids = bridge_state_loader.read_program_ids(chain)
        if chain == "gorchain":
            gorchain_igp_program_id = program_ids["igp_program_id"]
            gorchain_igp_account = program_ids["igp_account"]
        else:
            solana_igp_program_id = program_ids["igp_program_id"]
            solana_igp_account = program_ids["igp_account"]

    # Patch the spec with actual IGP values
    content = RELAYER_SPEC.read_text()
    content = content.replace(
        'GORCHAIN_IGP_PROGRAM_ID: "REPLACE_AT_RUNTIME"',
        f'GORCHAIN_IGP_PROGRAM_ID: "{gorchain_igp_program_id}"',
    )
    content = content.replace(
        'SOLANA_IGP_PROGRAM_ID: "REPLACE_AT_RUNTIME"',
        f'SOLANA_IGP_PROGRAM_ID: "{solana_igp_program_id}"',
    )
    content = content.replace(
        'GORCHAIN_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"',
        f'GORCHAIN_IGP_ACCOUNT: "{gorchain_igp_account}"',
    )
    content = content.replace(
        'SOLANA_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"',
        f'SOLANA_IGP_ACCOUNT: "{solana_igp_account}"',
    )
    patched_path = E2E_DIR / ".relayer-spec-patched.yml"
    patched_path.write_text(content)

    log.info("Preparing relayer stack...")
    deploy_info = deploy_prepare(
        "hyperlane-relayer", patched_path,
        spec_replacements=SPEC_REPLACEMENTS,
        deployment_id="relayer",
    )

    bridge_state_loader.populate("hyperlane-relayer", deploy_info.deploy_dir)

    # No MinIO credentials for the relayer — it uses an anonymous S3 client (.no_credentials())
    # to read validator checkpoints. Buckets are publicly readable (anonymous download policy).
    os.environ.update({
        "HYP_CHAINS_GORCHAIN_SIGNER_KEY": gorchain_signer_key,
        "HYP_CHAINS_SOLANA_SIGNER_KEY":   solana_signer_key,
        "RELAYER_KEYPAIR_JSON":           relayer_keypair_json,
    })

    log.info("Starting relayer stack...")
    deploy_start(deploy_info.deploy_dir)

    try:
        log.info("Waiting for relayer pod to be running...")
        wait_for_pod_phase(namespace, f"app={deploy_info.deployment_id}", "Running", timeout=180)
        log.info("Relayer is running")
    except Exception:
        save_pod_logs(namespace, f"app={deploy_info.deployment_id}", "relayer")
        save_pod_describe(namespace, f"app={deploy_info.deployment_id}", "relayer")
        raise

    yield RelayerInfo(deployment=deploy_info)

    save_pod_logs(namespace, f"app={deploy_info.deployment_id}", "relayer")
    patched_path.unlink(missing_ok=True)
    if not skip_cleanup:
        log.info("Stopping relayer stack...")
        stop_stack("hyperlane-relayer")


# ---------------------------------------------------------------------------
# Gas oracle deployment
# ---------------------------------------------------------------------------


def _wait_for_oracle_update(
    namespace: str, deployment_id: str, timeout: int = 120,
) -> dict:
    """Wait for the gas oracle to complete its first update cycle.

    Polls the oracle's output file (/tmp/oracle-latest.json) inside the pod
    via ``kubectl exec``. The oracle writes this file after each successful
    update, so its presence means the update is complete and its contents
    are the authoritative record of what was submitted on-chain.

    Returns the parsed JSON dict, or raises TimeoutError.
    """
    import json

    deadline = time.time() + timeout
    pod_name = None

    while time.time() < deadline:
        # Find the oracle pod name (needed for exec)
        if pod_name is None:
            result = subprocess.run(
                [
                    "kubectl", "-n", namespace, "get", "pods",
                    "-l", f"app={deployment_id}",
                    "-o", "jsonpath={.items[0].metadata.name}",
                ],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                pod_name = result.stdout.strip()

        if pod_name:
            result = subprocess.run(
                [
                    "kubectl", "-n", namespace, "exec", pod_name,
                    "--", "cat", "/tmp/oracle-latest.json",
                ],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError:
                    log.warning("oracle-latest.json not valid JSON yet")

        time.sleep(5)

    raise TimeoutError(
        f"Gas oracle did not complete update within {timeout}s"
    )


@pytest.fixture(scope="session")
def gas_oracle_deployment(
    deployer_deployment: DeploymentInfo,
    privy_mock: dict[str, str],
    host_prep: None,
    gas_oracle_image: None,
    bridge_state_loader: BridgeStateLoader,
    request: pytest.FixtureRequest,
) -> Generator[dict, None, None]:
    """Deploy the hyperlane-gas-oracle stack and wait for first update."""
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_oracle = request.config.getoption("--skip-gas-oracle-deploy", default=False)
    namespace = "laconic-hyperlane-gas-oracle"

    if skip_oracle:
        deploy_dir = DEPLOY_DIR / "hyperlane-gas-oracle"
        if deployment_exists(deploy_dir):
            deployment_id = get_deployment_id(deploy_dir)
            log.info("Reusing existing gas oracle deployment (namespace: %s)", namespace)
            # Read current oracle values from the running pod
            oracle_values = {}
            try:
                oracle_values = _wait_for_oracle_update(namespace, deployment_id, timeout=30)
            except TimeoutError:
                log.warning("Could not read oracle values from running pod")
            yield {
                "deployment": DeploymentInfo(
                    deploy_dir=deploy_dir, deployment_id=deployment_id, namespace=namespace,
                ),
                "oracle_values": oracle_values,
            }
            return
        log.info("--skip-gas-oracle-deploy set but %s missing — deploying fresh", deploy_dir)

    # Read IGP program IDs from the deployer state files
    gorchain_program_ids = bridge_state_loader.read_program_ids("gorchain")
    solana_program_ids = bridge_state_loader.read_program_ids("solana")
    gorchain_igp_program_id = gorchain_program_ids["igp_program_id"]
    solana_igp_program_id = solana_program_ids["igp_program_id"]

    # Patch the spec with actual IGP program IDs
    content = GAS_ORACLE_SPEC.read_text()
    content = content.replace(
        'GORCHAIN_IGP_PROGRAM_ID: "REPLACE_AT_RUNTIME"',
        f'GORCHAIN_IGP_PROGRAM_ID: "{gorchain_igp_program_id}"',
    )
    content = content.replace(
        'SOLANA_IGP_PROGRAM_ID: "REPLACE_AT_RUNTIME"',
        f'SOLANA_IGP_PROGRAM_ID: "{solana_igp_program_id}"',
    )
    patched_path = E2E_DIR / ".gas-oracle-spec-patched.yml"
    patched_path.write_text(content)

    log.info("Preparing gas oracle stack...")
    deploy_info = deploy_prepare(
        "hyperlane-gas-oracle", patched_path,
        spec_replacements=SPEC_REPLACEMENTS,
        deployment_id="gas-oracle",
    )

    bridge_state_loader.populate("hyperlane-gas-oracle", deploy_info.deploy_dir)

    os.environ.update({
        "PRIVY_APP_ID":           "test-app-id",
        "PRIVY_APP_SECRET":       "test-app-secret",
        "PRIVY_ORACLE_WALLET_ID": ORACLE_WALLET_ID,
    })

    log.info("Starting gas oracle stack...")
    deploy_start(deploy_info.deploy_dir)

    try:
        log.info("Waiting for gas oracle pod to be running...")
        wait_for_pod_phase(
            namespace, f"app={deploy_info.deployment_id}", "Running", timeout=120,
        )
        log.info("Gas oracle pod is running")
    except Exception:
        save_pod_logs(namespace, f"app={deploy_info.deployment_id}", "gas-oracle")
        save_pod_describe(namespace, f"app={deploy_info.deployment_id}", "gas-oracle")
        raise

    # Wait for the first successful oracle update
    try:
        log.info("Waiting for gas oracle to complete first update...")
        oracle_values = _wait_for_oracle_update(namespace, deploy_info.deployment_id)
        log.info("Gas oracle update complete. Values: %s", oracle_values)
    except TimeoutError:
        save_pod_logs(namespace, f"app={deploy_info.deployment_id}", "gas-oracle")
        raise

    yield {
        "deployment": deploy_info,
        "oracle_values": oracle_values,
    }

    save_pod_logs(namespace, f"app={deploy_info.deployment_id}", "gas-oracle")
    patched_path.unlink(missing_ok=True)
    if not skip_cleanup:
        log.info("Stopping gas oracle stack...")
        stop_stack("hyperlane-gas-oracle")


# ---------------------------------------------------------------------------
# Monitoring stack
# ---------------------------------------------------------------------------

GRAFANA_ADMIN_PASSWORD = "testadmin"
GRAFANA_HOSTNAME = "grafana.test"
GRAFANA_URL = f"https://{GRAFANA_HOSTNAME}"
PROMETHEUS_HOSTNAME = "prometheus.test"
PROMETHEUS_URL = f"https://{PROMETHEUS_HOSTNAME}"

def _wait_for_balance_monitor(
    namespace: str, pod_name: str, timeout: int = 60,
) -> None:
    """Wait for the balance monitor to complete at least one check cycle."""
    import subprocess

    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            [
                "kubectl", "-n", namespace, "logs", pod_name,
                "-c", "balance-monitor",
            ],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and "Gorchain wallets:" in result.stdout:
            log.info("Balance monitor has started and reported wallets")
            return
        time.sleep(5)

    raise TimeoutError(
        f"Balance monitor did not report wallets within {timeout}s"
    )


def _build_wallet_string(keypairs: KeypairSet) -> tuple[str, list[str]]:
    """Build MONITORED_WALLETS string and return (wallet_string, label_list).

    Both chains use the same wallets (all funded on both chains during setup).
    """
    # Get relayer pubkey from fee-claim keypair (if it exists)
    relayer_keypair_path = keypairs.keys_dir / "relayer-fee-claim.json"
    # (label, address, per-wallet threshold)
    wallet_entries = [
        ("deployer", keypairs.deployer_pubkey, None),
        ("igp-oracle", keypairs.igp_oracle_pubkey, "2.0"),
        ("igp-beneficiary", keypairs.igp_beneficiary_pubkey, None),
    ]
    if relayer_keypair_path.is_file():
        result = subprocess.run(
            ["solana-keygen", "pubkey", str(relayer_keypair_path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            wallet_entries.append(("relayer", result.stdout.strip(), "5.0"))

    parts = []
    for label, addr, threshold in wallet_entries:
        if threshold:
            parts.append(f"{label}:{addr}:{threshold}")
        else:
            parts.append(f"{label}:{addr}")
    wallet_string = ",".join(parts)
    labels = [label for label, _, _ in wallet_entries]
    return wallet_string, labels


@pytest.fixture(scope="session")
def monitoring_deployment(
    deployer_deployment: DeploymentInfo,
    keypairs: KeypairSet,
    host_prep: None,
    monitoring_images: None,
    bridge_state_loader: BridgeStateLoader,
    request: pytest.FixtureRequest,
) -> Generator[dict, None, None]:
    """Deploy the hyperlane-monitoring stack and wait for metrics flow."""
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_monitoring = request.config.getoption("--skip-monitoring-deploy", default=False)
    namespace = "laconic-hyperlane-monitoring"

    if skip_monitoring:
        deploy_dir = DEPLOY_DIR / "hyperlane-monitoring"
        if deployment_exists(deploy_dir):
            deployment_id = get_deployment_id(deploy_dir)
            pod_name = subprocess.run(
                [
                    "kubectl", "-n", namespace, "get", "pods",
                    "-l", f"app={deployment_id}",
                    "-o", "jsonpath={.items[0].metadata.name}",
                ],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
            assert pod_name, "Monitoring pod not found — cannot reuse deployment"
            # Recover wallet labels from Prometheus metrics
            wallet_labels = []
            probe = subprocess.run(
                ["curl", "-s",
                 f"{PROMETHEUS_URL}/api/v1/query?query=hyperlane_wallet_balance_sol"],
                capture_output=True, text=True, check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                import json as _json
                try:
                    data = _json.loads(probe.stdout)
                    labels = {
                        r["metric"].get("wallet")
                        for r in data.get("data", {}).get("result", [])
                        if r["metric"].get("wallet")
                    }
                    wallet_labels = sorted(labels)
                except (ValueError, KeyError):
                    pass
            log.info("Reusing existing monitoring deployment (namespace: %s)", namespace)
            yield {
                "deployment": DeploymentInfo(
                    deploy_dir=deploy_dir, deployment_id=deployment_id, namespace=namespace,
                ),
                "namespace": namespace,
                "pod_name": pod_name,
                "expected_wallet_labels": wallet_labels,
                "grafana_url": GRAFANA_URL,
                "prometheus_url": PROMETHEUS_URL,
            }
            return
        log.info("--skip-monitoring-deploy set but %s missing — deploying fresh", deploy_dir)

    # Build wallet strings from keypairs
    wallet_string, wallet_labels = _build_wallet_string(keypairs)
    log.info("Monitoring wallet string: %s", wallet_string)

    # Patch spec with wallet strings
    content = MONITORING_SPEC.read_text()
    content = content.replace(
        'MONITORED_WALLETS_GORCHAIN: "REPLACE_AT_RUNTIME"',
        f'MONITORED_WALLETS_GORCHAIN: "{wallet_string}"',
    )
    content = content.replace(
        'MONITORED_WALLETS_SOLANA: "REPLACE_AT_RUNTIME"',
        f'MONITORED_WALLETS_SOLANA: "{wallet_string}"',
    )
    patched_path = E2E_DIR / ".monitoring-spec-patched.yml"
    patched_path.write_text(content)

    log.info("Preparing monitoring stack...")
    deploy_info = deploy_prepare(
        "hyperlane-monitoring", patched_path,
        spec_replacements=SPEC_REPLACEMENTS,
        deployment_id="monitoring",
    )

    bridge_state_loader.populate("hyperlane-monitoring", deploy_info.deploy_dir)

    os.environ["GF_SECURITY_ADMIN_PASSWORD"] = GRAFANA_ADMIN_PASSWORD

    log.info("Starting monitoring stack...")
    deploy_start(deploy_info.deploy_dir)

    try:
        log.info("Waiting for monitoring pod to be running...")
        wait_for_pod_phase(
            namespace, f"app={deploy_info.deployment_id}", "Running", timeout=180,
        )
        log.info("Monitoring pod is running")
    except Exception:
        save_pod_logs(namespace, f"app={deploy_info.deployment_id}", "monitoring")
        save_pod_describe(namespace, f"app={deploy_info.deployment_id}", "monitoring")
        raise

    # Get pod name for kubectl exec in tests
    pod_name = subprocess.run(
        [
            "kubectl", "-n", namespace, "get", "pods",
            "-l", f"app={deploy_info.deployment_id}",
            "-o", "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Wait for balance monitor to complete first check
    try:
        log.info("Waiting for balance monitor to report wallets...")
        _wait_for_balance_monitor(namespace, pod_name)
    except TimeoutError:
        save_pod_logs(namespace, f"app={deploy_info.deployment_id}", "monitoring")
        save_pod_describe(namespace, f"app={deploy_info.deployment_id}", "monitoring")
        raise

    # Give Prometheus time to scrape the Pushgateway metrics
    log.info("Waiting for Prometheus to scrape balance metrics...")
    time.sleep(20)

    # Caddy serves grafana.test and prometheus.test via SO's http-proxy emission;
    # the cert was pre-loaded into caddy-system at install time.
    for url, health_path in [(GRAFANA_URL, "/api/health"),
                             (PROMETHEUS_URL, "/-/healthy")]:
        log.info("Waiting for %s to respond via Caddy ingress...", url)
        for _ in range(30):
            probe = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 f"{url}{health_path}"],
                capture_output=True, text=True, check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "200":
                break
            time.sleep(2)
        else:
            log.warning("%s not returning 200 after 60s", url)
        log.info("Ingress ready at %s", url)

    yield {
        "deployment": deploy_info,
        "namespace": namespace,
        "pod_name": pod_name,
        "expected_wallet_labels": wallet_labels,
        "grafana_url": GRAFANA_URL,
        "prometheus_url": PROMETHEUS_URL,
    }

    save_pod_logs(namespace, f"app={deploy_info.deployment_id}", "monitoring")
    patched_path.unlink(missing_ok=True)
    if not skip_cleanup:
        log.info("Stopping monitoring stack...")
        stop_stack("hyperlane-monitoring")


# ---------------------------------------------------------------------------
# Bridge transfer setup
# ---------------------------------------------------------------------------


def _get_warp_program_addresses(state_loader: BridgeStateLoader) -> dict[str, str]:
    """Read warp-deploy-outputs state files and return {chain: base58_address}."""
    import json

    outputs_dir = state_loader.state_dir / "warp-deploy-outputs"
    assert outputs_dir.is_dir(), f"{outputs_dir} does not exist"

    files = list(outputs_dir.iterdir())
    assert files, f"{outputs_dir} has no files"

    programs: dict[str, str] = {}
    for f in files:
        if not f.is_file():
            continue
        for chain_name, entry in json.loads(f.read_text()).items():
            if entry.get("base58"):
                programs[chain_name] = entry["base58"]
    return programs


def _get_synthetic_mint(warp_program: str, rpc: str) -> str:
    """Query the synthetic warp token program and extract the mint address."""
    result = run_deployer_cli(
        "token", "query",
        "--program-id", warp_program,
        "synthetic",
        rpc=rpc,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"token query synthetic failed: {output}"

    # Parse "Mint / Mint Authority: <pubkey>" from the Rust output
    match = re.search(r"Mint / Mint Authority:\s*(\w{32,44})", output)
    if match:
        return match.group(1)

    # Fallback: look for "mint: <pubkey>"
    match = re.search(r"\bmint:\s+(\w{32,44})", output)
    assert match, f"Could not parse synthetic mint from output: {output[:500]}"
    return match.group(1)


def _set_igp_beneficiary(
    rpc: str,
    program_id: str,
    igp_account: str,
    new_beneficiary: str,
    chain: str,
    owner_keypair: str | None = None,
) -> None:
    """Change the IGP account beneficiary to a new address.

    Must be signed by the IGP account owner (igp-oracle key after
    ownership transfer, or deployer key if transfer hasn't happened).
    """
    result = run_deployer_cli(
        "igp", "set-igp-beneficiary",
        "--program-id", program_id,
        "--igp-account", igp_account,
        new_beneficiary,
        keypair_path=owner_keypair,
        rpc=rpc,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"Failed to set IGP beneficiary on {chain}: {output}"
    )
    log.info("Set %s IGP beneficiary to %s", chain, new_beneficiary)


@pytest.fixture(scope="session")
def bridge_setup(
    warp_deployment: dict,
    relayer_deployment: RelayerInfo,
    validator_gorchain: ValidatorInfo,
    validator_solana: ValidatorInfo,
    bridge_state_loader: BridgeStateLoader,
) -> dict:
    """Set up bridge transfer context.

    Depends on all bridge infrastructure being deployed. Returns a dict with
    warp program addresses, token mints, and sender keypair path.

    Also configures a dedicated IGP beneficiary (separate from the deployer)
    so that fee claim tests can observe balance changes.
    """
    token_mint = warp_deployment["token_mint"]
    sender_keypair = str(KEYS_DIR / "deployer.json")

    log.info("Resolving warp program addresses...")
    warp_programs = _get_warp_program_addresses(bridge_state_loader)
    assert "solana" in warp_programs, "No warp program for solana"
    assert "gorchain" in warp_programs, "No warp program for gorchain"
    log.info("Warp programs: %s", warp_programs)

    log.info("Querying synthetic mint on Gorchain...")
    synthetic_mint = _get_synthetic_mint(
        warp_programs["gorchain"], rpc="http://localhost:8899",
    )
    log.info("Synthetic mint: %s", synthetic_mint)

    # Change IGP beneficiary on both chains from deployer → dedicated account.
    # The beneficiary keypair is generated and funded during initial test setup
    # (keygen.py). Without this, the deployer is both fee payer and beneficiary,
    # making fee collection invisible to fee claim tests.
    # TODO: add an ops job/playbook to configure the IGP beneficiary address
    # for production deployments (deployment/ops/).
    beneficiary_pubkey = subprocess.run(
        ["solana-keygen", "pubkey", str(KEYS_DIR / "igp-beneficiary.json")],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # IGP account ownership is transferred to the igp-oracle key during
    # core deployment. set-igp-beneficiary requires the owner's signature.
    igp_oracle_keypair = str(KEYS_DIR / "igp-oracle.json")
    log.info("Setting IGP beneficiary to %s...", beneficiary_pubkey)
    for chain in ("gorchain", "solana"):
        program_ids = bridge_state_loader.read_program_ids(chain)
        _set_igp_beneficiary(
            rpc=CHAINS[chain]["rpc"],
            program_id=program_ids["igp_program_id"],
            igp_account=program_ids["igp_account"],
            new_beneficiary=beneficiary_pubkey,
            chain=chain,
            owner_keypair=igp_oracle_keypair,
        )

    # Create bridge user keypairs and fund them for concurrent transfer tests.
    num_bridge_users = 5
    users: list[dict[str, str]] = []
    solana_rpc = CHAINS["solana"]["rpc"]
    gorchain_rpc = CHAINS["gorchain"]["rpc"]

    log.info("Creating %d bridge user keypairs...", num_bridge_users)
    for i in range(num_bridge_users):
        kp_path = KEYS_DIR / f"bridge-user-{i}.json"
        if not kp_path.exists():
            subprocess.run(
                [
                    "solana-keygen", "new",
                    "--no-bip39-passphrase", "--force",
                    "-o", str(kp_path),
                ],
                capture_output=True, text=True, check=True,
            )
        pubkey = subprocess.run(
            ["solana-keygen", "pubkey", str(kp_path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        users.append({"keypair_path": str(kp_path), "pubkey": pubkey})
        log.info("  bridge-user-%d: %s", i, pubkey)

    log.info("Funding bridge users with SOL...")
    for i, user in enumerate(users):
        label = f"bridge-user-{i}"
        _airdrop(10, user["pubkey"], solana_rpc, f"{label} (Solana)")
        _airdrop(10, user["pubkey"], gorchain_rpc, f"{label} (Gorchain)")

    log.info("Funding bridge users with USDC on Solana...")
    deployer_cfg = _write_solana_config(sender_keypair, solana_rpc)
    deployer_cli = ["--config", deployer_cfg, "--url", solana_rpc]
    for i, user in enumerate(users):
        result = subprocess.run(
            [
                "spl-token", *deployer_cli,
                "transfer", token_mint, "2", user["pubkey"],
                "--fund-recipient", "--allow-unfunded-recipient",
            ],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, (
            f"Failed to fund bridge-user-{i} with USDC: "
            f"{result.stdout} {result.stderr}"
        )
        log.info("  bridge-user-%d: funded 2.0 USDC", i)

    return {
        "relayer_namespace": relayer_deployment.namespace,
        "token_mint": token_mint,
        "synthetic_mint": synthetic_mint,
        "sender_keypair": sender_keypair,
        "warp_programs": warp_programs,
        "relayer_deployment_id": relayer_deployment.deployment_id,
        "users": users,
    }


# ---------------------------------------------------------------------------
# Warp UI deployment
# ---------------------------------------------------------------------------

WARP_UI_HOSTNAME = "bridge.test"
WARP_UI_URL = f"https://{WARP_UI_HOSTNAME}"

@pytest.fixture(scope="session")
def warp_ui_image(request: pytest.FixtureRequest, host_prep: None) -> None:
    """Build or pre-fetch the warp-ui image to host Docker (SO preloads it via image-overrides at deploy_start)."""
    if request.config.getoption("--skip-warp-ui-deploy", default=False):
        log.info("Skipping warp-ui image build (--skip-warp-ui-deploy)")
        return
    if request.config.getoption("--build-from-source"):
        log.info("Building warp-ui container image from source...")
        build_warp_ui_image()
    else:
        log.info("Pre-fetching published warp-ui image to host Docker...")
        prefetch_warp_ui_image()


@pytest.fixture(scope="session")
def warp_ui_deployment(
    request: pytest.FixtureRequest,
    warp_deployment: dict,
    deployer_deployment: DeploymentInfo,
    host_prep: None,
    bridge_state_loader: BridgeStateLoader,
) -> Generator[dict, None, None]:
    """Deploy the warp-ui stack with resolved addresses from state files."""
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_warp_ui = request.config.getoption("--skip-warp-ui-deploy", default=False)
    namespace = "laconic-hyperlane-warp-ui"

    # Resolve config values from deployer state files
    log.info("Resolving mailbox addresses from program-ids state files...")
    gorchain_programs = bridge_state_loader.read_program_ids("gorchain")
    solana_programs = bridge_state_loader.read_program_ids("solana")
    gorchain_mailbox = gorchain_programs["mailbox"]
    solana_mailbox = solana_programs["mailbox"]
    log.info("Mailboxes — gorchain: %s, solana: %s", gorchain_mailbox, solana_mailbox)

    warp_programs = _get_warp_program_addresses(bridge_state_loader)
    warp_collateral = warp_programs["solana"]
    warp_synthetic = warp_programs["gorchain"]
    log.info("Warp addresses — collateral: %s, synthetic: %s", warp_collateral, warp_synthetic)

    token_mint = warp_deployment["token_mint"]
    log.info("Token mint: %s", token_mint)

    synthetic_mint = _get_synthetic_mint(warp_synthetic, rpc="http://localhost:8899")
    log.info("Synthetic mint: %s", synthetic_mint)

    if skip_warp_ui:
        deploy_dir = DEPLOY_DIR / "hyperlane-warp-ui"
        if deployment_exists(deploy_dir):
            deployment_id = get_deployment_id(deploy_dir)
            log.info("Reusing existing warp-ui deployment (namespace: %s)", namespace)

            yield {
                "deployment": DeploymentInfo(deploy_dir=deploy_dir, deployment_id=deployment_id, namespace=namespace),
                "url": WARP_UI_URL,
                "gorchain_mailbox": gorchain_mailbox,
                "solana_mailbox": solana_mailbox,
                "warp_collateral": warp_collateral,
                "warp_synthetic": warp_synthetic,
                "token_mint": token_mint,
                "synthetic_mint": synthetic_mint,
            }
            return
        log.info("--skip-warp-ui-deploy set but %s missing — deploying fresh", deploy_dir)

    # Patch the spec with runtime values
    content = WARP_UI_SPEC.read_text()
    content = content.replace(
        'GORCHAIN_MAILBOX: "REPLACE_AT_RUNTIME"',
        f'GORCHAIN_MAILBOX: "{gorchain_mailbox}"',
    )
    content = content.replace(
        'SOLANA_MAILBOX: "REPLACE_AT_RUNTIME"',
        f'SOLANA_MAILBOX: "{solana_mailbox}"',
    )
    content = content.replace(
        'WARP_COLLATERAL_ADDRESS: "REPLACE_AT_RUNTIME"',
        f'WARP_COLLATERAL_ADDRESS: "{warp_collateral}"',
    )
    content = content.replace(
        'WARP_SYNTHETIC_ADDRESS: "REPLACE_AT_RUNTIME"',
        f'WARP_SYNTHETIC_ADDRESS: "{warp_synthetic}"',
    )
    content = content.replace(
        'WARP_TOKEN_MINT: "REPLACE_AT_RUNTIME"',
        f'WARP_TOKEN_MINT: "{token_mint}"',
    )
    content = content.replace(
        'WARP_SYNTHETIC_MINT: "REPLACE_AT_RUNTIME"',
        f'WARP_SYNTHETIC_MINT: "{synthetic_mint}"',
    )
    patched_path = E2E_DIR / ".warp-ui-spec-patched.yml"
    patched_path.write_text(content)

    log.info("Preparing warp-ui stack...")
    deploy_info = deploy_prepare(
        "hyperlane-warp-ui", patched_path,
        spec_replacements=SPEC_REPLACEMENTS,
        deployment_id="warp-ui",
    )

    bridge_state_loader.populate("hyperlane-warp-ui", deploy_info.deploy_dir)

    log.info("Starting warp-ui stack...")
    deploy_start(deploy_info.deploy_dir)

    log.info("Waiting for warp-ui pod to be running...")
    wait_for_pod_phase(namespace, f"app={deploy_info.deployment_id}", "Running", timeout=120)
    log.info("Warp UI is running")

    # Caddy serves bridge.test via SO's http-proxy emission.
    log.info("Waiting for warp-ui to respond via Caddy ingress...")
    for _ in range(60):
        probe = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"{WARP_UI_URL}/"],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "200":
            break
        time.sleep(2)
    else:
        log.warning("Warp UI not returning 200 after 120s")
    log.info("Warp UI ingress ready at %s", WARP_UI_URL)

    yield {
        "deployment": deploy_info,
        "url": WARP_UI_URL,
        "gorchain_mailbox": gorchain_mailbox,
        "solana_mailbox": solana_mailbox,
        "warp_collateral": warp_collateral,
        "warp_synthetic": warp_synthetic,
        "token_mint": token_mint,
        "synthetic_mint": synthetic_mint,
    }

    save_pod_logs(namespace, f"app={deploy_info.deployment_id}", "warp-ui")
    patched_path.unlink(missing_ok=True)
    if not skip_cleanup:
        log.info("Stopping warp-ui stack...")
        stop_stack("hyperlane-warp-ui")


@pytest.fixture(scope="session")
def warp_ui_browser(
    request: pytest.FixtureRequest,
    warp_ui_deployment: dict,
    bridge_setup: dict,
) -> Generator[dict, None, None]:
    """Launch Playwright browser with Backpack wallet extension.

    Uses a persistent browser context with the Backpack extension loaded.
    Chrome extensions require headed mode (headless=False). For headless
    operation, wrap the pytest invocation with ``xvfb-run``::

        xvfb-run pytest -v test_11_warp_ui_bridge.py

    Pass ``--headed`` to show the browser window on a real display.

    The Backpack wallet is configured with the test keypair and custom
    RPC URLs for gorchain and solana.
    """
    from playwright.sync_api import sync_playwright

    from lib.backpack import (
        get_backpack_extension_path,
        launch_browser_with_backpack,
        setup_backpack_wallet,
    )

    url = warp_ui_deployment["url"]
    sender_keypair = bridge_setup["sender_keypair"]

    # Download/cache Backpack extension
    ext_path = get_backpack_extension_path()

    pw = sync_playwright().start()

    # Launch browser with Backpack loaded
    context = launch_browser_with_backpack(pw, ext_path)

    # Configure wallet with test keypair and custom RPC URLs
    rpc_urls = {
        "gorchain": CHAINS["gorchain"]["rpc"],
        "solana": CHAINS["solana"]["rpc"],
    }
    wallet_pubkey = setup_backpack_wallet(context, sender_keypair, rpc_urls)
    log.info("Backpack wallet configured with pubkey: %s", wallet_pubkey)

    yield {
        "context": context,
        "playwright": pw,
        "url": url,
        "sender_keypair": sender_keypair,
        "wallet_pubkey": wallet_pubkey,
        "token_mint": bridge_setup["token_mint"],
        "synthetic_mint": bridge_setup["synthetic_mint"],
    }

    context.close()
    pw.stop()
