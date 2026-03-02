import json
import logging
import re
import subprocess
import time
from pathlib import Path

import pytest

from lib.common import E2E_DIR
from lib.deploy import DeploymentInfo, deploy_prepare, deploy_start, stop_stack

log = logging.getLogger(__name__)

WARP_POD_TIMEOUT = 600
CONFIGMAP_TIMEOUT = 30
WARP_SPEC = E2E_DIR / "fixtures" / "test-spec-warp-deployer.yml"


def _kubectl_get_configmap(namespace: str, name: str) -> dict:
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "configmap", name, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _wait_for_pod_phase(namespace: str, label: str, phase: str, timeout: int) -> None:
    subprocess.run(
        [
            "kubectl",
            "wait",
            "--for=jsonpath={.status.phase}=" + phase,
            "-n",
            namespace,
            "-l",
            label,
            f"--timeout={timeout}s",
            "pod",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _wait_for_configmap(namespace: str, name: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kubectl", "-n", namespace, "get", "configmap", name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        time.sleep(2)
    raise TimeoutError(f"ConfigMap {name} not found in namespace {namespace} within {timeout}s")


def _dump_pod_logs(namespace: str, label: str) -> None:
    result = subprocess.run(
        ["kubectl", "logs", "-n", namespace, "-l", label, "--tail=200"],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        log.info("--- Pod logs (%s) ---\n%s", label, result.stdout)


def _create_spl_token() -> str:
    """Create a test SPL token on the local Solana validator and return the mint address."""
    result = subprocess.run(
        ["spl-token", "create-token", "--url", "http://localhost:18899"],
        capture_output=True,
        text=True,
        check=True,
    )
    output = result.stdout + result.stderr

    match = re.search(r"Creating token (\w+)", output)
    if not match:
        match = re.search(r"Address:\s+(\w+)", output)
    if not match:
        raise RuntimeError(f"Failed to parse SPL token mint from output: {output}")

    return match.group(1)


def _patch_warp_spec(token_mint: str) -> Path:
    """Substitute the placeholder in the warp spec with the real token mint."""
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

    log.info("Creating test SPL token on Solana...")
    token_mint = _create_spl_token()
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
        try:
            _wait_for_pod_phase(ns, "app.kubernetes.io/name=warp-deployer", "Succeeded", WARP_POD_TIMEOUT)
        except subprocess.CalledProcessError:
            _dump_pod_logs(ns, "app.kubernetes.io/name=warp-deployer")
            pytest.fail("Warp deployer pod did not reach Succeeded phase")

    def test_warp_token_configmap(self, warp_deployment: dict) -> None:
        ns = warp_deployment["namespace"]
        token_mint = warp_deployment["token_mint"]

        _wait_for_configmap(ns, "hyperlane-token-config", CONFIGMAP_TIMEOUT)
        cm = _kubectl_get_configmap(ns, "hyperlane-token-config")
        raw = cm.get("data", {}).get("token-config.json", "")
        assert raw, "token-config data is empty"

        parsed = json.loads(raw)
        assert isinstance(parsed, dict), "token-config is not a JSON object"

        config_mint = parsed.get("warpRoute", {}).get("tokenMint", "")
        assert config_mint == token_mint, f"token-config has wrong token mint: expected {token_mint}, got {config_mint}"

    def test_warp_deploy_outputs(self, warp_deployment: dict) -> None:
        ns = warp_deployment["namespace"]
        _wait_for_configmap(ns, "hyperlane-warp-deploy-outputs", CONFIGMAP_TIMEOUT)
        _kubectl_get_configmap(ns, "hyperlane-warp-deploy-outputs")
