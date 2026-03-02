"""Shared utilities for Hyperlane e2e tests."""

from __future__ import annotations

import json
import logging
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
    _logger.info(msg)


def log_error(msg: str) -> None:
    _logger.error(msg)


def log_pass(msg: str) -> None:
    _logger.log(PASS_LEVEL, msg)


def log_fail(msg: str) -> None:
    _logger.log(FAIL_LEVEL, msg)


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
# Verbose mode — when True, subprocess output streams to terminal
# ---------------------------------------------------------------------------
VERBOSE = False


def set_verbose(enabled: bool) -> None:
    global VERBOSE  # noqa: PLW0603
    VERBOSE = enabled


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------
def run_cmd(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    if not quiet:
        log_info(f"Running: {' '.join(str(a) for a in args)}")

    # Default: capture output unless verbose mode is on
    if capture_output is None:
        capture_output = not VERBOSE

    try:
        if capture_output:
            result = subprocess.run(
                args,
                check=check,
                capture_output=True,
                text=True,
                cwd=cwd,
                env=env,
            )
        else:
            # Stream to the real terminal fds, bypassing pytest's capture.
            # sys.__stdout__/__stderr__ are the original streams before
            # pytest redirects sys.stdout/sys.stderr.
            result = subprocess.run(
                args,
                check=check,
                stdout=sys.__stdout__,
                stderr=sys.__stderr__,
                text=True,
                cwd=cwd,
                env=env,
            )
        return result
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
        current_phase = result.stdout.strip()

        if current_phase == phase:
            log_info(f"Pod ({label_selector}) reached phase: {phase}")
            return

        if current_phase == "Failed":
            log_error(f"Pod ({label_selector}) entered Failed phase")
            run_cmd(
                ["kubectl", "logs", "-n", namespace, "-l", label_selector, "--tail=50"],
                check=False,
                quiet=True,
            )
            raise RuntimeError(f"Pod ({label_selector}) entered Failed phase")

        if time.time() >= deadline:
            log_error(f"Timed out waiting for pod ({label_selector}) — current phase: {current_phase}")
            run_cmd(
                ["kubectl", "describe", "pods", "-n", namespace, "-l", label_selector],
                check=False,
                quiet=True,
            )
            raise TimeoutError(
                f"Timed out after {timeout}s waiting for pod ({label_selector}) "
                f"to reach {phase}, current: {current_phase}"
            )

        time.sleep(5)


def wait_for_rpc_health(url: str, timeout: int = 120) -> None:
    log_info(f"Waiting for RPC health at {url} (timeout {timeout}s)...")

    def _check() -> bool:
        result = run_cmd(
            ["curl", "-sf", url, "-o", "/dev/null"],
            check=False,
            quiet=True,
        )
        return result.returncode == 0

    wait_for(_check, timeout=timeout, interval=5, description=f"RPC health at {url}")
    log_info(f"RPC at {url} is healthy")


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
    cluster_id: str | None = None,
) -> str:
    if deploy_namespace:
        return deploy_namespace
    if cluster_id:
        return f"laconic-{cluster_id}"
    fail_exit("Neither deploy_namespace nor cluster_id is provided")
    return ""  # unreachable, keeps type checker happy
