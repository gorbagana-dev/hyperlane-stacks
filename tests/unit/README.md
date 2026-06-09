# Unit Tests

Fast, self-contained tests for individual helper scripts and pure functions. Unlike
the [e2e suite](../e2e/README.md), these need **no** kind cluster, chain nodes,
`laconic-so`, or Docker — they run in a tempdir against the script/function under
test and finish in well under a second.

## What's tested

- **`test_relayer_whitelist_builder.py`** — drives
  `stack_orchestrator/data/config/warp-deployer-scripts-config/build-relayer-whitelist.sh`
  against fixture state in a tempdir and asserts the emitted Hyperlane
  `MatchingList`: unions each route's per-chain warp program `hex` as
  `{recipientaddress}` rules, dedupes a program shared across routes,
  `0x`-prefixes bare hex, and falls back to the deny-all sentinel
  (`[{"recipientaddress":"0x000…000"}]`) when no rules are produced.

## Prerequisites

- Python 3.10+ with `pytest`
- `bash` and `jq` on `PATH` (the whitelist builder shells out to `jq`)

`pytest` is already in the e2e venv (`tests/e2e/requirements.txt`); activating it
is the simplest way to get a runner. No other dependencies are needed.

## Running

```bash
# From the repo root — no cluster, no venv-only deps beyond pytest:
pytest tests/unit/ -v
```

These tests do **not** load `tests/e2e/conftest.py` (they live outside that
subtree), so they are unaffected by the e2e fixtures and run standalone.

## Adding a test

Keep unit tests hermetic: no network, no Docker, no cluster, no reads of a live
deployment. Build inputs in `tmp_path`, invoke the script/function, assert on its
output. Anything that needs a real deployment belongs in `tests/e2e/` instead.
