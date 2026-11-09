"""Offline checks for the creator-fee sweep instructions.
Discriminators measured from the official IDLs (pump.json / pump_amm.json)."""
from solders.keypair import Keypair

from curvebot.pump_ix import (
    COLLECT_COIN_CREATOR_FEE,
    COLLECT_CREATOR_FEE_V2,
    PUMP_AMM,
    PUMP_PROGRAM,
    collect_coin_creator_fee_ix,
    collect_creator_fee_v2_ix,
    creator_vault,
)


def test_discriminators_measured():
    assert COLLECT_CREATOR_FEE_V2 == bytes([207, 17, 138, 242, 4, 34, 19, 56])
    assert COLLECT_COIN_CREATOR_FEE == bytes([160, 57, 89, 42, 181, 139, 43, 66])


def test_curve_sweep_instruction_layout():
    creator = Keypair().pubkey()
    ix = collect_creator_fee_v2_ix(creator)
    assert str(ix.program_id) == PUMP_PROGRAM
    assert len(ix.accounts) == 10          # IDL: 10 accounts
    assert ix.data == COLLECT_CREATOR_FEE_V2
    # creator is the signer (index 0)
    assert ix.accounts[0].is_signer
    assert str(ix.accounts[0].pubkey) == str(creator)


def test_amm_sweep_instruction_layout():
    creator = Keypair().pubkey()
    ix = collect_coin_creator_fee_ix(creator)
    assert str(ix.program_id) == PUMP_AMM
    assert len(ix.accounts) == 8          # IDL: 8 accounts
    assert ix.data == COLLECT_COIN_CREATOR_FEE
    assert ix.accounts[2].is_signer
    assert str(ix.accounts[2].pubkey) == str(creator)


def test_creator_vault_deterministic():
    creator = Keypair().pubkey()
    v1 = creator_vault(creator)
    v2 = creator_vault(creator)
    assert v1 == v2
    assert v1 != creator_vault(Keypair().pubkey())
