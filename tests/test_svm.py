"""The bot's Solana code EXECUTED on a real SVM (LiteSVM, offline): refunds,
deposit detection end-to-end, token distribution on Token-2022 and SPL
Token mints, size limits and failure modes. Nothing here is mocked except
the RPC transport (see litesvm_rpc.py)."""
from __future__ import annotations

import asyncio

import pytest
from conftest import Recorder, make_call
from litesvm_rpc import LiteRPC, TxFailed
from solders.keypair import Keypair
from solders.message import Message, MessageV0
from solders.pubkey import Pubkey
from solders.system_program import CreateAccountParams, TransferParams, create_account, transfer
from solders.transaction import VersionedTransaction
from spl.token.constants import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
    initialize_mint,
    mint_to_checked,
    models,
)

from curvebot import db, service
from curvebot import handlers as h
from curvebot.chain import JITO_TIP_ACCOUNTS, Chain
from curvebot.launch import compute_distribution, plan_chunks
from curvebot.monitor import DepositMonitor
from curvebot.pump_curve import tokens_out_for_sol

SOL = 1_000_000_000
FEE = 5_000                      # one signature
PROGRAMS = [TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID]


@pytest.fixture
def rpc():
    return LiteRPC()


@pytest.fixture
def treasury(rpc):
    c = Chain.__new__(Chain)
    c.keypair = Keypair()
    c.client = rpc
    c._blockhash = None
    rpc.svm.airdrop(c.keypair.pubkey(), 200 * SOL)
    return c


def _send(rpc, signer: Keypair, ixs, v0=False):
    bh = rpc.svm.latest_blockhash()
    msg = (MessageV0.try_compile(signer.pubkey(), ixs, [], bh) if v0
           else Message.new_with_blockhash(ixs, signer.pubkey(), bh))
    return rpc.execute(VersionedTransaction(msg, [signer]), raise_on_error=False)


def _new_mint(rpc, authority: Keypair, program: Pubkey) -> Pubkey:
    mint = Keypair()
    rent = rpc.svm.minimum_balance_for_rent_exemption(82)
    bh = rpc.svm.latest_blockhash()
    ixs = [create_account(CreateAccountParams(
               from_pubkey=authority.pubkey(), to_pubkey=mint.pubkey(),
               lamports=rent, space=82, owner=program)),
           initialize_mint(models.InitializeMintParams(
               decimals=6, program_id=program, mint=mint.pubkey(),
               mint_authority=authority.pubkey(), freeze_authority=None))]
    rpc.execute(VersionedTransaction(Message.new_with_blockhash(ixs, authority.pubkey(), bh),
                                     [authority, mint]))
    return mint.pubkey()


def _fund_treasury_tokens(rpc, treasury: Chain, program: Pubkey, amount: int) -> Pubkey:
    kp = treasury.keypair
    mint = _new_mint(rpc, kp, program)
    ata = get_associated_token_address(kp.pubkey(), mint, program)
    _send(rpc, kp, [
        create_idempotent_associated_token_account(kp.pubkey(), kp.pubkey(), mint, program),
        mint_to_checked(models.MintToCheckedParams(
            program_id=program, mint=mint, dest=ata, mint_authority=kp.pubkey(),
            amount=amount, decimals=6, signers=[kp.pubkey()]))])
    assert rpc.token_balance(ata) == amount
    return mint


# ---------------- refunds (send_sol) ----------------

def test_send_sol_executes_exactly(rpc, treasury):
    dest = Keypair().pubkey()
    before = rpc.svm.get_balance(treasury.keypair.pubkey())
    sig = asyncio.run(treasury.send_sol(str(dest), 0.123456789))
    assert rpc.svm.get_balance(dest) == 123_456_789
    assert rpc.svm.get_balance(treasury.keypair.pubkey()) == before - 123_456_789 - FEE
    assert sig == rpc.history[-1][0]


def test_send_sol_insufficient_funds_raises(rpc, treasury):
    with pytest.raises(TxFailed):
        asyncio.run(treasury.send_sol(str(Keypair().pubkey()), 10_000.0))


def test_refund_handler_pays_on_chain(rpc, treasury, cfg, conn):
    user = Keypair()
    db.set_wallet(conn, 1001, str(user.pubkey()))
    service.record_deposit(conn, 1001, 2.0)
    rec = Recorder()

    async def flow():
        await h.cb_refund_amt(make_call(1001, "refund:amt:50", rec), cfg, conn)
        await h.cb_refund_go(make_call(1001, "refund:go", rec), cfg, conn, treasury)
    asyncio.run(flow())
    assert rpc.svm.get_balance(user.pubkey()) == round(1.0 * 0.997 * SOL)
    assert db.user_contribution(conn, 1001) == pytest.approx(1.0)
    assert "Rimborso completato" in rec.last


# ---------------- deposits: wallet -> treasury -> monitor ----------------

@pytest.mark.parametrize("v0", [False, True])
def test_deposit_detected_end_to_end(rpc, treasury, cfg, db_path, v0):
    user, other = Keypair(), Keypair()
    rpc.svm.airdrop(user.pubkey(), 10 * SOL)
    me = treasury.keypair.pubkey()
    _send(rpc, user, [transfer(TransferParams(from_pubkey=user.pubkey(),
                                              to_pubkey=me, lamports=1_500_000_000))], v0=v0)
    # noise the monitor must ignore: a failed transfer and a refund out
    _send(rpc, user, [transfer(TransferParams(from_pubkey=user.pubkey(),
                                              to_pubkey=me, lamports=999 * SOL))])
    asyncio.run(treasury.send_sol(str(other.pubkey()), 0.1))
    with db.connect(db_path) as c:
        db.set_wallet(c, 1001, str(user.pubkey()))
    mon = DepositMonitor(treasury, cfg, lambda: db.connect(db_path))
    for _ in range(3):                                     # repeated polls
        asyncio.run(mon._poll_sol_deposits())
    with db.connect(db_path) as c:
        assert db.user_contribution(c, 1001) == pytest.approx(1.5)
        assert c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1


# ---------------- distribution on real token programs ----------------

@pytest.mark.parametrize("program", PROGRAMS, ids=["token-2022", "spl-token"])
@pytest.mark.parametrize("n", [1, 9, 10, 36])
def test_distribution_lands_exactly(rpc, treasury, program, n):
    bought = tokens_out_for_sol(9.0)
    mint = _fund_treasury_tokens(rpc, treasury, program, bought)
    users = [Keypair() for _ in range(n)]
    weights = {i: 0.1 * (i % 5 + 1) for i in range(n)}          # REAL-style floats
    dist, team, dust = compute_distribution(weights, bought, 0, 10)
    recipients = [(str(users[i].pubkey()), amt) for i, amt in dist]
    # one user already holds an ATA: idempotent create must not fail
    _send(rpc, treasury.keypair, [create_idempotent_associated_token_account(
        treasury.keypair.pubkey(), users[0].pubkey(), mint, program)])
    tip = JITO_TIP_ACCOUNTS[0]
    rpc.svm.airdrop(Pubkey.from_string(tip), SOL)
    tip_before = rpc.svm.get_balance(Pubkey.from_string(tip))
    asyncio.run(treasury.refresh_blockhash())
    chunks = plan_chunks(recipients)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        raw = treasury.build_distribution_tx(str(mint), chunk, 1_000_000 if last else 0,
                                             tip, token_program=str(program))
        rpc.execute(raw)
    for (wallet, amt) in recipients:
        ata = get_associated_token_address(Pubkey.from_string(wallet), mint, program)
        assert rpc.token_balance(ata) == amt
    src = get_associated_token_address(treasury.keypair.pubkey(), mint, program)
    assert rpc.token_balance(src) == bought - sum(a for _, a in recipients)
    assert rpc.svm.get_balance(Pubkey.from_string(tip)) == tip_before + 1_000_000


def test_wrong_token_program_fails_on_chain(rpc, treasury):
    """The original bug: SPL Token instructions against a Token-2022 mint."""
    mint = _fund_treasury_tokens(rpc, treasury, TOKEN_2022_PROGRAM_ID, 10**12)
    asyncio.run(treasury.refresh_blockhash())
    raw = treasury.build_distribution_tx(str(mint), [(str(Keypair().pubkey()), 10)],
                                         token_program=str(TOKEN_PROGRAM_ID))
    with pytest.raises(TxFailed, match="IncorrectProgramId"):
        rpc.execute(raw)


def test_over_distribution_fails_the_tx(rpc, treasury):
    """If the dev buy returned fewer tokens than planned the distribution tx
    fails, which inside a Jito bundle reverts the whole launch (safe)."""
    mint = _fund_treasury_tokens(rpc, treasury, TOKEN_2022_PROGRAM_ID, 1_000)
    asyncio.run(treasury.refresh_blockhash())
    raw = treasury.build_distribution_tx(str(mint), [(str(Keypair().pubkey()), 1_001)])
    with pytest.raises(TxFailed, match="InsufficientFunds|0x1"):
        rpc.execute(raw)


def test_offcurve_pda_wallet_receives(rpc, treasury):
    pda, _ = Pubkey.find_program_address([b"vault"], Pubkey.from_string(
        "SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf"))
    mint = _fund_treasury_tokens(rpc, treasury, TOKEN_2022_PROGRAM_ID, 10**9)
    asyncio.run(treasury.refresh_blockhash())
    rpc.execute(treasury.build_distribution_tx(str(mint), [(str(pda), 777)]))
    ata = get_associated_token_address(pda, mint, TOKEN_2022_PROGRAM_ID)
    assert rpc.token_balance(ata) == 777
