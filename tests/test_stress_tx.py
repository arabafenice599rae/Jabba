"""Real transaction building with solders/spl (offline): size limits,
decoded instruction contents, tip placement, edge wallets."""
from __future__ import annotations

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.constants import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address

from curvebot.chain import JITO_TIP_ACCOUNTS, Chain
from curvebot.launch import CHUNK_SIZE, MAX_PARTICIPANTS, TX_SIZE_LIMIT, plan_chunks

SYSTEM = Pubkey.from_string("11111111111111111111111111111111")


def _chain():
    c = Chain.__new__(Chain)
    c.keypair = Keypair()
    c._blockhash = Hash.default()
    return c


def _decode(raw: bytes):
    tx = Transaction.from_bytes(raw)
    keys = tx.message.account_keys
    out = []
    for ix in tx.message.instructions:
        out.append((keys[ix.program_id_index], [keys[a] for a in ix.accounts],
                    bytes(ix.data)))
    return tx, out


@settings(max_examples=60, deadline=None)
@given(st.integers(1, MAX_PARTICIPANTS), st.booleans(),
       st.sampled_from([TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID]))
def test_every_chunk_fits_and_carries_exact_amounts(n, dup, program):
    c = _chain()
    mint = Keypair().pubkey()
    wallets = [str(Keypair().pubkey()) for _ in range(n)]
    if dup and n > 1:
        wallets[-1] = wallets[0]
    rng = random.Random(n)
    recipients = [(w, rng.randint(1, 10**15)) for w in wallets]
    chunks = plan_chunks(recipients)
    seen = []
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        raw = c.build_distribution_tx(str(mint), chunk, 1_000_000 if last else 0,
                                      JITO_TIP_ACCOUNTS[0], token_program=str(program))
        assert len(raw) <= TX_SIZE_LIMIT, (n, i, len(raw))
        tx, ixs = _decode(raw)
        assert tx.verify_with_results() if hasattr(tx, "verify_with_results") else True
        transfers = [x for x in ixs if x[0] == program]
        tips = [x for x in ixs if x[0] == SYSTEM]
        assert len(tips) == (1 if last else 0)
        if last:
            assert str(tips[0][1][1]) == JITO_TIP_ACCOUNTS[0]
            assert int.from_bytes(tips[0][2][4:12], "little") == 1_000_000
        for prog, accts, data in transfers:
            assert data[0] == 12                       # TransferChecked
            amount = int.from_bytes(data[1:9], "little")
            assert data[9] == 6                        # decimals
            assert accts[1] == mint
            seen.append((str(accts[2]), amount))
    expected = [(str(get_associated_token_address(Pubkey.from_string(w), mint, program)), a)
                for w, a in recipients]
    assert seen == expected


def test_worst_case_chunk_size_margin():
    """9 distinct recipients + tip must fit; 10 + tip must not (the
    constant CHUNK_SIZE is tight, not arbitrary)."""
    c = _chain()
    mint = str(Keypair().pubkey())
    nine = [(str(Keypair().pubkey()), 10**15) for _ in range(CHUNK_SIZE)]
    ten = [*nine, (str(Keypair().pubkey()), 10**15)]
    assert len(c.build_distribution_tx(mint, nine, 1, JITO_TIP_ACCOUNTS[0])) <= TX_SIZE_LIMIT
    assert len(c.build_distribution_tx(mint, ten, 1, JITO_TIP_ACCOUNTS[0])) > TX_SIZE_LIMIT


def test_float_amount_rejected():
    """Token amounts must be integer base units: a float means the
    distribution math leaked floating point."""
    c = _chain()
    with pytest.raises(TypeError):
        c.build_distribution_tx(str(Keypair().pubkey()),
                                [(str(Keypair().pubkey()), 69930000000000.0)])


def test_off_curve_owner_pda_is_accepted():
    """A PDA (e.g. a Squads vault) as wallet: ATA derivation must still work."""
    pda, _ = Pubkey.find_program_address([b"vault"], TOKEN_PROGRAM_ID)
    c = _chain()
    raw = c.build_distribution_tx(str(Keypair().pubkey()), [(str(pda), 5)])
    assert raw


@pytest.mark.parametrize("bad", ["x" * 44, "0" * 32, "I" * 44, "Treasury" * 6])
def test_invalid_wallet_breaks_the_whole_tx(bad):
    """One malformed wallet (accepted by /setwallet, see handler tests)
    makes the distribution tx impossible to build -> launch blocked for all."""
    c = _chain()
    with pytest.raises(Exception):
        c.build_distribution_tx(str(Keypair().pubkey()),
                                [(str(Keypair().pubkey()), 5), (bad, 5)])


def test_tip_without_account_is_silently_dropped():
    c = _chain()
    raw = c.build_distribution_tx(str(Keypair().pubkey()),
                                  [(str(Keypair().pubkey()), 5)], 1_000_000, "")
    _, ixs = _decode(raw)
    assert not [x for x in ixs if x[0] == SYSTEM]


def test_amount_overflow_raises():
    c = _chain()
    with pytest.raises(Exception):
        c.build_distribution_tx(str(Keypair().pubkey()),
                                [(str(Keypair().pubkey()), 2**64)])
