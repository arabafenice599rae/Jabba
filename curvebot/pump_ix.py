"""pump.fun create_v2 + buy_v3 built directly on the pump program, no
PumpPortal (no 0.5% fee, no third-party router in the path of the funds).

Source: the official IDL and docs in github.com/pump-fun/pump-public-docs
(idl/pump.json, docs/instructions/COIN_CREATION.md, TRADE_V3.md, Oct 2026).
Validated by tests/test_fork_mainnet.py against the real programs cloned
from mainnet: if pump.fun changes an account or a discriminator, that test
fails before a launch does.

The create tx is: create_v2 -> ATA of the treasury (Token-2022, idempotent,
buy_v3 does not create it) -> buy_v3 of an EXACT token amount with a SOL cap.
No compute-budget instructions: inside a Jito bundle the tip pays for
inclusion and three instructions fit the default compute limit.
"""

from __future__ import annotations

import struct

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MAYHEM_PROGRAM = "MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e"
PUMP_FEES_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM = "11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"

CREATE_V2 = bytes.fromhex("d6904cec5f8b31b4")
BUY_V3 = bytes.fromhex("07051dc4f5176550")

# docs/FEE_RECIPIENTS.md: any of the 8 buyback recipients; for a SOL-paired
# coin the wallet itself is passed
BUYBACK_FEE_RECIPIENTS = [
    "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD",
    "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
    "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
    "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
    "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6",
    "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
    "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD",
    "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
]

MAX_NAME, MAX_SYMBOL, MAX_URI = 32, 13, 200


def _pk(s):
    from solders.pubkey import Pubkey
    return s if isinstance(s, Pubkey) else Pubkey.from_string(s)


def pda(seeds: list[bytes], program: str = PUMP_PROGRAM):
    from solders.pubkey import Pubkey
    return Pubkey.find_program_address(seeds, _pk(program))[0]


def ata(owner, mint, token_program: str):
    from solders.pubkey import Pubkey
    return Pubkey.find_program_address(
        [bytes(_pk(owner)), bytes(_pk(token_program)), bytes(_pk(mint))],
        _pk(ATA_PROGRAM))[0]


def bonding_curve(mint):
    return pda([b"bonding-curve", bytes(_pk(mint))])


def _borsh_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def create_v2_ix(mint, user, name: str, symbol: str, uri: str, creator=None):
    """create_v2 for a SOL-paired, non-mayhem coin (16 IDL accounts, no
    remaining accounts). Trailing optional args are omitted (= false/0)."""
    from solders.instruction import AccountMeta, Instruction

    if not (0 < len(name) <= MAX_NAME and 0 < len(symbol) <= MAX_SYMBOL
            and 0 < len(uri) <= MAX_URI):
        raise ValueError("nome/simbolo/uri fuori dai limiti pump.fun (32/13/200)")
    mint, user = _pk(mint), _pk(user)
    creator = _pk(creator) if creator is not None else user
    curve = bonding_curve(mint)
    sol_vault = pda([b"sol-vault"], MAYHEM_PROGRAM)

    def m(k, w=False, s=False):
        return AccountMeta(_pk(k), is_signer=s, is_writable=w)
    accounts = [
        m(mint, w=True, s=True),
        m(pda([b"mint-authority"])),
        m(curve, w=True),
        m(ata(curve, mint, TOKEN_2022), w=True),
        m(pda([b"global"])),
        m(user, w=True, s=True),
        m(SYSTEM),
        m(TOKEN_2022),
        m(ATA_PROGRAM),
        m(MAYHEM_PROGRAM, w=True),
        m(pda([b"global-params"], MAYHEM_PROGRAM)),
        m(sol_vault, w=True),
        m(pda([b"mayhem-state", bytes(mint)], MAYHEM_PROGRAM), w=True),
        m(ata(sol_vault, mint, TOKEN_2022), w=True),
        m(pda([b"__event_authority"])),
        m(PUMP_PROGRAM),
    ]
    data = (CREATE_V2 + _borsh_str(name) + _borsh_str(symbol) + _borsh_str(uri)
            + bytes(creator) + b"\x00")                     # is_mayhem_mode = false
    return Instruction(_pk(PUMP_PROGRAM), data, accounts)


def buy_v3_ix(mint, user, amount: int, max_sol_cost: int,
              buyback_fee_recipient: str = BUYBACK_FEE_RECIPIENTS[0]):
    """buy_v3: buy EXACTLY `amount` base units, paying at most
    `max_sol_cost` lamports including protocol and creator fee."""
    from solders.instruction import AccountMeta, Instruction

    if amount <= 0 or max_sol_cost <= 0:
        raise ValueError("amount e max_sol_cost devono essere > 0")
    mint, user = _pk(mint), _pk(user)
    curve = bonding_curve(mint)

    def m(k, w=False, s=False):
        return AccountMeta(_pk(k), is_signer=s, is_writable=w)
    accounts = [
        m(pda([b"global"])),
        m(mint),
        m(WSOL),
        m(TOKEN_2022),
        m(SPL_TOKEN),
        m(curve, w=True),
        m(ata(curve, mint, TOKEN_2022), w=True),
        m(ata(curve, WSOL, SPL_TOKEN), w=True),
        m(user, w=True, s=True),
        m(ata(user, mint, TOKEN_2022), w=True),
        m(ata(user, WSOL, SPL_TOKEN), w=True),
        m(pda([b"user_volume_accumulator", bytes(user)]), w=True),
        m(pda([b"fee_config", bytes(_pk(PUMP_PROGRAM))], PUMP_FEES_PROGRAM)),
        m(buyback_fee_recipient, w=True),
        m(SYSTEM),
        m(pda([b"__event_authority"])),
        m(PUMP_PROGRAM),
    ]
    data = BUY_V3 + struct.pack("<QQ", amount, max_sol_cost)
    return Instruction(_pk(PUMP_PROGRAM), data, accounts)


def create_and_buy_ixs(mint, user, name: str, symbol: str, uri: str,
                       amount: int, max_sol_cost: int) -> list:
    """The three instructions of the launch's first transaction."""
    from spl.token.instructions import create_idempotent_associated_token_account

    return [
        create_v2_ix(mint, user, name, symbol, uri),
        create_idempotent_associated_token_account(
            payer=_pk(user), owner=_pk(user), mint=_pk(mint),
            token_program_id=_pk(TOKEN_2022)),
        buy_v3_ix(mint, user, amount, max_sol_cost),
    ]

# --- creator fee sweep (docs/instructions/COLLECT_CREATOR_FEE.md, Oct 2026) ---
# Discriminators measured from the official IDLs (idl/pump.json,
# idl/pump_amm.json) — NOT from secondary sources.
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
COLLECT_CREATOR_FEE_V2 = bytes([207, 17, 138, 242, 4, 34, 19, 56])
COLLECT_COIN_CREATOR_FEE = bytes([160, 57, 89, 42, 181, 139, 43, 66])


def creator_vault(creator):
    """Pump PDA [b"creator-vault", creator]: holds curve-phase fees."""
    return pda([b"creator-vault", bytes(_pk(creator))])


def creator_vault_balance_accounts(creator):
    """The two accounts to read for a balance report (SOL-paired)."""
    vault = creator_vault(creator)
    return vault, ata(vault, WSOL, SPL_TOKEN)


def collect_creator_fee_v2_ix(creator):
    """Sweep the BONDING-CURVE creator vault (SOL-paired): lamports go
    from creator_vault to the creator's wallet. Permissionless."""
    from solders.instruction import AccountMeta, Instruction

    creator = _pk(creator)
    vault = creator_vault(creator)

    def m(k, w=False, s=False):
        return AccountMeta(_pk(k), is_signer=s, is_writable=w)

    accounts = [
        m(creator, w=True, s=True),                       # creator (signer)
        m(ata(creator, WSOL, SPL_TOKEN), w=True),         # creator_token_account
        m(vault, w=True),                                 # creator_vault
        m(ata(vault, WSOL, SPL_TOKEN), w=True),           # creator_vault_token_account
        m(WSOL),                                          # quote_mint
        m(SPL_TOKEN),                                     # quote_token_program
        m(ATA_PROGRAM),                                  # associated_token_program
        m(SYSTEM),                                       # system_program
        m(pda([b"__event_authority"])),                  # event_authority
        m(PUMP_PROGRAM),                                 # program
    ]
    return Instruction(_pk(PUMP_PROGRAM), COLLECT_CREATOR_FEE_V2, accounts)


def collect_coin_creator_fee_ix(creator):
    """Sweep the PUMPSWAP (post-graduation) coin-creator vault ATA
    (SOL-paired) into the creator's WSOL account. Permissionless; fails
    harmlessly if the coin has not graduated (vault ATA absent)."""
    from solders.instruction import AccountMeta, Instruction

    creator = _pk(creator)
    vault_authority = pda([b"creator_vault", bytes(creator)], PUMP_AMM)

    def m(k, w=False, s=False):
        return AccountMeta(_pk(k), is_signer=s, is_writable=w)

    accounts = [
        m(WSOL),                                          # quote_mint
        m(SPL_TOKEN),                                     # quote_token_program
        m(creator, s=True),                                # coin_creator
        m(vault_authority),                               # coin_creator_vault_authority
        m(ata(vault_authority, WSOL, SPL_TOKEN), w=True), # coin_creator_vault_ata
        m(ata(creator, WSOL, SPL_TOKEN), w=True),         # coin_creator_token_account
        m(pda([b"__event_authority"], PUMP_AMM)),         # event_authority (AMM)
        m(PUMP_AMM),                                      # program
    ]
    return Instruction(_pk(PUMP_AMM), COLLECT_COIN_CREATOR_FEE, accounts)
