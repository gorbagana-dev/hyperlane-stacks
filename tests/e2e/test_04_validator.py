"""E2E tests for the hyperlane-validator stack.

Verifies validator pod health, KMS proxy connectivity, metrics endpoint,
checkpoint writing to MinIO, announcement validity, and on-chain announce tx.

Each validator (gorchain, solana) is deployed as a separate stack instance
with its own secrets and Privy wallet ID.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time

import pytest
from conftest import MinioInfo, ValidatorInfo

from lib.common import (
    CHAINS,
    PortForward,
    get_configmap_json,
    run_deployer_cli,
)
from lib.deploy import DeploymentInfo
from lib.privy_mock import (
    GORCHAIN_WALLET_ID,
    SOLANA_WALLET_ID,
    derive_h160_address,
)

log = logging.getLogger(__name__)

MC_IMAGE = "minio/mc:latest"

# Mapping from chain name to MinIO bucket and validator wallet ID
CHAIN_CONFIG = {
    "gorchain": {
        "bucket": "hyperlane-validator-gorchain",
        "wallet_id": GORCHAIN_WALLET_ID,
        "domain_id": CHAINS["gorchain"]["domain_id"],
        "rpc": CHAINS["gorchain"]["rpc"],
    },
    "solana": {
        "bucket": "hyperlane-validator-solana",
        "wallet_id": SOLANA_WALLET_ID,
        "domain_id": CHAINS["solana"]["domain_id"],
        "rpc": CHAINS["solana"]["rpc"],
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_mc_command(
    *args: str, minio: MinioInfo, host_port: int = 19000,
) -> subprocess.CompletedProcess[str]:
    """Run a minio/mc command via docker against a port-forwarded MinIO."""
    mc_args = " ".join(str(a) for a in args)
    return subprocess.run(
        [
            "docker", "run", "--rm", "--network", "host",
            "--entrypoint", "sh",
            MC_IMAGE,
            "-c",
            f"mc alias set test http://localhost:{host_port} "
            f"{minio.user} {minio.password} && {mc_args}",
        ],
        capture_output=True,
        text=True,
    )


def _find_pod_name(namespace: str, label: str) -> str:
    """Find the first pod matching a label selector."""
    return subprocess.run(
        ["kubectl", "get", "pods", "-n", namespace, "-l", label,
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _get_container_statuses(namespace: str, pod_name: str) -> dict[str, str]:
    """Get container name -> ready status from a pod."""
    result = subprocess.run(
        ["kubectl", "get", "pod", pod_name, "-n", namespace,
         "-o", "jsonpath={range .status.containerStatuses[*]}{.name}={.ready} {end}"],
        capture_output=True, text=True, check=True,
    )
    statuses = {}
    for pair in result.stdout.strip().split():
        if "=" in pair:
            name, ready = pair.split("=", 1)
            statuses[name] = ready
    return statuses


def _wait_for_minio_file(
    minio: MinioInfo, bucket: str, filename: str,
    host_port: int, timeout: int = 120,
) -> dict:
    """Wait for a JSON file to appear in MinIO and return parsed content.

    Port-forwards to the MinIO pod, polls with mc cat until the file exists,
    then returns the parsed JSON.
    """
    ns = minio.namespace
    cluster_id = minio.cluster_id
    pod_name = _find_pod_name(ns, f"app={cluster_id}")

    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        with PortForward(ns, f"pod/{pod_name}", host_port, 9000):
            result = run_mc_command(
                "mc", "cat", f"test/{bucket}/{filename}",
                minio=minio, host_port=host_port,
            )
            if result.returncode == 0 and result.stdout.strip():
                # mc alias set prints "Added `test` successfully." before
                # the mc cat output; find the JSON start.
                raw = result.stdout
                json_start = raw.find("{")
                if json_start == -1:
                    last_err = f"No JSON in output: {raw[:200]}"
                    continue
                return json.loads(raw[json_start:])
            last_err = result.stderr
        time.sleep(10)

    pytest.fail(
        f"{filename} not found in {bucket} after {timeout}s. "
        f"Last error: {last_err}"
    )


def _get_validator_h160(privy_mock: dict[str, str], wallet_id: str) -> str:
    """Derive the validator's H160 address from the Privy mock wallet keys."""
    private_key_hex = privy_mock[wallet_id]
    return derive_h160_address(private_key_hex)


# ---------------------------------------------------------------------------
# Shared test implementations
# ---------------------------------------------------------------------------


def _test_validator_pod_running(info: ValidatorInfo) -> None:
    result = subprocess.run(
        ["kubectl", "get", "pods", "-n", info.namespace,
         "-l", f"app={info.cluster_id}",
         "-o", "jsonpath={.items[0].status.phase}"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "Running", f"Expected Running, got: {result.stdout}"


def _test_both_containers_ready(info: ValidatorInfo) -> None:
    pod_name = _find_pod_name(info.namespace, f"app={info.cluster_id}")
    statuses = _get_container_statuses(info.namespace, pod_name)

    assert "validator" in statuses, f"validator container not found, got: {list(statuses.keys())}"
    assert statuses["validator"] == "true", f"validator not ready: {statuses}"
    assert "kms-proxy" in statuses, f"kms-proxy container not found, got: {list(statuses.keys())}"
    assert statuses["kms-proxy"] == "true", f"kms-proxy not ready: {statuses}"


def _test_kms_proxy_health(info: ValidatorInfo, local_port: int) -> None:
    pod_name = _find_pod_name(info.namespace, f"app={info.cluster_id}")

    with PortForward(info.namespace, f"pod/{pod_name}", local_port, 9999):
        result = subprocess.run(
            ["curl", "-sf", f"http://localhost:{local_port}/health"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"KMS proxy health check failed: {result.stderr}"
        assert result.stdout.strip() == "ok", f"Unexpected health response: {result.stdout}"


def _test_kms_proxy_recovered_key(info: ValidatorInfo) -> None:
    """KMS proxy logs show it successfully recovered the validator public key."""
    pod_name = _find_pod_name(info.namespace, f"app={info.cluster_id}")
    result = subprocess.run(
        ["kubectl", "logs", pod_name, "-c", "kms-proxy", "-n", info.namespace, "--tail=50"],
        capture_output=True, text=True, check=True,
    )
    logs = result.stdout
    assert "Recovered validator public key" in logs, (
        f"KMS proxy did not recover validator public key. Logs:\n{logs[-500:]}"
    )


def _test_validator_metrics(info: ValidatorInfo, chain: str, local_port: int) -> None:
    """Validator metrics endpoint has Hyperlane-specific metrics."""
    pod_name = _find_pod_name(info.namespace, f"app={info.cluster_id}")

    with PortForward(info.namespace, f"pod/{pod_name}", local_port, 9090):
        result = subprocess.run(
            ["curl", "-sf", f"http://localhost:{local_port}/metrics"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Metrics request failed: {result.stderr}"
        metrics = result.stdout

        # Basic Prometheus format check
        assert "# HELP" in metrics or "# TYPE" in metrics, (
            f"Response doesn't look like Prometheus metrics: {metrics[:200]}"
        )

        # Hyperlane-specific metrics should be present (block height is
        # always emitted; checkpoint metrics only appear after messages)
        assert "hyperlane_block_height" in metrics, (
            "hyperlane_block_height metric not found in metrics output"
        )
        assert "hyperlane_wallet_balance" in metrics, (
            "hyperlane_wallet_balance metric not found in metrics output"
        )


def _test_validator_logs_no_fatal(info: ValidatorInfo) -> None:
    pod_name = _find_pod_name(info.namespace, f"app={info.cluster_id}")
    result = subprocess.run(
        ["kubectl", "logs", pod_name, "-c", "validator", "-n", info.namespace, "--tail=100"],
        capture_output=True, text=True, check=True,
    )
    logs = result.stdout.upper()
    assert "FATAL" not in logs, f"FATAL found in validator logs:\n{result.stdout[-500:]}"
    assert "PANIC" not in logs, f"PANIC found in validator logs:\n{result.stdout[-500:]}"


def _test_announcement_in_minio(
    info: ValidatorInfo,
    minio: MinioInfo,
    privy_mock: dict[str, str],
    chain: str,
    host_port: int,
) -> None:
    """Validate announcement.json in MinIO has correct structure and values."""
    cfg = CHAIN_CONFIG[chain]
    announcement = _wait_for_minio_file(
        minio, cfg["bucket"], "announcement.json", host_port=host_port,
    )

    # Top-level structure
    assert "value" in announcement, f"announcement.json missing 'value': {announcement}"
    assert "signature" in announcement or "serialized_signature" in announcement, (
        f"announcement.json missing signature: {announcement}"
    )

    value = announcement["value"]

    # Validator H160 address matches the Privy mock wallet
    expected_h160 = _get_validator_h160(privy_mock, cfg["wallet_id"])
    actual_h160 = value.get("validator", "")
    assert actual_h160.lower() == expected_h160.lower(), (
        f"Validator address mismatch: expected {expected_h160}, got {actual_h160}"
    )

    # Mailbox domain matches chain config
    assert value.get("mailbox_domain") == cfg["domain_id"], (
        f"mailbox_domain mismatch: expected {cfg['domain_id']}, "
        f"got {value.get('mailbox_domain')}"
    )

    # mailbox_address is a valid hex hash
    mailbox = value.get("mailbox_address", "")
    assert mailbox.startswith("0x") and len(mailbox) == 66, (
        f"mailbox_address doesn't look like H256: {mailbox}"
    )

    # storage_location references the correct bucket and region
    storage = value.get("storage_location", "")
    assert cfg["bucket"] in storage, (
        f"storage_location doesn't reference bucket {cfg['bucket']}: {storage}"
    )
    assert "us-east-1" in storage, (
        f"storage_location doesn't reference region us-east-1: {storage}"
    )

    log.info(
        "%s: announcement valid — validator=%s, domain=%d, storage=%s",
        chain, actual_h160, value["mailbox_domain"], storage,
    )


def _test_metadata_in_minio(
    minio: MinioInfo, chain: str, host_port: int,
) -> None:
    """Validate metadata_latest.json in MinIO has expected fields."""
    cfg = CHAIN_CONFIG[chain]
    metadata = _wait_for_minio_file(
        minio, cfg["bucket"], "metadata_latest.json", host_port=host_port,
    )

    # Must have git_sha (identifies the agent build)
    assert metadata.get("git_sha"), (
        f"metadata_latest.json missing git_sha: {metadata}"
    )

    log.info("%s: metadata valid — git_sha=%s", chain, metadata["git_sha"])


def _test_announce_on_chain(
    info: ValidatorInfo,
    deployer_deployment: DeploymentInfo,
    privy_mock: dict[str, str],
    chain: str,
) -> None:
    """Verify the validator's announce transaction succeeded on-chain."""
    cfg = CHAIN_CONFIG[chain]
    ns = deployer_deployment.namespace

    # Get the validator_announce program ID from the core deployer ConfigMap
    program_ids = get_configmap_json(
        ns, "hyperlane-program-ids", f"{chain}-program-ids.json",
    )
    va_program_id = program_ids["validator_announce"]

    # Get the validator's H160 address
    validator_h160 = _get_validator_h160(privy_mock, cfg["wallet_id"])

    # Query the on-chain announce account
    result = run_deployer_cli(
        "validator-announce", "query",
        "--program-id", va_program_id,
        validator_h160,
        rpc=cfg["rpc"],
    )

    output = result.stdout + result.stderr
    log.info("%s: validator-announce query output:\n%s", chain, output[:2000])

    assert result.returncode == 0, (
        f"validator-announce query failed: {output}"
    )
    assert "not yet announced" not in output.lower(), (
        f"{chain}: Validator {validator_h160} has not announced. Output: {output}"
    )

    # The output should contain the storage location
    assert cfg["bucket"] in output, (
        f"{chain}: announce output doesn't reference bucket {cfg['bucket']}: {output}"
    )

    log.info("%s: on-chain announce verified for %s", chain, validator_h160)


# ---------------------------------------------------------------------------
# Tests — Gorchain validator
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestValidatorGorchain:
    """Tests for the gorchain hyperlane-validator stack."""

    def test_validator_pod_running(self, validator_gorchain: ValidatorInfo) -> None:
        """Validator pod reaches Running phase."""
        _test_validator_pod_running(validator_gorchain)

    def test_both_containers_ready(self, validator_gorchain: ValidatorInfo) -> None:
        """Both validator and kms-proxy containers are ready."""
        _test_both_containers_ready(validator_gorchain)

    def test_kms_proxy_health(self, validator_gorchain: ValidatorInfo) -> None:
        """KMS proxy health endpoint responds on :9999."""
        _test_kms_proxy_health(validator_gorchain, local_port=19999)

    def test_kms_proxy_recovered_key(self, validator_gorchain: ValidatorInfo) -> None:
        """KMS proxy logs show successful key recovery."""
        _test_kms_proxy_recovered_key(validator_gorchain)

    def test_validator_metrics(self, validator_gorchain: ValidatorInfo) -> None:
        """Validator metrics endpoint has Hyperlane-specific metrics."""
        _test_validator_metrics(validator_gorchain, "gorchain", local_port=19090)

    def test_validator_logs_no_fatal(self, validator_gorchain: ValidatorInfo) -> None:
        """Validator logs contain no FATAL or PANIC entries."""
        _test_validator_logs_no_fatal(validator_gorchain)

    def test_announcement_valid(
        self, validator_gorchain: ValidatorInfo,
        minio_deployment: MinioInfo, privy_mock: dict[str, str],
    ) -> None:
        """announcement.json in MinIO has correct validator, domain, and storage."""
        _test_announcement_in_minio(
            validator_gorchain, minio_deployment, privy_mock,
            chain="gorchain", host_port=19002,
        )

    def test_metadata_valid(
        self, validator_gorchain: ValidatorInfo, minio_deployment: MinioInfo,
    ) -> None:
        """metadata_latest.json in MinIO has expected fields."""
        _test_metadata_in_minio(minio_deployment, "gorchain", host_port=19002)

    def test_announce_on_chain(
        self, validator_gorchain: ValidatorInfo,
        deployer_deployment: DeploymentInfo,
        privy_mock: dict[str, str],
    ) -> None:
        """Validator announce tx succeeded on-chain (queried via sealevel-client)."""
        _test_announce_on_chain(
            validator_gorchain, deployer_deployment, privy_mock, chain="gorchain",
        )


# ---------------------------------------------------------------------------
# Tests — Solana validator
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestValidatorSolana:
    """Tests for the solana hyperlane-validator stack."""

    def test_validator_pod_running(self, validator_solana: ValidatorInfo) -> None:
        """Validator pod reaches Running phase."""
        _test_validator_pod_running(validator_solana)

    def test_both_containers_ready(self, validator_solana: ValidatorInfo) -> None:
        """Both validator and kms-proxy containers are ready."""
        _test_both_containers_ready(validator_solana)

    def test_kms_proxy_health(self, validator_solana: ValidatorInfo) -> None:
        """KMS proxy health endpoint responds on :9999."""
        _test_kms_proxy_health(validator_solana, local_port=19998)

    def test_kms_proxy_recovered_key(self, validator_solana: ValidatorInfo) -> None:
        """KMS proxy logs show successful key recovery."""
        _test_kms_proxy_recovered_key(validator_solana)

    def test_validator_metrics(self, validator_solana: ValidatorInfo) -> None:
        """Validator metrics endpoint has Hyperlane-specific metrics."""
        _test_validator_metrics(validator_solana, "solana", local_port=19091)

    def test_validator_logs_no_fatal(self, validator_solana: ValidatorInfo) -> None:
        """Validator logs contain no FATAL or PANIC entries."""
        _test_validator_logs_no_fatal(validator_solana)

    def test_announcement_valid(
        self, validator_solana: ValidatorInfo,
        minio_deployment: MinioInfo, privy_mock: dict[str, str],
    ) -> None:
        """announcement.json in MinIO has correct validator, domain, and storage."""
        _test_announcement_in_minio(
            validator_solana, minio_deployment, privy_mock,
            chain="solana", host_port=19003,
        )

    def test_metadata_valid(
        self, validator_solana: ValidatorInfo, minio_deployment: MinioInfo,
    ) -> None:
        """metadata_latest.json in MinIO has expected fields."""
        _test_metadata_in_minio(minio_deployment, "solana", host_port=19003)

    def test_announce_on_chain(
        self, validator_solana: ValidatorInfo,
        deployer_deployment: DeploymentInfo,
        privy_mock: dict[str, str],
    ) -> None:
        """Validator announce tx succeeded on-chain (queried via sealevel-client)."""
        _test_announce_on_chain(
            validator_solana, deployer_deployment, privy_mock, chain="solana",
        )
