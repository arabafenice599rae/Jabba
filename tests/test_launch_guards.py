"""Tests for Jito bundle state machine and launch cost preflight."""
import asyncio

import pytest

from curvebot.launch import (
    NETWORK_FEE_LAMPORTS,
    estimate_launch_costs,
    parse_confirmation,
    parse_inflight_status,
    plan_dev_buy,
)
from curvebot.pump_curve import (
    TOKEN_ACCOUNT_SIZE,
    classic_rent,
    create_account_sizes,
    max_sol_cost_lamports,
)

MAINNET_RENT_PER_BYTE = 5080        # measured on mainnet, 2026-11


def mainnet_rent(size):
    return (size + 128) * MAINNET_RENT_PER_BYTE


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
    def _chain(self, rent=mainnet_rent, fail=False):
        class FakeRPC:
            async def get_minimum_balance_for_rent_exemption(self, size):
                if fail:
                    raise RuntimeError("rpc down")
                return type("R", (), {"value": rent(size)})
        return type("C", (), {"client": FakeRPC()})()

    def _cfg(self, keep=10.0):
        return type("Cfg", (), {"jito_tip_lamports": 1_000_000, "token_name": "Jabba",
                                "token_symbol": "JABBA", "team_keep_percent": keep,
                                "min_dev_buy_percent": 50.0})()

    def test_costs_breakdown(self):
        costs = asyncio.run(estimate_launch_costs(self._chain(), self._cfg(), 36, 1.0))
        assert costs["dev_buy"] == max_sol_cost_lamports(1.0)     # curve + fee cap
        assert costs["ata_rent"] == 36 * mainnet_rent(TOKEN_ACCOUNT_SIZE)
        assert costs["network_fees"] == NETWORK_FEE_LAMPORTS * 6  # create (2 sig) + 4 dist
        assert costs["jito_tip"] == 1_000_000
        assert "priority_fee" not in costs                       # tip pays inclusion
        assert costs["total"] == sum(v for k, v in costs.items() if k != "total")

    def test_create_rent_matches_mainnet_fork(self):
        """Fork, 2026-11-09: our create+buy opened 8_615_680 lamports of
        accounts with an 82-byte uri; the budget uses a 80-byte Pinata uri."""
        costs = asyncio.run(estimate_launch_costs(self._chain(), self._cfg(), 1, 0.0))
        fork_meta = len("Jabba Test") + len("JABBA") + 82      # what the fork used
        budget_meta = len("Jabba") + len("JABBA") + 80
        assert costs["create_rent"] == 8_615_680 - (fork_meta - budget_meta) * MAINNET_RENT_PER_BYTE

    def test_rpc_down_falls_back_to_classic_rent_upper_bound(self):
        hi = asyncio.run(estimate_launch_costs(self._chain(fail=True), self._cfg(), 36, 0.0))
        lo = asyncio.run(estimate_launch_costs(self._chain(), self._cfg(), 36, 0.0))
        assert hi["total"] > lo["total"]
        assert hi["ata_rent"] == 36 * classic_rent(TOKEN_ACCOUNT_SIZE)

    def test_mint_size_grows_with_metadata(self):
        assert create_account_sizes("a" * 32, "b" * 13, "c" * 200)[0] == 316 + 245


class TestDynamicDevBuy:
    def _plan(self, raised, n=3, treasury=None, keep=10.0):
        t = TestCostPreflight()
        treasury = round(raised * 1e9) if treasury is None else treasury
        return asyncio.run(plan_dev_buy(t._chain(), t._cfg(keep), n, raised, treasury))

    def test_big_raise_uses_the_full_target(self):
        dev, costs = self._plan(9.0, n=36)
        assert dev == pytest.approx(8.1)
        assert costs["team_left"] > 0.5e9

    @pytest.mark.parametrize("raised", [0.1, 0.05, 0.04])
    def test_small_raise_is_lowered_to_what_fits(self, raised):
        """With 0.1 SOL the fixed 90% used to be refused by the preflight."""
        dev, costs = self._plan(raised)
        assert dev < raised * 0.9
        assert costs["total"] <= round(raised * 1e9)
        assert costs["team_left"] >= 0

    def test_dev_buy_never_below_the_floor(self):
        """3 wallets: ~0.0148 SOL of fixed costs -> smallest viable raise is
        ~0.0301 SOL with MIN_DEV_BUY_PERCENT=50."""
        with pytest.raises(RuntimeError, match="troppo piccola"):
            self._plan(0.030, n=3)
        self._plan(0.0302, n=3)

    @pytest.mark.parametrize("raised", [0.06, 0.2, 1.0, 9.0, 40.0])
    def test_plan_always_fits_the_treasury(self, raised):
        dev, costs = self._plan(raised, n=12)
        assert costs["total"] <= round(raised * 1e9)
        assert costs["dev_buy"] == max_sol_cost_lamports(dev)

    def test_curve_capacity_still_refused(self):
        with pytest.raises(RuntimeError, match="capacita della curva"):
            self._plan(120.0, n=36)


def test_treasury_stays_rent_exempt():
    """Fork, 0.1 SOL test launch: squeezing the dev buy to the last lamport
    left the treasury below its rent-exempt minimum and the last tx failed
    with InsufficientFundsForRent. The minimum is now part of the costs."""
    t = TestCostPreflight()
    costs = asyncio.run(estimate_launch_costs(t._chain(), t._cfg(), 3, 0.0))
    assert costs["treasury_reserve"] == mainnet_rent(0)
    dev, plan = TestDynamicDevBuy()._plan(0.1)
    assert plan["team_left"] >= 0                                 # on top of the reserve
