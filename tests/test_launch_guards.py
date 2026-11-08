"""Tests for Jito bundle state machine and launch cost preflight."""
import asyncio

import pytest

from curvebot.launch import (NETWORK_FEE_LAMPORTS, TOKEN_ACCOUNT_RENT_LAMPORTS,
                             estimate_launch_costs, parse_confirmation,
                             parse_inflight_status)


class TestBundleStatusParsing:
    def test_inflight_pending(self):
        data = {"result": {"value": [{"bundle_id": "x", "status":
                {"Pending": {"land_block": None}}} ]}}
        assert parse_inflight_status(data) == "Pending"

    def test_inflight_landed(self):
        data = {"result": {"value": [{"bundle_id": "x", "status":
                {"Landed": {"land_block": 123}}} ]}}
        assert parse_inflight_status(data) == "Landed"

    def test_inflight_failed(self):
        data = {"result": {"value": [{"bundle_id": "x", "status":
                {"Failed": {"err": "sim failed"}}} ]}}
        assert parse_inflight_status(data) == "Failed"

    def test_inflight_malformed(self):
        assert parse_inflight_status({}) == "Unknown"
        assert parse_inflight_status({"result": {"value": []}}) == "Unknown"

    def test_confirmation_finalized(self):
        data = {"result": {"value": [{"transactions": [
            {"signature": "a", "confirmation_status": "finalized"},
            {"signature": "b", "confirmation_status": "confirmed"},
        ]}]}}
        assert parse_confirmation(data) == "finalized"

    def test_confirmation_confirmed(self):
        data = {"result": {"value": [{"transactions": [
            {"signature": "a", "confirmation_status": "confirmed"},
        ]}]}}
        assert parse_confirmation(data) == "confirmed"

    def test_confirmation_processed_only(self):
        data = {"result": {"value": [{"transactions": [
            {"signature": "a", "confirmation_status": "processed"},
        ]}]}}
        assert parse_confirmation(data) == "processed"

    def test_confirmation_malformed(self):
        assert parse_confirmation({}) == "unknown"


class TestCostPreflight:
    def _chain(self, rent_value=None):
        class FakeRPC:
            async def get_minimum_balance_for_rent_exemption(self, size):
                class R:
                    value = rent_value if rent_value is not None else \
                        TOKEN_ACCOUNT_RENT_LAMPORTS
                return R
        chain = type("C", (), {"client": FakeRPC()})()
        return chain

    def _cfg(self):
        class Cfg:
            launch_priority_fee = 0.0005
            jito_tip_lamports = 1_000_000
        return Cfg()

    def test_costs_breakdown(self):
        cfg = self._cfg()
        chain = self._chain()
        costs = asyncio.run(estimate_launch_costs(chain, cfg, 36, 1.0))
        assert costs["dev_buy"] == 1_000_000_000
        assert costs["ata_rent"] == 36 * TOKEN_ACCOUNT_RENT_LAMPORTS
        assert costs["network_fees"] == NETWORK_FEE_LAMPORTS * 5
        assert costs["priority_fee"] == 500_000
        assert costs["jito_tip"] == 1_000_000
        assert costs["total"] == sum(v for k, v in costs.items() if k != "total")

    def test_costs_rent_36_users_meaningful(self):
        """36 ATA rent is NOT negligible: must be ~0.073 SOL."""
        cfg = self._cfg()
        chain = self._chain()
        costs = asyncio.run(estimate_launch_costs(chain, cfg, 36, 0.0))
        assert costs["ata_rent"] / 1e9 == pytest.approx(0.0734, abs=0.001)

    def test_costs_rpc_rent_override(self):
        cfg = self._cfg()
        chain = self._chain(rent_value=3_000_000)
        costs = asyncio.run(estimate_launch_costs(chain, cfg, 2, 0.0))
        assert costs["ata_rent"] == 6_000_000
