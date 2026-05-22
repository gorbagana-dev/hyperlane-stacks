"""Shared utilities for Hyperlane e2e tests."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
LIB_DIR = Path(__file__).resolve().parent
E2E_DIR = LIB_DIR.parent
REPO_ROOT = E2E_DIR.parent.parent

# ---------------------------------------------------------------------------
# Logging with colored output
# ---------------------------------------------------------------------------
_BLUE = "\033[0;34m"
_RED = "\033[0;31m"
_GREEN = "\033[0;32m"
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        "INFO": _BLUE,
        "ERROR": _RED,
        "PASS": _GREEN,
        "FAIL": _RED,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelname, _RESET)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return f"{color}[{ts}] [{record.levelname}]{_RESET} {record.getMessage()}"


# Custom log levels for PASS / FAIL
PASS_LEVEL = 25
FAIL_LEVEL = 35
logging.addLevelName(PASS_LEVEL, "PASS")
logging.addLevelName(FAIL_LEVEL, "FAIL")

_logger = logging.getLogger("e2e")
_logger.setLevel(logging.DEBUG)

if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(_ColorFormatter())
    _logger.addHandler(_handler)


def log_info(msg: str) -> None:
    _logger.info(msg, stacklevel=2)


def log_error(msg: str) -> None:
    _logger.error(msg, stacklevel=2)


def log_pass(msg: str) -> None:
    _logger.log(PASS_LEVEL, msg, stacklevel=2)


def log_fail(msg: str) -> None:
    _logger.log(FAIL_LEVEL, msg, stacklevel=2)


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
class AssertionError(Exception):
    pass


def fail_exit(msg: str) -> None:
    log_error(msg)
    sys.exit(1)


def assert_eq(expected: str, actual: str, msg: str = "assert_eq failed") -> None:
    if expected != actual:
        log_fail(f"{msg}: expected '{expected}', got '{actual}'")
        raise AssertionError(f"{msg}: expected '{expected}', got '{actual}'")


def assert_not_empty(value: str, msg: str = "assert_not_empty failed") -> None:
    if not value:
        log_fail(f"{msg}: value is empty")
        raise AssertionError(f"{msg}: value is empty")


def assert_contains(haystack: str, needle: str, msg: str = "assert_contains failed") -> None:
    if needle not in haystack:
        log_fail(f"{msg}: '{haystack}' does not contain '{needle}'")
        raise AssertionError(f"{msg}: '{haystack}' does not contain '{needle}'")


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def force_rmtree(path: Path) -> None:
    """Remove a directory tree, using sudo if normal removal fails.

    Docker containers (via laconic-so) create root-owned files inside
    deployment directories. Plain shutil.rmtree fails on those.
    """
    import shutil

    try:
        shutil.rmtree(path)
    except PermissionError:
        log_info(f"Permission denied removing {path}, retrying with sudo...")
        subprocess.run(["sudo", "rm", "-rf", str(path)], check=True)


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = ("KEYPAIR=", "PRIVATE_KEY=", "SECRET=")


def _redact_args(args: list[str]) -> str:
    """Join args for logging, redacting values that look like secrets."""
    parts = []
    for a in args:
        s = str(a)
        for pat in _SECRET_PATTERNS:
            if pat in s:
                idx = s.index(pat) + len(pat)
                s = s[:idx] + "<REDACTED>"
                break
        parts.append(s)
    return " ".join(parts)


def run_cmd(
    args: list[str],
    *,
    check: bool = True,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    quiet: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if not quiet:
        _logger.info("Running: %s", _redact_args(args), stacklevel=2)

    try:
        return subprocess.run(
            args,
            check=check,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            input=input_text,
        )
    except subprocess.CalledProcessError as exc:
        log_error(f"Command failed (rc={exc.returncode}): {' '.join(str(a) for a in args)}")
        if exc.stdout:
            log_error(f"stdout: {exc.stdout[-2000:]}")
        if exc.stderr:
            log_error(f"stderr: {exc.stderr[-2000:]}")
        raise


# ---------------------------------------------------------------------------
# Wait helpers
# ---------------------------------------------------------------------------
def wait_for(
    fn: Callable[[], Any],
    timeout: int = 120,
    interval: int = 5,
    description: str = "",
) -> Any:
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while True:
        try:
            result = fn()
            if result:
                return result
        except Exception as exc:
            last_exc = exc

        if time.time() >= deadline:
            desc = description or "condition"
            msg = f"Timed out after {timeout}s waiting for: {desc}"
            if last_exc:
                msg += f" (last error: {last_exc})"
            log_error(msg)
            raise TimeoutError(msg)

        time.sleep(interval)


def wait_for_pod_phase(
    namespace: str,
    label_selector: str,
    phase: str,
    timeout: int = 600,
) -> None:
    log_info(f"Waiting for pod ({label_selector}) in {namespace} to reach phase {phase} (timeout {timeout}s)...")

    deadline = time.time() + timeout
    last_kubectl_error = ""
    while True:
        result = run_cmd(
            [
                "kubectl",
                "get",
                "pods",
                "-n",
                namespace,
                "-l",
                label_selector,
                "-o",
                "jsonpath={.items[0].status.phase}",
            ],
            check=False,
            quiet=True,
        )

        if result.returncode != 0:
            last_kubectl_error = (result.stderr or result.stdout or "").strip()
            if time.time() >= deadline:
                raise TimeoutError(
                    f"Timed out after {timeout}s waiting for pod ({label_selector}). "
                    f"kubectl kept failing: {last_kubectl_error}"
                )
            time.sleep(5)
            continue

        current_phase = result.stdout.strip()

        if current_phase == phase:
            log_info(f"Pod ({label_selector}) reached phase: {phase}")
            return

        if current_phase == "Failed":
            log_error(f"Pod ({label_selector}) entered Failed phase")
            logs = run_cmd(
                ["kubectl", "logs", "-n", namespace, "-l", label_selector, "--tail=50"],
                check=False,
                quiet=True,
            )
            if logs.stdout:
                log_error(f"Pod logs:\n{logs.stdout}")
            raise RuntimeError(f"Pod ({label_selector}) entered Failed phase")

        if time.time() >= deadline:
            log_error(f"Timed out waiting for pod ({label_selector}) — current phase: {current_phase}")
            describe = run_cmd(
                ["kubectl", "describe", "pods", "-n", namespace, "-l", label_selector],
                check=False,
                quiet=True,
            )
            if describe.stdout:
                log_error(f"Pod describe:\n{describe.stdout}")
            raise TimeoutError(
                f"Timed out after {timeout}s waiting for pod ({label_selector}) "
                f"to reach {phase}, current: {current_phase}"
            )

        time.sleep(5)


def wait_for_rpc_health(url: str, timeout: int = 120) -> None:
    log_info(f"Waiting for RPC health at {url} (timeout {timeout}s)...")

    health_url = url.rstrip("/") + "/health"

    def _check() -> bool:
        result = run_cmd(
            ["curl", "-sf", health_url, "-o", "/dev/null"],
            check=False,
            quiet=True,
        )
        return result.returncode == 0

    wait_for(_check, timeout=timeout, interval=5, description=f"RPC health at {health_url}")
    log_info(f"RPC at {url} is healthy")


def wait_for_rpc_accounts_ready(url: str, timeout: int = 120) -> None:
    """Wait until the RPC node can serve account queries (ledger replay done).

    agave-validator returns /health OK while still replaying its ledger.
    Polling getAccountInfo for the system program confirms that accounts are
    actually queryable before tests try to look up deployed programs.
    """
    import json as _json

    system_program = "11111111111111111111111111111111"
    payload = _json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getAccountInfo",
        "params": [system_program, {"encoding": "base64"}],
    })

    def _check() -> bool:
        result = run_cmd(
            ["curl", "-sf", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", payload, url],
            check=False, quiet=True,
        )
        if result.returncode != 0:
            return False
        try:
            resp = _json.loads(result.stdout)
            return resp.get("result", {}).get("value") is not None
        except Exception:
            return False

    wait_for(_check, timeout=timeout, interval=5, description=f"RPC accounts ready at {url}")
    log_info(f"RPC at {url} is serving accounts")


def wait_for_configmap(namespace: str, name: str, timeout: int = 120) -> None:
    log_info(f"Waiting for ConfigMap {name} in {namespace} (timeout {timeout}s)...")

    def _check() -> bool:
        result = run_cmd(
            [
                "kubectl",
                "get",
                "configmap",
                name,
                "-n",
                namespace,
                "-o",
                "jsonpath={.data}",
            ],
            check=False,
            quiet=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    wait_for(_check, timeout=timeout, interval=5, description=f"ConfigMap {name}")
    log_info(f"ConfigMap {name} exists and has data")


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------
def wait_for_job_complete(namespace: str, job_name: str, timeout: int = 1200) -> None:
    """Wait for a k8s Job to complete successfully.

    Polls until the Job resource exists, then uses ``kubectl wait``
    for the ``complete`` condition. Raises on failure or timeout.
    """
    log_info(f"Waiting for Job {job_name} in {namespace} (timeout {timeout}s)...")
    deadline = time.monotonic() + timeout

    # Wait for the Job to exist
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kubectl", "-n", namespace, "get", "job", job_name],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            break
        time.sleep(5)
    else:
        raise TimeoutError(f"Job {job_name} not found in namespace {namespace} within {timeout}s")

    # Wait for completion
    remaining = max(int(deadline - time.monotonic()), 1)
    result = subprocess.run(
        [
            "kubectl", "wait", "--for=condition=complete",
            f"--timeout={remaining}s", "-n", namespace, f"job/{job_name}",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        status_result = subprocess.run(
            [
                "kubectl", "-n", namespace, "get", "job", job_name,
                "-o", "jsonpath={.status.conditions[?(@.type=='Failed')].status}",
            ],
            capture_output=True, text=True,
        )
        if status_result.stdout.strip() == "True":
            dump_job_logs(namespace, job_name)
            raise RuntimeError(f"Job {job_name} failed. stderr: {result.stderr.strip()}")
        dump_job_logs(namespace, job_name)
        raise TimeoutError(
            f"Job {job_name} did not complete within {remaining}s. stderr: {result.stderr.strip()}"
        )
    log_info(f"Job {job_name} completed successfully")


def dump_job_logs(namespace: str, job_name: str) -> None:
    """Dump the last 200 lines of a Job's pod logs."""
    result = subprocess.run(
        ["kubectl", "logs", "-n", namespace, f"job/{job_name}", "--tail=200"],
        capture_output=True, text=True,
    )
    if result.stdout:
        log_info(f"--- Job logs ({job_name}) ---\n{result.stdout}")
    elif result.stderr:
        log_error(f"Could not fetch job logs ({job_name}): {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Log collection
# ---------------------------------------------------------------------------
LOGS_DIR = E2E_DIR / ".logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def save_job_logs(namespace: str, job_name: str) -> None:
    """Save all container logs for a completed Job to .logs/."""
    result = subprocess.run(
        ["kubectl", "logs", "-n", namespace, f"job/{job_name}", "--all-containers"],
        capture_output=True, text=True,
    )
    if result.stdout:
        out = LOGS_DIR / f"job_{job_name}.log"
        out.write_text(result.stdout)
        log_info(f"Job logs saved to {out}")


def save_pod_logs(namespace: str, label_selector: str, name: str) -> None:
    """Save all container logs for pods matching a label selector to .logs/.

    Each container gets its own file: .logs/{name}_{container}.log
    """
    # Get pod names
    result = subprocess.run(
        ["kubectl", "get", "pods", "-n", namespace, "-l", label_selector,
         "-o", "jsonpath={.items[*].metadata.name}"],
        capture_output=True, text=True,
    )
    if not result.stdout.strip():
        return

    for pod_name in result.stdout.strip().split():
        # Get container names
        for container_key in ("containers", "initContainers"):
            jsonpath = f"{{.spec.{container_key}[*].name}}"
            containers_result = subprocess.run(
                ["kubectl", "get", "pod", pod_name, "-n", namespace,
                 "-o", f"jsonpath={jsonpath}"],
                capture_output=True, text=True,
            )
            if not containers_result.stdout.strip():
                continue
            for container in containers_result.stdout.strip().split():
                prefix = "init_" if container_key == "initContainers" else ""
                log_result = subprocess.run(
                    ["kubectl", "logs", "-n", namespace, pod_name, "-c", container],
                    capture_output=True, text=True,
                )
                if log_result.stdout:
                    out = LOGS_DIR / f"{name}_{prefix}{container}.log"
                    out.write_text(log_result.stdout)

    log_info(f"Pod logs for {name} saved to {LOGS_DIR}")


def save_pod_describe(namespace: str, label_selector: str, name: str) -> None:
    """Save ``kubectl describe`` output for pods matching a label selector."""
    result = subprocess.run(
        ["kubectl", "describe", "pods", "-n", namespace, "-l", label_selector],
        capture_output=True, text=True,
    )
    if result.stdout:
        out = LOGS_DIR / f"{name}_describe.txt"
        out.write_text(result.stdout)
        log_info(f"Pod describe for {name} saved to {out}")


def save_job_describe(namespace: str, job_name: str) -> None:
    """Save ``kubectl describe`` output for a Job and its pods."""
    # Describe the Job itself
    result = subprocess.run(
        ["kubectl", "describe", "job", job_name, "-n", namespace],
        capture_output=True, text=True,
    )
    parts = []
    if result.stdout:
        parts.append(result.stdout)

    # Describe the Job's pods (labeled job-name=<job_name>)
    pod_result = subprocess.run(
        ["kubectl", "describe", "pods", "-n", namespace, "-l", f"job-name={job_name}"],
        capture_output=True, text=True,
    )
    if pod_result.stdout:
        parts.append(pod_result.stdout)

    if parts:
        out = LOGS_DIR / f"job_{job_name}_describe.txt"
        out.write_text("\n---\n".join(parts))
        log_info(f"Job describe for {job_name} saved to {out}")


# ---------------------------------------------------------------------------
# kubectl helper
# ---------------------------------------------------------------------------
def kubectl_json(args: list[str]) -> dict[str, Any]:
    result = run_cmd(["kubectl"] + args + ["-o", "json"])
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Namespace helper
# ---------------------------------------------------------------------------
def get_namespace(
    deploy_namespace: str | None = None,
    deployment_id: str | None = None,
) -> str:
    if deploy_namespace:
        return deploy_namespace
    if deployment_id:
        return f"laconic-{deployment_id}"
    fail_exit("Neither deploy_namespace nor deployment_id is provided")
    return ""  # unreachable, keeps type checker happy


# ---------------------------------------------------------------------------
# Chain config (shared across all test modules)
# ---------------------------------------------------------------------------
CHAINS = {
    "gorchain": {"domain_id": 99999, "rpc": "http://localhost:8899"},
    # Use 127.0.0.1 (not localhost) so the Hyperlane SDK's hostname-based
    # chain detection can distinguish solana from gorchain. The SDK extracts
    # just the hostname from the wallet's RPC URL and does a substring match
    # against chain metadata — both chains on "localhost" are ambiguous.
    "solana": {"domain_id": 99998, "rpc": "http://127.0.0.1:18899"},
}

CONFIGMAP_TIMEOUT = 30


# ---------------------------------------------------------------------------
# ConfigMap helpers
# ---------------------------------------------------------------------------
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def is_base58_pubkey(value: str) -> bool:
    """Check if a string looks like a valid Solana base58 public key."""
    return bool(_BASE58_RE.match(value))


def get_configmap_data(namespace: str, name: str) -> dict[str, str]:
    """Fetch a ConfigMap and return its .data dict."""
    cm = kubectl_json(["-n", namespace, "get", "configmap", name])
    return cm.get("data", {})


def get_configmap_json(namespace: str, name: str, key: str) -> Any:
    """Fetch a ConfigMap, parse one data key as JSON."""
    data = get_configmap_data(namespace, name)
    raw = data.get(key, "")
    assert raw, f"ConfigMap {name} key '{key}' is empty"
    return json.loads(raw)


# ---------------------------------------------------------------------------
# On-chain verification helpers
# ---------------------------------------------------------------------------
def assert_program_on_chain(
    chain_name: str,
    rpc: str,
    addr: str,
    *,
    label: str = "",
    keypair_path: str | None = None,
) -> None:
    """Assert a Solana program exists on-chain via ``solana program show``."""
    from .keygen import KEYS_DIR

    keypair = keypair_path or str(KEYS_DIR / "deployer.json")
    display = f"{label} ({addr})" if label else addr

    result = run_cmd(
        [
            "solana", "program", "show", addr,
            "--url", rpc,
            "--keypair", keypair,
            "--output", "json",
        ],
        check=False,
        quiet=True,
    )
    assert result.returncode == 0, (
        f"{chain_name}: program {display} not found on-chain. "
        f"stderr: {result.stderr.strip()}"
    )
    log_info(f"{chain_name}: {display} verified on-chain")


class PortForward:
    """Context manager for kubectl port-forward."""

    def __init__(self, namespace: str, target: str, local_port: int, remote_port: int):
        self.namespace = namespace
        self.target = target
        self.local_port = local_port
        self.remote_port = remote_port
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> PortForward:
        self.proc = subprocess.Popen(
            [
                "kubectl", "port-forward",
                "-n", self.namespace,
                self.target,
                f"{self.local_port}:{self.remote_port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Wait for port-forward to establish
        time.sleep(3)
        if self.proc.poll() is not None:
            stderr = self.proc.stderr.read().decode() if self.proc.stderr else ""
            raise RuntimeError(f"port-forward failed to start: {stderr}")
        return self

    def __exit__(self, *exc) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)


def run_deployer_cli(
    *args: str,
    keypair_path: str | None = None,
    rpc: str,
) -> subprocess.CompletedProcess[str]:
    """Run a ``hyperlane-sealevel-client`` command via docker run.

    Uses the deployer image with ``--network host`` so it can reach local
    RPC endpoints. Creates a minimal Solana CLI config inside the container
    (the client unconditionally loads it even when --keypair is set).
    Returns the CompletedProcess (caller should check returncode).
    """
    from .deploy import DEPLOYER_IMAGE
    from .keygen import KEYS_DIR

    keypair = keypair_path or str(KEYS_DIR / "deployer.json")
    cli_args = " ".join(str(a) for a in args)

    # The sealevel client always loads /root/.config/solana/cli/config.yml
    # at startup (even when --keypair and --url are explicit). Write a
    # minimal config so the container doesn't panic on missing file.
    setup = (
        "mkdir -p /root/.config/solana/cli && "
        "printf 'json_rpc_url: \"%s\"\\nwebsocket_url: \"\"\\n"
        "keypair_path: \"/tmp/key.json\"\\ncommitment: finalized\\n' "
        f"'{rpc}' > /root/.config/solana/cli/config.yml"
    )
    shell_cmd = f"{setup} && hyperlane-sealevel-client --keypair /tmp/key.json --url {rpc} {cli_args}"

    return run_cmd(
        [
            "docker", "run", "--rm", "--network", "host",
            "--entrypoint", "sh",
            "-v", f"{keypair}:/tmp/key.json:ro",
            DEPLOYER_IMAGE,
            "-c", shell_cmd,
        ],
        check=False,
    )


# ---------------------------------------------------------------------------
# SPL token balance helpers
# ---------------------------------------------------------------------------


def get_spl_token_balance(
    mint: str,
    owner_keypair: str,
    rpc_url: str,
) -> float:
    """Query SPL token balance for a mint. Returns 0.0 if no token account exists."""
    result = run_cmd(
        [
            "spl-token", "balance", mint,
            "--owner", owner_keypair,
            "--url", rpc_url,
        ],
        check=False,
        quiet=True,
    )
    if result.returncode != 0:
        # "Could not find any SPL Token account" → balance is 0
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def wait_for_token_balance(
    mint: str,
    owner_keypair: str,
    rpc_url: str,
    expected_min: float,
    *,
    timeout: int = 120,
    poll_interval: int = 5,
    label: str = "",
) -> float:
    """Poll SPL token balance until it reaches *expected_min* or timeout.

    Returns the final observed balance. Raises AssertionError on timeout.
    """
    display = label or mint[:12]
    deadline = time.time() + timeout
    last_balance = 0.0

    while time.time() < deadline:
        last_balance = get_spl_token_balance(mint, owner_keypair, rpc_url)
        if last_balance >= expected_min:
            log_info(f"{display}: balance reached {last_balance}")
            return last_balance
        remaining = int(deadline - time.time())
        log_info(f"{display}: balance {last_balance}, waiting for {expected_min} ({remaining}s remaining)")
        time.sleep(poll_interval)

    raise AssertionError(
        f"{display}: balance {last_balance} did not reach {expected_min} "
        f"within {timeout}s. Check validator and relayer logs."
    )
