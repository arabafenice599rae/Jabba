"""Shared fixtures for the stress suite: temp DB, config, fake chain,
fake Telegram objects. Everything offline and deterministic."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curvebot import db  # noqa: E402

ADMIN = 1000
USERS = [1001, 1002, 1003]


def finding(reason: str):
    """A confirmed bug: strict xfail, so the test flips to FAIL (XPASS)
    the moment the bug is fixed and the marker must be removed."""
    return pytest.mark.xfail(strict=True, reason=f"BUG: {reason}")


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "jabba.db")


@pytest.fixture
def conn(db_path):
    c = db.open_conn(db_path)
    yield c
    c.close()


@pytest.fixture
def cfg():
    return SimpleNamespace(
        allowed_ids=[ADMIN, *USERS, *range(2000, 2100)],
        refund_fee_bps=30, team_keep_percent=10.0, team_share_bps=0,
        safety_bps=10, min_deposit_sol=0.01, poll_interval_s=0,
        token_name="Jabba", token_symbol="JABBA", token_description="",
        token_image_path="x.png", pinata_jwt="jwt", token_twitter="",
        token_telegram="", token_website="", launch_slippage=10,
        launch_priority_fee=0.0005, jito_tip_lamports=1_000_000,
    )


class FakeChain:
    """Treasury simulator: tracks lamports, records outgoing SOL,
    optional latency on send_sol to expose races."""

    def __init__(self, balance_sol: float = 1_000.0, send_delay: float = 0.0):
        self.lamports = int(balance_sol * 1e9)
        self.sent: list[tuple[str, float]] = []
        self.send_delay = send_delay
        self.pubkey = "TreasuryPubkey1111111111111111111111111111"
        self.fail_send = False

    async def balance_sol(self):
        return self.lamports / 1e9

    async def send_sol(self, wallet, amount_sol):
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        if self.fail_send:
            raise RuntimeError("rpc down")
        lam = int(round(amount_sol * 1e9))
        if lam > self.lamports:
            raise RuntimeError("insufficient funds")
        self.lamports -= lam
        self.sent.append((wallet, amount_sol))
        return f"sig{len(self.sent)}"


@pytest.fixture
def chain():
    return FakeChain()


class Recorder:
    def __init__(self):
        self.texts: list[str] = []
        self.alerts: list[str] = []
        self.photos = 0

    @property
    def last(self) -> str:
        return self.texts[-1] if self.texts else ""


def make_message(uid: int, rec: Recorder):
    async def answer(text, reply_markup=None, **kw):
        rec.texts.append(text)
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), answer=answer)


def make_call(uid: int, data: str, rec: Recorder):
    async def edit_text(text, reply_markup=None, **kw):
        rec.texts.append(text)

    async def msg_answer(text, reply_markup=None, **kw):
        rec.texts.append(text)

    async def answer_photo(png, caption=None, reply_markup=None, **kw):
        rec.photos += 1

    async def answer(text=None, show_alert=False, **kw):
        if text:
            rec.alerts.append(text)

    message = SimpleNamespace(edit_text=edit_text, answer=msg_answer,
                              answer_photo=answer_photo)
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), data=data,
                           message=message, answer=answer)


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture(autouse=True)
def _clear_pending():
    from curvebot import handlers
    handlers.PENDING_REFUNDS.clear()
    yield
    handlers.PENDING_REFUNDS.clear()
