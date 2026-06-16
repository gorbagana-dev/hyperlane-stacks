"""Unit tests for the balance-monitor script (no cluster/network).

Imports the script by path (it ships as a ConfigMap file, not a package) and
exercises the pure functions: the alert-decision state machine and the Slack
message builders.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "stack_orchestrator/data/config/balance-monitor-scripts-config/check-balance.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("check_balance", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cb():
    return _load_module()


def _watches():
    return [
        {"chain": "solana", "label": "relayer", "address": "AAA",
         "tokens": [{"symbol": "SOL", "mint": "native", "threshold": 5.0},
                    {"symbol": "USDC", "mint": "MINT", "threshold": 100.0}]},
    ]


def test_breach_then_no_repeat_then_repeat(cb):
    state = {}
    # SOL below (1.0 < 5.0), USDC ok (200 >= 100)
    def bal(chain, addr, mint):
        return 1.0 if mint == "native" else 200.0

    breaches, recoveries = cb.evaluate_cycle(_watches(), bal, state, now=0.0, repeat=100.0)
    assert [b["symbol"] for b in breaches] == ["SOL"]
    assert recoveries == []

    # Still low, within repeat window -> no new breach
    breaches, _ = cb.evaluate_cycle(_watches(), bal, state, now=50.0, repeat=100.0)
    assert breaches == []

    # Still low, past repeat window -> re-alert
    breaches, _ = cb.evaluate_cycle(_watches(), bal, state, now=150.0, repeat=100.0)
    assert [b["symbol"] for b in breaches] == ["SOL"]


def test_recovery_emitted_once(cb):
    state = {}
    def low(c, a, m):
        return 1.0 if m == "native" else 200.0
    cb.evaluate_cycle(_watches(), low, state, now=0.0, repeat=100.0)

    def high(c, a, m):
        return 10.0 if m == "native" else 200.0
    _, recoveries = cb.evaluate_cycle(_watches(), high, state, now=1.0, repeat=100.0)
    assert [r["symbol"] for r in recoveries] == ["SOL"]

    # No duplicate recovery on the next ok cycle
    _, recoveries = cb.evaluate_cycle(_watches(), high, state, now=2.0, repeat=100.0)
    assert recoveries == []


def test_none_balance_does_not_toggle_state(cb):
    state = {}
    # First, drive SOL into the low state
    def low(c, a, m):
        return 1.0 if m == "native" else 200.0
    cb.evaluate_cycle(_watches(), low, state, now=0.0, repeat=100.0)
    # Transient failure (None) must not produce a recovery or a breach
    def none(c, a, m):
        return None
    breaches, recoveries = cb.evaluate_cycle(_watches(), none, state, now=1.0, repeat=100.0)
    assert breaches == [] and recoveries == []
    assert state[("solana", "AAA", "native")]["low"] is True


def test_build_breach_message_lists_each(cb):
    msg = cb.build_breach_message([
        {"label": "relayer", "chain": "solana", "symbol": "SOL",
         "balance": 1.0, "threshold": 5.0, "address": "AAA"},
    ])
    assert "low balance" in msg.lower()
    assert "relayer" in msg and "SOL" in msg and "solana" in msg


def test_slack_post_noop_when_disabled(cb, monkeypatch):
    cb.SLACK_WEBHOOK_URL = ""
    called = {"n": 0}
    monkeypatch.setattr(cb.urllib.request, "urlopen",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    cb.slack_post("hi")
    assert called["n"] == 0
