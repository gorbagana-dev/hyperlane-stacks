"""E2E tests for the hyperlane-gas-oracle stack.

Verifies the gas oracle service successfully fetches live token prices,
computes exchange rates, and updates on-chain IGP gas oracle configurations
on both Gorchain and Solana.
"""

from __future__ import annotations

import logging
import re

import pytest

from lib.common import CHAINS, get_configmap_json, run_deployer_cli

log = logging.getLogger(__name__)


@pytest.mark.slow
class TestGasOracle:
    """Gas oracle service tests.

    The gas_oracle_deployment fixture deploys the oracle, waits for its
    first successful update, and captures the computed values from logs.
    Tests verify on-chain state matches what the oracle submitted.
    """

    def test_oracle_completed_update(self, gas_oracle_deployment: dict) -> None:
        """Verify the oracle completed at least one update cycle."""
        oracle_values = gas_oracle_deployment["oracle_values"]

        # The fixture waited for "Gas oracle update complete." in logs,
        # so we just verify the parsed output is non-empty.
        assert oracle_values, "Oracle log parsing returned empty — no update detected"
        assert "gorchain_to_solana_exchange_rate" in oracle_values, (
            "Missing Gorchain→Solana exchange rate in oracle output"
        )
        assert "solana_to_gorchain_exchange_rate" in oracle_values, (
            "Missing Solana→Gorchain exchange rate in oracle output"
        )
        log.info("Oracle values: %s", oracle_values)

    def test_oracle_updated_on_chain(self, gas_oracle_deployment: dict) -> None:
        """Verify on-chain IGP state matches oracle's submitted values."""
        ns = gas_oracle_deployment["deployment"].namespace
        oracle_values = gas_oracle_deployment["oracle_values"]
        remote_chain = {"gorchain": "solana", "solana": "gorchain"}

        # Map oracle log keys to chain direction
        direction_keys = {
            "gorchain": {
                "exchange_rate": "gorchain_to_solana_exchange_rate",
                "gas_price": "gorchain_to_solana_gas_price",
            },
            "solana": {
                "exchange_rate": "solana_to_gorchain_exchange_rate",
                "gas_price": "solana_to_gorchain_gas_price",
            },
        }

        for chain_name, chain_info in CHAINS.items():
            program_ids = get_configmap_json(
                ns, "hyperlane-program-ids", f"{chain_name}-program-ids.json",
            )
            igp_id = program_ids["igp_program_id"]
            igp_account = program_ids["igp_account"]
            remote_domain = CHAINS[remote_chain[chain_name]]["domain_id"]

            # Query on-chain IGP state
            result = run_deployer_cli(
                "igp", "query",
                "--program-id", igp_id,
                "--igp-account", igp_account,
                rpc=chain_info["rpc"],
            )
            output = result.stdout + result.stderr
            log.info("%s: IGP query output:\n%s", chain_name, output[:2000])
            assert result.returncode == 0, (
                f"{chain_name}: IGP query failed: {output}"
            )

            # Verify the remote domain appears
            assert str(remote_domain) in output, (
                f"{chain_name}: remote domain {remote_domain} not found in IGP output"
            )

            # Parse on-chain values
            rate_match = re.search(r"token_exchange_rate:\s*(\d+)", output)
            assert rate_match, (
                f"{chain_name}: token_exchange_rate not found in IGP output"
            )
            gas_match = re.search(r"gas_price:\s*(\d+)", output)
            assert gas_match, (
                f"{chain_name}: gas_price not found in IGP output"
            )

            # Compare against oracle's logged values
            keys = direction_keys[chain_name]
            expected_rate = oracle_values[keys["exchange_rate"]]
            expected_gas = oracle_values[keys["gas_price"]]

            assert rate_match.group(1) == expected_rate, (
                f"{chain_name}: on-chain exchange rate {rate_match.group(1)} "
                f"doesn't match oracle's {expected_rate}"
            )
            assert gas_match.group(1) == expected_gas, (
                f"{chain_name}: on-chain gas price {gas_match.group(1)} "
                f"doesn't match oracle's {expected_gas}"
            )

            log.info(
                "%s: on-chain IGP matches oracle (rate=%s, gasPrice=%s)",
                chain_name, rate_match.group(1), gas_match.group(1),
            )

    def test_oracle_rates_reasonable(self, gas_oracle_deployment: dict) -> None:
        """Sanity check that oracle-set values are in plausible ranges."""
        ns = gas_oracle_deployment["deployment"].namespace

        for chain_name, chain_info in CHAINS.items():
            program_ids = get_configmap_json(
                ns, "hyperlane-program-ids", f"{chain_name}-program-ids.json",
            )
            igp_id = program_ids["igp_program_id"]
            igp_account = program_ids["igp_account"]

            result = run_deployer_cli(
                "igp", "query",
                "--program-id", igp_id,
                "--igp-account", igp_account,
                rpc=chain_info["rpc"],
            )
            output = result.stdout + result.stderr
            assert result.returncode == 0

            # Exchange rate should be in Sealevel scale (1e18 - 1e22 range)
            rate_match = re.search(r"token_exchange_rate:\s*(\d+)", output)
            assert rate_match
            rate = int(rate_match.group(1))
            assert rate > 0, f"{chain_name}: exchange rate is zero"
            assert rate >= 10**18, (
                f"{chain_name}: exchange rate {rate} below Sealevel minimum scale (1e18)"
            )
            assert rate <= 10**22, (
                f"{chain_name}: exchange rate {rate} above reasonable maximum (1e22)"
            )

            # Gas price should be positive
            gas_match = re.search(r"gas_price:\s*(\d+)", output)
            assert gas_match
            gas_price = int(gas_match.group(1))
            assert gas_price > 0, f"{chain_name}: gas price is zero"

            # Token decimals should be 9 (SOL/gGOR)
            dec_match = re.search(r"token_decimals:\s*(\d+)", output)
            assert dec_match
            assert int(dec_match.group(1)) == 9, (
                f"{chain_name}: token_decimals {dec_match.group(1)} != 9"
            )

            log.info(
                "%s: rates reasonable (rate=%s, gasPrice=%s, decimals=9)",
                chain_name, rate, gas_price,
            )
