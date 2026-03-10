import base64
import dataclasses
import logging
import re
import secrets
import subprocess
import tempfile
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
    apply_host_chain_services,
    apply_rbac,
    create_kind_cluster,
    create_namespace,
    create_selfsigned_issuer,
    destroy_kind_cluster,
    install_cert_manager,
)
from lib.common import E2E_DIR, get_configmap_json, wait_for_job_complete, wait_for_pod_phase
from lib.deploy import (
    DEPLOY_DIR,
    DeploymentInfo,
    deploy_prepare,
    deploy_start,
    get_cluster_id,
    prefetch_deployer_image,
    stop_stack,
)
from lib.keygen import (
    KEYS_DIR,
    KeypairSet,
    create_deployer_secrets,
    create_minio_secrets,
    create_warp_deployer_secrets,
    fund_wallets,
    generate_test_keypairs,
)

log = logging.getLogger(__name__)

FIXTURE_SPEC = E2E_DIR / "fixtures" / "test-spec-deployer.yml"
WARP_SPEC = E2E_DIR / "fixtures" / "test-spec-warp-deployer.yml"
MINIO_SPEC = E2E_DIR / "fixtures" / "test-spec-minio.yml"


@dataclasses.dataclass
class MinioInfo:
    """Minio deployment info with credentials."""
    deployment: DeploymentInfo
    user: str
    password: str

    # Delegate common fields for convenience
    @property
    def namespace(self) -> str:
        return self.deployment.namespace

    @property
    def cluster_id(self) -> str:
        return self.deployment.cluster_id

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


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def kind_cluster(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    skip_setup = request.config.getoption("--skip-cluster-setup")
    skip_cleanup = request.config.getoption("--skip-cleanup")

    if not skip_setup:
        log.info("Creating kind cluster...")
        create_kind_cluster()
        log.info("Installing cert-manager...")
        install_cert_manager()
        log.info("Creating self-signed issuer...")
        create_selfsigned_issuer()
    else:
        log.info("Skipping cluster setup (--skip-cluster-setup)")

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

    yield

    if not skip_cleanup:
        if started_solana:
            log.info("Stopping Solana test validator...")
            stop_solana_test_validator(port=18899, name="solana")
        if not skip_setup:
            log.info("Stopping Gorchain stack...")
            stop_gorchain_stack(gorchain_deploy_dir)


@pytest.fixture(scope="session")
def deployer_image(request: pytest.FixtureRequest, kind_cluster: None) -> None:
    """Build or pre-fetch the deployer image and load it into the kind cluster."""
    if request.config.getoption("--build-from-source"):
        from lib.deploy import build_deployer_image
        log.info("Building deployer container image from source...")
        build_deployer_image()
    else:
        log.info("Pre-fetching published deployer image into kind cluster...")
        prefetch_deployer_image()


@pytest.fixture(scope="session")
def keypairs() -> KeypairSet:
    log.info("Generating test keypairs...")
    return generate_test_keypairs()


# ---------------------------------------------------------------------------
# MinIO deployment
# ---------------------------------------------------------------------------


def _recover_minio_credentials(namespace: str) -> tuple[str, str]:
    """Read minio credentials from k8s secret (for --skip-minio-deploy reuse)."""
    user = password = ""
    for field in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
        result = subprocess.run(
            ["kubectl", "get", "secret", "hyperlane-minio-secrets", "-n", namespace,
             "-o", f"jsonpath={{.data.{field}}}"],
            capture_output=True, text=True, check=True,
        )
        value = base64.b64decode(result.stdout.strip()).decode()
        if field == "MINIO_ROOT_USER":
            user = value
        else:
            password = value
    log.info("Recovered minio credentials from k8s secret")
    return user, password


@pytest.fixture(scope="session")
def minio_deployment(
    request: pytest.FixtureRequest,
    kind_cluster: None,
) -> Generator[MinioInfo, None, None]:
    """Deploy the hyperlane-minio stack.

    Self-contained: only requires a Kind cluster. Creates its own namespace
    and secrets. Uses the default cluster-id (KIND_CLUSTER_NAME), so when the
    full suite runs, all stacks share the same namespace naturally.
    """
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_minio = request.config.getoption("--skip-minio-deploy", default=False)

    if skip_minio:
        deploy_dir = DEPLOY_DIR / "hyperlane-minio"
        cluster_id = get_cluster_id(deploy_dir)
        namespace = f"laconic-{cluster_id}"
        log.info("Reusing existing minio deployment (namespace: %s)", namespace)
        user, password = _recover_minio_credentials(namespace)
        yield MinioInfo(
            deployment=DeploymentInfo(deploy_dir=deploy_dir, cluster_id=cluster_id, namespace=namespace),
            user=user,
            password=password,
        )
        return

    minio_user = f"minio-{secrets.token_hex(4)}"
    minio_password = secrets.token_hex(16)

    log.info("Preparing minio stack...")
    deploy_info = deploy_prepare("hyperlane-minio", MINIO_SPEC)
    namespace = deploy_info.namespace
    cluster_id = deploy_info.cluster_id

    # Create namespace before deploy start — secrets must exist before pods start.
    # Idempotent: no-ops if deployer_deployment already created it.
    log.info("Creating namespace %s...", namespace)
    create_namespace(namespace)

    log.info("Creating minio secrets in namespace %s...", namespace)
    create_minio_secrets(namespace, minio_user, minio_password)

    log.info("Starting minio stack...")
    deploy_start(deploy_info.deploy_dir)

    log.info("Waiting for minio pod to be running...")
    wait_for_pod_phase(namespace, f"app={cluster_id}", "Running", timeout=120)

    log.info("Waiting for minio-init job to complete...")
    job_name = f"{cluster_id}-job-hyperlane-minio-init"
    wait_for_job_complete(namespace, job_name, timeout=120)
    log.info("MinIO stack deployed and initialized")

    yield MinioInfo(deployment=deploy_info, user=minio_user, password=minio_password)

    if not skip_cleanup:
        log.info("Stopping minio stack...")
        stop_stack("hyperlane-minio")


# ---------------------------------------------------------------------------
# Core deployer deployment
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def deployer_deployment(
    request: pytest.FixtureRequest,
    deployer_image: None,
    keypairs: KeypairSet,
    kind_cluster: None,
    chain_nodes: None,
) -> Generator[DeploymentInfo, None, None]:
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_core_deploy = request.config.getoption("--skip-core-deploy")

    if skip_core_deploy:
        # Reuse existing deployment from a previous run (requires --skip-cleanup).
        # The Solana test validator runs detached (start_new_session) so it
        # survives Ctrl+C — deployed programs and funded wallets persist.
        deploy_dir = DEPLOY_DIR / "hyperlane-svm-deployer"
        cluster_id = get_cluster_id(deploy_dir)
        namespace = f"laconic-{cluster_id}"
        log.info("Reusing existing core deployment (cluster-id: %s, namespace: %s)", cluster_id, namespace)
        yield DeploymentInfo(deploy_dir=deploy_dir, cluster_id=cluster_id, namespace=namespace)
        return

    log.info("Preparing deployer stack...")
    deploy_info = deploy_prepare("hyperlane-svm-deployer", FIXTURE_SPEC)
    namespace = deploy_info.namespace

    # TODO: revisit — manually creating the namespace is a workaround because
    # laconic-so only creates it during deploy start, but we need it earlier
    # for host-chain-services, RBAC, and secrets. Consider reordering or
    # letting laconic-so handle this.
    log.info("Creating namespace %s...", namespace)
    create_namespace(namespace)

    log.info("Applying host-chain-services to namespace %s...", namespace)
    apply_host_chain_services(namespace)

    log.info("Applying RBAC to namespace %s...", namespace)
    apply_rbac(namespace)

    log.info("Creating deployer secrets...")
    create_deployer_secrets(namespace, keypairs)

    log.info("Creating warp deployer secrets...")
    create_warp_deployer_secrets(namespace, keypairs)

    log.info("Funding wallets...")
    fund_wallets(keypair_set=keypairs, gorchain_rpc="http://localhost:8899", solana_rpc="http://localhost:18899")

    log.info("Starting deployer stack...")
    deploy_start(deploy_info.deploy_dir)

    log.info("Waiting for deployer job to complete...")
    job_name = f"{deploy_info.cluster_id}-job-hyperlane-svm-deployer"
    wait_for_job_complete(namespace, job_name)
    log.info("Core deployer job complete, artifacts available")

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
    request: pytest.FixtureRequest,
) -> Generator[dict, None, None]:
    """Deploy the warp route stack once for the entire test session."""
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_warp_deploy = request.config.getoption("--skip-warp-deploy")

    if skip_warp_deploy:
        # Reuse existing warp deployment — recover token_mint from the
        # hyperlane-token-config ConfigMap written by the warp deployer job.
        namespace = deployer_deployment.namespace
        log.info("Reusing existing warp deployment (namespace: %s)", namespace)
        token_config = get_configmap_json(namespace, "hyperlane-token-config", "token-config.json")
        token_mint = token_config.get("warpRoute", {}).get("tokenMint", "")
        assert token_mint, "Cannot recover token_mint from hyperlane-token-config (is warp deployed?)"
        log.info("Recovered token mint from ConfigMap: %s", token_mint)

        deploy_dir = DEPLOY_DIR / "hyperlane-svm-warp-deployer"
        yield {
            "deployment": DeploymentInfo(
                deploy_dir=deploy_dir,
                cluster_id=deployer_deployment.cluster_id,
                namespace=namespace,
            ),
            "token_mint": token_mint,
            "namespace": namespace,
        }
        return

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
        cluster_id=deployer_deployment.cluster_id,
    )

    log.info("Starting warp deployer stack...")
    deploy_start(warp_info.deploy_dir)

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
