import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from lib.common import E2E_DIR, kubectl_json, wait_for_configmap, wait_for_job_complete
from lib.deploy import DeploymentInfo, deploy_prepare, deploy_start, stop_stack
from lib.keygen import KEYS_DIR

log = logging.getLogger(__name__)

WARP_JOB_TIMEOUT = 1200
CONFIGMAP_TIMEOUT = 30
WARP_SPEC = E2E_DIR / "fixtures" / "test-spec-warp-deployer.yml"


# ---------------------------------------------------------------------------
# Helpers
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


# ---------------------------------------------------------------------------
# Module-scoped fixture: deploy the warp stack once for all warp tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def warp_deployment(
    deployer_deployment: DeploymentInfo,
    request: pytest.FixtureRequest,
) -> dict:
    """Deploy the warp route stack and return context dict with deployment info and token mint."""
    skip_cleanup = request.config.getoption("--skip-cleanup")

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestWarpDeployer:
    def test_warp_deployer_completes(self, warp_deployment: dict) -> None:
        ns = warp_deployment["namespace"]
        cid = warp_deployment["deployment"].cluster_id
        job_name = f"{cid}-job-hyperlane-svm-warp-deployer"
        try:
            wait_for_job_complete(ns, job_name, WARP_JOB_TIMEOUT)
        except (TimeoutError, RuntimeError, subprocess.CalledProcessError):
            pytest.fail("Warp deployer job did not complete successfully")

    def test_warp_token_configmap(self, warp_deployment: dict) -> None:
        ns = warp_deployment["namespace"]

        wait_for_configmap(ns, "hyperlane-token-config", CONFIGMAP_TIMEOUT)
        cm = kubectl_json(["-n", ns, "get", "configmap", "hyperlane-token-config"])
        raw = cm.get("data", {}).get("token-config.json", "")
        assert raw, "token-config data is empty"

        parsed = json.loads(raw)
        assert isinstance(parsed, dict), "token-config is not a JSON object"

        # Verify the warpRoute structure contains our token mint
        warp_route = parsed.get("warpRoute")
        assert warp_route is not None, "token-config missing 'warpRoute' key"
        assert isinstance(warp_route, dict), "warpRoute is not a JSON object"

    def test_warp_deploy_outputs(self, warp_deployment: dict) -> None:
        ns = warp_deployment["namespace"]
        wait_for_configmap(ns, "hyperlane-warp-deploy-outputs", CONFIGMAP_TIMEOUT)
        kubectl_json(["-n", ns, "get", "configmap", "hyperlane-warp-deploy-outputs"])
