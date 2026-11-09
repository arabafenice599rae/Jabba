"""OPT-IN (JABBA_FORK=1): the REAL run_launch against a mainnet fork.

- PumpPortal is called for real (it only BUILDS the unsigned create tx)
- every account the txs touch is cloned from mainnet into LiteSVM
- the Jito bundle is executed in LiteSVM, in order, instead of being sent
- only Pinata (metadata upload) is stubbed
Nothing is ever submitted to mainnet. Run it before every real launch: it
catches changes in pump.fun, PumpPortal fees/format or the token program.
"""
from __future__ import annotations

import asyncio
import os

import aiohttp
import pytest
from litesvm_rpc import MainnetFork, TxFailed
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from spl.token.constants import TOKEN_2022_PROGRAM_ID
from spl.token.instructions import get_associated_token_address

from curvebot import db, service
from curvebot import launch as L
from curvebot.chain import Chain

pytestmark = pytest.mark.skipif(os.environ.get("JABBA_FORK") != "1",
                                reason="mainnet fork test: set JABBA_FORK=1")
SOL = 1_000_000_000


@pytest.mark.parametrize("n_users,sol_each", [(36, 0.25), (3, 2.0)])
def test_real_launch_on_mainnet_fork(monkeypatch, cfg, conn, n_users, sol_each):
    fork = MainnetFork(os.environ.get("JABBA_FORK_RPC", "https://api.mainnet-beta.solana.com"))
    chain = Chain.__new__(Chain)
    chain.keypair, chain.client, chain._blockhash = Keypair(), fork, None
    users = [Keypair() for _ in range(n_users)]
    for i, u in enumerate(users):
        db.set_wallet(conn, 7000 + i, str(u.pubkey()))
        service.record_deposit(conn, 7000 + i, sol_each)
    raised_lamports = round(n_users * sol_each * SOL)
    fork.svm.airdrop(chain.keypair.pubkey(), raised_lamports)    # treasury = deposits only

    async def no_pinata(http, cfg):
        return "https://ipfs.io/ipfs/bafkreiexample"
    outcome = {}

    async def bundle_in_fork(http, txs):
        try:
            for tx in txs:
                fork.execute(tx)
            outcome["status"] = "finalized"
        except TxFailed as e:
            outcome["status"], outcome["error"] = "failed", str(e)
        return "fork-bundle"

    async def wait(http, bundle_id, **kw):
        return outcome["status"]
    monkeypatch.setattr(L, "upload_pinata_metadata", no_pinata)
    monkeypatch.setattr(L, "wait_bundle", wait)
    chain.send_bundle = bundle_in_fork

    async def go():
        async with aiohttp.ClientSession() as http:
            return await L.run_launch(chain, cfg, conn, http)
    try:
        report = asyncio.run(go())
    except RuntimeError:
        pytest.fail(f"launch aborted on fork: {outcome.get('error', '')}")

    mint = Pubkey.from_string(report["mint"])
    assert fork.svm.get_account(mint).owner == TOKEN_2022_PROGRAM_ID
    weights = {7000 + i: sol_each for i in range(n_users)}
    dist, _, _ = L.compute_distribution(weights, round(report["tokens_bought"] * 10**6),
                                        cfg.team_share_bps, cfg.safety_bps)
    for (uid, amount), u in zip(dist, users):
        ata = get_associated_token_address(u.pubkey(), mint, TOKEN_2022_PROGRAM_ID)
        assert fork.token_balance(ata) == amount, uid
    left = fork.token_balance(get_associated_token_address(
        chain.keypair.pubkey(), mint, TOKEN_2022_PROGRAM_ID))
    bought = round(report["tokens_bought"] * 10**6)
    assert left == bought - sum(a for _, a in dist)
    assert left <= bought * (cfg.safety_bps + 1) // 10_000        # only safety + dust stays
    assert db.get_meta(conn, "state") == "launched"
    sol_left = fork.svm.get_balance(chain.keypair.pubkey())
    print(f"\n{n_users} utenti x {sol_each} SOL: token comprati {bought/1e6:,.0f}, "
          f"in treasury {left/1e6:,.0f}, SOL rimasti al treasury {sol_left/1e9:.6f} "
          f"su {raised_lamports/1e9:.4f} raccolti")
