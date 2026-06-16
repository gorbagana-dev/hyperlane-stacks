import base64
import dataclasses
import json
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
    ensure_sol_balance,
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
WARP_DEPLOYER_SPEC = E2E_DIR / "fixtures" / "test-spec-warp-deployer.yml"
MINIO_SPEC = E2E_DIR / "fixtures" / "test-spec-minio.yml"
VALIDATOR_GORCHAIN_SPEC = E2E_DIR / "fixtures" / "test-spec-validator-gorchain.yml"
VALIDATOR_SOLANA_SPEC = E2E_DIR / "fixtures" / "test-spec-validator-solana.yml"
RELAYER_SPEC = E2E_DIR / "fixtures" / "test-spec-relayer.yml"
GAS_ORACLE_SPEC = E2E_DIR / "fixtures" / "test-spec-gas-oracle.yml"
MONITORING_SPEC = E2E_DIR / "fixtures" / "test-spec-monitoring.yml"
WARP_UI_SPEC = E2E_DIR / "fixtures" / "test-spec-warp-ui.yml"

# Warp routes deployed by the warp_deployment fixture. A SINGLE config-driven
# warp-deployer deployment deploys every route here, selected via the spec's
# `WARP_ROUTES` env var. Each entry's `stem` names the e2e route menu file
# (fixtures/warp-routes/<stem>.yml); `name` is the route name declared inside
# that menu and the per-route state dir. The fixture
# renders the selected menu files into the deployment's warp-routes-config
# configmap dir, injecting a freshly created+funded test SPL mint into each
# `needs_spl_mint` (collateral-origin) route. USDC stays first so
# WARP_ROUTES[0]["name"] is the USDC route consumed by the bridge tests.
WARP_ROUTES = [
    {"name": "USDC-solana-gorchain", "stem": "usdc", "needs_spl_mint": True},
    {"name": "SOL-solana-gorchain", "stem": "sol", "needs_spl_mint": False},
]

# Single config-driven warp-deployer deployment. SO names the Job
# f"{deployment_id}-job-{compose-service-stack}".
WARP_NS = "laconic-hyperlane-warp-deployer"
WARP_DEPLOYMENT_ID = "warp-deployer"
WARP_DEPLOY_DIR = DEPLOY_DIR / "hyperlane-warp-deployer"
WARP_JOB_NAME = f"{WARP_DEPLOYMENT_ID}-job-hyperlane-svm-warp-deployer"

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
        "BRIDGE_OWNER_PUBKEY":        keypairs.owner_pubkey,
        "IGP_ORACLE_PUBKEY":          keypairs.igp_oracle_pubkey,
        "IGP_BENEFICIARY_PUBKEY":     keypairs.igp_beneficiary_pubkey,
        "GORCHAIN_VALIDATOR_ADDRESS": keypairs.gorchain_validator_address,
        "SOLANA_VALIDATOR_ADDRESS":   keypairs.solana_validator_address,
        "SOLANA_RPC_URL":             "http://solana-rpc:18899",
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


def _write_warp_menu(deploy_dir: Path, test_mint: str) -> None:
    """Render the e2e route menu into the deployment's warp-routes-config
    configmap dir as JSON, injecting the e2e test SPL mint into the USDC route."""
    import json

    import yaml

    cmdir = deploy_dir / "configmaps" / "warp-routes-config"
    cmdir.mkdir(parents=True, exist_ok=True)
    menu_dir = E2E_DIR / "fixtures" / "warp-routes"
    for route in WARP_ROUTES:
        cfg = yaml.safe_load((menu_dir / f"{route['stem']}.yml").read_text())
        if route["needs_spl_mint"]:
            cfg["origin"]["token"] = test_mint
        (cmdir / f"{route['stem']}.json").write_text(json.dumps(cfg))


@pytest.fixture(scope="session")
def warp_deployment(
    deployer_deployment: DeploymentInfo,
    bridge_state_loader: BridgeStateLoader,
    keypairs: KeypairSet,
    request: pytest.FixtureRequest,
) -> Generator[dict, None, None]:
    """Deploy every configured warp route once for the entire test session.

    A SINGLE config-driven warp-deployer deployment deploys all WARP_ROUTES,
    selected via the spec's WARP_ROUTES env var. Collateral routes
    (needs_spl_mint) get a freshly created+funded test SPL mint injected into
    their route menu's origin token; native routes deploy as-is.

    Yields {"routes": {route_name: {"deployment", "namespace", "origin_token"}}}.
    """
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_warp_deploy = request.config.getoption("--skip-warp-deploy")

    deploy_dir = WARP_DEPLOY_DIR
    namespace = WARP_NS
    job_name = WARP_JOB_NAME

    try:
        if skip_warp_deploy and deployment_exists(deploy_dir):
            log.info("Reusing existing warp deployment (namespace: %s)", namespace)
            deployment = DeploymentInfo(
                deploy_dir=deploy_dir,
                deployment_id=get_deployment_id(deploy_dir),
                namespace=namespace,
            )
        else:
            if skip_warp_deploy:
                log.info("--skip-warp-deploy set but %s missing — deploying fresh", deploy_dir)

            log.info("Creating and funding test SPL token for the USDC route...")
            test_mint = _create_and_fund_spl_token(keypair_path=str(KEYS_DIR / "deployer.json"))
            log.info("USDC route origin token mint: %s", test_mint)

            log.info("Preparing warp deployer stack...")
            warp_info = deploy_prepare(
                "hyperlane-svm-warp-deployer",
                WARP_DEPLOYER_SPEC,
                deploy_dir=deploy_dir,
                namespace=namespace,
                spec_replacements=SPEC_REPLACEMENTS,
                deployment_id=WARP_DEPLOYMENT_ID,
            )

            _write_warp_menu(warp_info.deploy_dir, test_mint)
            bridge_state_loader.populate("hyperlane-svm-warp-deployer", warp_info.deploy_dir)

            os.environ.update({
                "DEPLOYER_KEYPAIR":       keypairs.deployer_keypair,
                "BRIDGE_OWNER_PUBKEY":    keypairs.owner_pubkey,
                "SOLANA_RPC_URL":         "http://solana-rpc:18899",
            })

            log.info("Starting warp deployer stack...")
            deploy_start(warp_info.deploy_dir)

            try:
                log.info("Waiting for warp deployer job to complete...")
                wait_for_job_complete(namespace, job_name, timeout=1800)
                save_job_logs(namespace, job_name)
                log.info("Warp deployer job complete, artifacts available")
            except Exception:
                save_job_logs(namespace, job_name)
                save_job_describe(namespace, job_name)
                raise

            deployment = warp_info

        # Build per-route context from each route's token-config.json written by
        # the deployer job, keyed by the known WARP_ROUTES selection.
        routes: dict[str, dict] = {}
        for route in WARP_ROUTES:
            name = route["name"]
            token_config = bridge_state_loader.read_route_token_config(name)
            # The origin collateral mint is the side carrying a "token" field;
            # a native origin has none, so this is None for the native route.
            warp_route = token_config.get("warpRoute", {})
            origin_token = next(
                (side["token"] for side in warp_route.values()
                 if isinstance(side, dict) and side.get("token")),
                None,
            )
            log.info("Route %s origin token: %s", name, origin_token)
            routes[name] = {
                "deployment": deployment,
                "namespace": namespace,
                "origin_token": origin_token,
            }

        yield {"routes": routes}
    finally:
        if not skip_cleanup:
            log.info("Stopping warp deployer stack...")
            stop_stack("hyperlane-svm-warp-deployer", deploy_dir=WARP_DEPLOY_DIR)


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
        "SOLANA_RPC_URL":          "http://solana-rpc:18899",
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
    warp_deployment: dict,
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
    # Relayer whitelist: built by the warp-deployer (build-relayer-whitelist.sh).
    # Single-quote the JSON so the embedded double quotes stay valid YAML.
    whitelist = bridge_state_loader.read_json("relayer-whitelist.json")
    content = content.replace(
        'HYP_WHITELIST: "REPLACE_AT_RUNTIME"',
        "HYP_WHITELIST: '" + json.dumps(whitelist, separators=(",", ":")) + "'",
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
        "SOLANA_RPC_URL":                 "http://solana-rpc:18899",
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


def _build_watches(keypairs: KeypairSet) -> tuple[dict, list[str]]:
    """Build a watches.json doc for e2e and return (doc, low_labels).

    Uses keypairs funded during setup. The igp-beneficiary is left UNFUNDED in
    setup, so it is the deliberate low-balance watch that must trigger a Slack
    alert; the deployer (funded) watch must stay quiet.
    """
    high, low = "1.0", "1000000.0"  # low threshold = quiet; high = guaranteed breach
    watches = {
        "watches": [
            {"chain": "gorchain", "label": "relayer", "address": keypairs.deployer_pubkey,
             "tokens": [{"symbol": "GOR", "mint": "native", "threshold": high}]},
            {"chain": "solana", "label": "relayer", "address": keypairs.deployer_pubkey,
             "tokens": [{"symbol": "SOL", "mint": "native", "threshold": high}]},
            {"chain": "solana", "label": "igp-beneficiary", "address": keypairs.igp_beneficiary_pubkey,
             "tokens": [{"symbol": "SOL", "mint": "native", "threshold": low}]},
        ]
    }
    return watches, ["igp-beneficiary"]


class _SlackCapture:
    """Threaded HTTP server capturing POSTed Slack payloads for assertions."""

    def __init__(self, port: int = 18080) -> None:
        import http.server
        import threading

        self.payloads: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    outer.payloads.append(json.loads(body))
                except ValueError:
                    pass
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):  # silence
                return

        self._server = http.server.HTTPServer(("0.0.0.0", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_SlackCapture":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()


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
            log.info("Reusing existing monitoring deployment (namespace: %s)", namespace)
            yield {
                "deployment": DeploymentInfo(
                    deploy_dir=deploy_dir, deployment_id=deployment_id, namespace=namespace,
                ),
                "namespace": namespace,
                "pod_name": pod_name,
                "low_labels": [],
                "slack_payloads": [],
                "grafana_url": GRAFANA_URL,
                "prometheus_url": PROMETHEUS_URL,
            }
            return
        log.info("--skip-monitoring-deploy set but %s missing — deploying fresh", deploy_dir)

    # Build the watch file from keypairs (no spec wallet substitution anymore).
    watches_doc, low_labels = _build_watches(keypairs)

    log.info("Preparing monitoring stack...")
    deploy_info = deploy_prepare(
        "hyperlane-monitoring", MONITORING_SPEC,
        spec_replacements=SPEC_REPLACEMENTS,
        deployment_id="monitoring",
    )

    # Write the watch file into the runtime configmap dir (monitoring has no
    # bridge_state_loader populate mapping — the watch doc is built here).
    watch_dir = deploy_info.deploy_dir / "configmaps" / "balance-monitor-config"
    watch_dir.mkdir(parents=True, exist_ok=True)
    (watch_dir / "watches.json").write_text(json.dumps(watches_doc))

    os.environ["GF_SECURITY_ADMIN_PASSWORD"] = GRAFANA_ADMIN_PASSWORD

    with _SlackCapture(port=18080) as slack:
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

        # Wait for the balance monitor to alert on the underfunded wallet.
        log.info("Waiting for balance monitor to alert on the underfunded wallet...")
        deadline = time.time() + 120
        while time.time() < deadline and not slack.payloads:
            time.sleep(3)

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
            "low_labels": low_labels,
            "slack_payloads": list(slack.payloads),
            "grafana_url": GRAFANA_URL,
            "prometheus_url": PROMETHEUS_URL,
        }

    save_pod_logs(namespace, f"app={deploy_info.deployment_id}", "monitoring")
    if not skip_cleanup:
        log.info("Stopping monitoring stack...")
        stop_stack("hyperlane-monitoring")


# ---------------------------------------------------------------------------
# Bridge transfer setup
# ---------------------------------------------------------------------------


def _read_route_synthetic_mint(bridge_state_loader: BridgeStateLoader, route: str) -> str:
    """Read the synthetic SPL mint the warp deployer emits into a route's token-config.

    The deployer queries the synthetic program and writes the synthetic side's mint
    (warpRoute.<synthetic chain>.mint); the warp-UI SDK requires this value
    explicitly (it does not auto-derive the PDA).
    """
    warp_route = bridge_state_loader.read_route_token_config(route).get("warpRoute", {})
    synthetic_mint = next(
        (
            side["mint"]
            for side in warp_route.values()
            if isinstance(side, dict) and side.get("type") == "synthetic" and side.get("mint")
        ),
        "",
    )
    assert synthetic_mint, (
        f"synthetic mint missing from route {route} token-config.json "
        "(is the warp deployer emitting it?)"
    )
    return synthetic_mint



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

    The IGP beneficiary is configured by the deployer Job at deploy time (not
    here); the dedicated keygen account stays funded so fee-claim tests can
    observe balance changes.
    """
    sender_keypair = str(KEYS_DIR / "deployer.json")

    # Resolve per-route warp program addresses and synthetic mints from each
    # route's warp-deploy-outputs state files.
    route_setups: dict[str, dict] = {}
    for name, route_ctx in warp_deployment["routes"].items():
        log.info("Resolving warp program addresses for route %s...", name)
        programs = bridge_state_loader.read_route_program_addresses(name)
        assert "solana" in programs, f"No warp program for solana in route {name}"
        assert "gorchain" in programs, f"No warp program for gorchain in route {name}"
        log.info("Route %s warp programs: %s", name, programs)

        log.info("Reading synthetic mint for route %s...", name)
        synthetic = _read_route_synthetic_mint(bridge_state_loader, name)
        log.info("Route %s synthetic mint: %s", name, synthetic)

        route_setups[name] = {
            "warp_solana": programs["solana"],
            "warp_gorchain": programs["gorchain"],
            "synthetic_mint": synthetic,
            "origin_token": route_ctx["origin_token"],
        }

    # USDC route drives the flat keys / user funding consumed by the CLI bridge
    # tests; per-route data lives under "routes".
    usdc_route = WARP_ROUTES[0]["name"]
    token_mint = route_setups[usdc_route]["origin_token"]
    synthetic_mint = route_setups[usdc_route]["synthetic_mint"]
    warp_programs = {
        "solana": route_setups[usdc_route]["warp_solana"],
        "gorchain": route_setups[usdc_route]["warp_gorchain"],
    }

    # IGP beneficiary is set by the deployer Job at deploy time
    # (IGP_BENEFICIARY_PUBKEY → keypairs.igp_beneficiary_pubkey); the dedicated
    # keygen account is still funded so fee-claim tests observe a balance bump.

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
        ensure_sol_balance(user["pubkey"], solana_rpc, 20, f"{label} (Solana)")
        ensure_sol_balance(user["pubkey"], gorchain_rpc, 20, f"{label} (Gorchain)")

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
        "routes": {
            name: {
                "warp_solana": r["warp_solana"],
                "warp_gorchain": r["warp_gorchain"],
                "synthetic_mint": r["synthetic_mint"],
            }
            for name, r in route_setups.items()
        },
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

    # Resolve mailbox addresses from deployer state files
    log.info("Resolving mailbox addresses from program-ids state files...")
    gorchain_programs = bridge_state_loader.read_program_ids("gorchain")
    solana_programs = bridge_state_loader.read_program_ids("solana")
    gorchain_mailbox = gorchain_programs["mailbox"]
    solana_mailbox = solana_programs["mailbox"]
    log.info("Mailboxes — gorchain: %s, solana: %s", gorchain_mailbox, solana_mailbox)

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
            }
            return
        log.info("--skip-warp-ui-deploy set but %s missing — deploying fresh", deploy_dir)

    # Patch the spec with runtime mailbox values
    content = WARP_UI_SPEC.read_text()
    content = content.replace(
        'GORCHAIN_MAILBOX: "REPLACE_AT_RUNTIME"',
        f'GORCHAIN_MAILBOX: "{gorchain_mailbox}"',
    )
    content = content.replace(
        'SOLANA_MAILBOX: "REPLACE_AT_RUNTIME"',
        f'SOLANA_MAILBOX: "{solana_mailbox}"',
    )
    patched_path = E2E_DIR / ".warp-ui-spec-patched.yml"
    patched_path.write_text(content)

    log.info("Preparing warp-ui stack...")
    deploy_info = deploy_prepare(
        "hyperlane-warp-ui", patched_path,
        spec_replacements=SPEC_REPLACEMENTS,
        deployment_id="warp-ui",
    )

    # warpRoutes.yaml is built by the warp-deployer (under warp-routes/); populate copies it
    # into the warp-ui-config ConfigMap dir.
    bridge_state_loader.populate("hyperlane-warp-ui", deploy_info.deploy_dir)

    log.info("Starting warp-ui stack...")
    deploy_start(deploy_info.deploy_dir)

    log.info("Waiting for warp-ui pod to be running...")
    wait_for_pod_phase(namespace, f"app={deploy_info.deployment_id}", "Running", timeout=120)
    log.info("Warp UI is running")

    # Caddy serves bridge.test via SO's http-proxy emission.
    log.info("Waiting for warp-ui to respond via Caddy ingress...")
    for _ in range(90):
        probe = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"{WARP_UI_URL}/"],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "200":
            break
        time.sleep(2)
    else:
        log.warning("Warp UI not returning 200 after 180s")
    log.info("Warp UI ingress ready at %s", WARP_UI_URL)

    yield {
        "deployment": deploy_info,
        "url": WARP_UI_URL,
        "gorchain_mailbox": gorchain_mailbox,
        "solana_mailbox": solana_mailbox,
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
