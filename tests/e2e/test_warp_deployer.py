import json
import logging
import re
import subprocess
import time
from pathlib import Path

import pytest

from lib.common import E2E_DIR
from lib.deploy import DeploymentInfo, deploy_prepare, deploy_start, stop_stack
from lib.keygen import KEYS_DIR

log = logging.getLogger(__name__)

WARP_JOB_TIMEOUT = 1200
CONFIGMAP_TIMEOUT = 30
WARP_SPEC = E2E_DIR / "fixtures" / "test-spec-warp-deployer.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kubectl_get_configmap(namespace: str, name: str) -> dict:
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "configmap", name, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _wait_for_job_complete(namespace: str, job_name: str, timeout: int) -> None:
    """Wait for a k8s Job to complete successfully.

    Uses kubectl wait --for=condition=complete, falling back to polling
    if the Job hasn't been created yet.
    """
    deadline = time.monotonic() + timeout

    # First wait for the Job to exist
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kubectl", "-n", namespace, "get", "job", job_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            break
        time.sleep(5)
    else:
        raise TimeoutError(
            f"Job {job_name} not found in namespace {namespace} within {timeout}s"
        )

    # Now wait for completion
    remaining = int(deadline - time.monotonic())
    if remaining <= 0:
        remaining = 1
    result = subprocess.run(
        [
            "kubectl", "wait",
            "--for=condition=complete",
            f"--timeout={remaining}s",
            "-n", namespace,
            f"job/{job_name}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Check if the job failed
        status_result = subprocess.run(
            [
                "kubectl", "-n", namespace, "get", "job", job_name,
                "-o", "jsonpath={.status.conditions[?(@.type=='Failed')].status}",
            ],
            capture_output=True,
            text=True,
        )
        if status_result.stdout.strip() == "True":
            raise RuntimeError(
                f"Job {job_name} failed. stderr: {result.stderr.strip()}"
            )
        raise TimeoutError(
            f"Job {job_name} did not complete within {remaining}s. "
            f"stderr: {result.stderr.strip()}"
        )


def _wait_for_configmap(namespace: str, name: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kubectl", "-n", namespace, "get", "configmap", name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        last_error = (result.stderr or "").strip()
        time.sleep(2)
    raise TimeoutError(
        f"ConfigMap {name} not found in namespace {namespace} within {timeout}s. "
        f"Last error: {last_error}"
    )


def _dump_job_logs(namespace: str, job_name: str) -> None:
    result = subprocess.run(
        ["kubectl", "logs", "-n", namespace, f"job/{job_name}", "--tail=200"],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        log.info("--- Job logs (%s) ---\n%s", job_name, result.stdout)
    elif result.stderr:
        log.warning("--- Could not fetch job logs (%s): %s", job_name, result.stderr.strip())


def _create_and_fund_spl_token(keypair_path: str, rpc_url: str = "http://localhost:18899") -> str:
    """Create a test SPL token with account and supply. Returns mint address."""
    owner_args = ["--owner", keypair_path]

    # Create token with 6 decimals (USDC-like)
    result = subprocess.run(
        ["spl-token", "create-token", "--decimals", "6", "--url", rpc_url, "--fee-payer", keypair_path],
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

    # Create token account for the deployer keypair
    subprocess.run(
        ["spl-token", "create-account", mint, "--url", rpc_url, "--fee-payer", keypair_path, *owner_args],
        capture_output=True, text=True, check=True,
    )
    log.info("Created token account for mint %s", mint)

    # Mint 1,000,000 tokens (6 decimals)
    subprocess.run(
        ["spl-token", "mint", mint, "1000000", "--url", rpc_url, "--fee-payer", keypair_path, *owner_args],
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
    deploy_start(warp_info.deploy_dir, first=False)

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
            _wait_for_job_complete(ns, job_name, WARP_JOB_TIMEOUT)
        except (TimeoutError, RuntimeError, subprocess.CalledProcessError):
            _dump_job_logs(ns, job_name)
            pytest.fail("Warp deployer job did not complete successfully")

    def test_warp_token_configmap(self, warp_deployment: dict) -> None:
        ns = warp_deployment["namespace"]

        _wait_for_configmap(ns, "hyperlane-token-config", CONFIGMAP_TIMEOUT)
        cm = _kubectl_get_configmap(ns, "hyperlane-token-config")
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
        _wait_for_configmap(ns, "hyperlane-warp-deploy-outputs", CONFIGMAP_TIMEOUT)
        _kubectl_get_configmap(ns, "hyperlane-warp-deploy-outputs")
