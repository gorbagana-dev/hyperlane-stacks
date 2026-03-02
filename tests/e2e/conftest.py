import logging
from collections.abc import Generator

import pytest

from lib.chain import start_solana_test_validator, stop_solana_test_validator
from lib.cluster import (
    apply_host_chain_services,
    apply_rbac,
    create_kind_cluster,
    create_selfsigned_issuer,
    destroy_kind_cluster,
    install_cert_manager,
)
from lib.common import E2E_DIR
from lib.deploy import (
    DeploymentInfo,
    deploy_prepare,
    deploy_start,
    stop_stack,
)
from lib.keygen import (
    KeypairSet,
    create_deployer_secrets,
    create_warp_deployer_secrets,
    fund_wallets,
    generate_test_keypairs,
)

log = logging.getLogger(__name__)

FIXTURE_SPEC = E2E_DIR / "fixtures" / "test-spec-deployer.yml"


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
        "--build-from-source", action="store_true", default=False, help="Build container images from source instead of using published images"
    )
    parser.addoption("--skip-cleanup", action="store_true", default=False, help="Don't tear down after tests")


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

    if not skip_setup:
        log.info("Starting Solana test validator...")
        proc = start_solana_test_validator()
    else:
        log.info("Skipping chain setup (--skip-chain-setup)")
        proc = None

    yield

    if not skip_cleanup and proc is not None:
        log.info("Stopping Solana test validator...")
        stop_solana_test_validator(proc)


@pytest.fixture(scope="session")
def deployer_image(request: pytest.FixtureRequest) -> None:
    """Build container images from source if --build-from-source is passed."""
    if request.config.getoption("--build-from-source"):
        from lib.deploy import build_deployer_image
        log.info("Building deployer container image from source...")
        build_deployer_image()
    else:
        log.info("Using published deployer image (pass --build-from-source to build locally)")


@pytest.fixture(scope="session")
def keypairs() -> KeypairSet:
    log.info("Generating test keypairs...")
    return generate_test_keypairs()


@pytest.fixture(scope="session")
def deployer_deployment(
    request: pytest.FixtureRequest,
    deployer_image: None,
    keypairs: KeypairSet,
    kind_cluster: None,
    chain_nodes: None,
) -> Generator[DeploymentInfo, None, None]:
    skip_cleanup = request.config.getoption("--skip-cleanup")

    log.info("Preparing deployer stack...")
    deploy_info = deploy_prepare("hyperlane-svm-deployer", FIXTURE_SPEC)
    namespace = deploy_info.namespace

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
    deploy_start(deploy_info.deploy_dir, first=True)

    yield deploy_info

    if not skip_cleanup:
        log.info("Stopping deployer stack...")
        stop_stack("hyperlane-svm-deployer")
