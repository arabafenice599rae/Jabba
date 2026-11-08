"""Offline smoke test: build the batch distribution tx without network."""
import asyncio
import json
import os
import tempfile

import pytest
from solders.keypair import Keypair


class FakeRPC:
    async def send_transaction(self, txn):
        class R:
            value = "fakesig123"
        return R()

    async def get_latest_blockhash(self):
        from solders.hash import Hash
        class R:
            value = type("V", (), {"blockhash": Hash.from_string(
                "11111111111111111111111111111111")})()
        return R()


def test_batch_transfer_builds_single_tx(tmp_path):
    kp = Keypair()
    kp_path = tmp_path / "kp.json"
    kp_path.write_text(json.dumps(list(kp.to_bytes())))

    from curvebot.chain import Chain
    chain = Chain.__new__(Chain)
    chain.keypair = kp
    chain.client = FakeRPC()

    recipients = [(str(Keypair().pubkey()), 100.0) for _ in range(5)]
    sig = asyncio.run(chain.transfer_tokens_batch(
        str(Keypair().pubkey()), recipients))
    assert sig == "fakesig123"


def test_batch_transfer_empty(tmp_path):
    kp = Keypair()
    from curvebot.chain import Chain
    chain = Chain.__new__(Chain)
    chain.keypair = kp
    chain.client = FakeRPC()
    sig = asyncio.run(chain.transfer_tokens_batch(
        str(Keypair().pubkey()), []))
    assert sig == ""


def test_batch_transfer_skips_zero_amounts():
    kp = Keypair()
    from curvebot.chain import Chain
    chain = Chain.__new__(Chain)
    chain.keypair = kp
    chain.client = FakeRPC()
    sig = asyncio.run(chain.transfer_tokens_batch(
        str(Keypair().pubkey()), [(str(Keypair().pubkey()), 0.0)]))
    assert sig == ""
