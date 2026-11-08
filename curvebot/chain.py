"""Solana chain operations (minimal: one address, SOL in / SOL out).

No on-chain token exists: balances are database entries only.
Imports solana libs lazily so pure logic is testable offline.
"""

from __future__ import annotations

LAMPORTS = 1_000_000_000


class Chain:
    def __init__(self, rpc_url: str, keypair_path: str):
        import json

        from solders.keypair import Keypair
        from solana.rpc.async_api import AsyncClient

        with open(keypair_path) as f:
            self.keypair = Keypair.from_json(f.read())
        self.client = AsyncClient(rpc_url)

    @property
    def pubkey(self) -> str:
        return str(self.keypair.pubkey())

    async def balance_sol(self) -> float:
        resp = await self.client.get_balance(self.keypair.pubkey())
        return resp.value / LAMPORTS

    async def send_sol(self, destination_wallet: str, amount_sol: float) -> str:
        """Send SOL from the treasury address. Returns the tx signature."""
        from solders.pubkey import Pubkey
        from solders.system_program import TransferParams, transfer
        from solders.transaction import Transaction

        ix = transfer(TransferParams(
            from_pubkey=self.keypair.pubkey(),
            to_pubkey=Pubkey.from_string(destination_wallet),
            lamports=int(round(amount_sol * LAMPORTS)),
        ))
        txn = Transaction.new_signed_with_payer([ix], self.keypair)
        resp = await self.client.send_transaction(txn)
        return str(resp.value)

    async def incoming_sol_transfers(self, max_signatures: int = 25) -> list[dict]:
        """SOL transfers TO the treasury address. Returns dicts with
        signature, slot, sender, amount_sol."""
        sigs = await self.client.get_signatures_for_address(
            self.keypair.pubkey(), limit=max_signatures
        )
        out = []
        for s in sigs.value:
            if s.err:
                continue
            tx = await self.client.get_transaction(s.signature)
            if tx.value is None:
                continue
            transfer = self._find_incoming_transfer(tx.value)
            if transfer is not None:
                out.append({
                    "signature": str(s.signature),
                    "slot": s.slot,
                    "sender": transfer["sender"],
                    "amount_sol": transfer["amount_sol"],
                })
        return out

    def _find_incoming_transfer(self, tx_value) -> dict | None:
        from solders.pubkey import Pubkey

        try:
            keys = tx_value.transaction.message.account_keys
            me = self.keypair.pubkey()
            for instr in tx_value.transaction.message.instructions:
                data = bytes(instr.data)
                # System program Transfer: instruction id 2 (little-endian u32)
                if len(data) < 12 or data[:4] != b"\x02\x00\x00\x00":
                    continue
                lamports = int.from_bytes(data[4:12], "little")
                if lamports <= 0:
                    continue
                src = keys[instr.accounts[0]]
                dst = keys[instr.accounts[1]]
                if dst == me:
                    return {"sender": str(src), "amount_sol": lamports / LAMPORTS}
        except Exception:
            return None
        return None

    # --- launch support (pump.fun) ---

    async def sign_send_b58(self, tx_b58: str, extra_signers=None) -> str:
        """Sign a base58 PumpPortal transaction and send it via our RPC."""
        import base64

        import base58
        from solders.transaction import VersionedTransaction

        raw = base58.b58decode(tx_b58)
        txn = VersionedTransaction.from_bytes(raw)
        signers = [self.keypair] + list(extra_signers or [])
        txn = VersionedTransaction(txn.message, signers)
        resp = await self.client.send_transaction(txn)
        return str(resp.value)

    async def token_balance(self, mint: str) -> float:
        """Token balance of the treasury's ATA for `mint` (ui units)."""
        from spl.token.async_client import AsyncToken
        from spl.token.constants import TOKEN_PROGRAM_ID

        token = AsyncToken(self.client, Pubkey_from(mint), TOKEN_PROGRAM_ID,
                           self.keypair)
        ata = token.get_or_create_associated_token_account(self.keypair.pubkey())
        acct = await token.get_account_by_owner(self.keypair.pubkey())
        if acct.value is None:
            return 0.0
        return acct.value.amount / 10 ** self_token_decimals(self)

    async def transfer_tokens(self, mint: str, destination_wallet: str,
                              ui_amount: float) -> str:
        """Create the destination ATA if needed and transfer tokens."""
        from spl.token.async_client import AsyncToken
        from spl.token.constants import TOKEN_PROGRAM_ID

        token = AsyncToken(self.client, Pubkey_from(mint), TOKEN_PROGRAM_ID,
                           self.keypair)
        dest_ata = await token.create_associated_token_account(
            Pubkey_from(destination_wallet), self.keypair)
        src_ata = await token.create_associated_token_account(
            self.keypair.pubkey(), self.keypair)
        resp = await token.transfer(src_ata, dest_ata, self.keypair,
                                    int(round(ui_amount * 10 ** 9)))
        return str(resp.value)


    async def transfer_tokens_batch(self, mint: str,
                                    recipients: list[tuple[str, float]]) -> str:
        """All transfers in ONE transaction: create destination ATAs
        (idempotent) and transfer tokens to every recipient. Minimizes the
        custody window to a single slot after the dev buy."""
        import solders.transaction as txmod
        from spl.token.constants import TOKEN_PROGRAM_ID
        from spl.token.instructions import (
            create_idempotent_associated_token_account,
            get_associated_token_address,
            transfer_checked,
        )
        from spl.token.instructions import models

        mint_pk = Pubkey_from(mint)
        owner = self.keypair.pubkey()
        src_ata = get_associated_token_address(owner, mint_pk)
        ixs = []
        for wallet, amount in recipients:
            if amount <= 0:
                continue
            dest_pk = Pubkey_from(wallet)
            dest_ata = get_associated_token_address(dest_pk, mint_pk)
            ixs.append(create_idempotent_associated_token_account(
                payer=owner, owner=dest_pk, mint=mint_pk,
            ))
            ixs.append(transfer_checked(models.TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=src_ata,
                mint=mint_pk,
                dest=dest_ata,
                owner=owner,
                amount=int(round(amount * 10 ** 9)),
                decimals=9,
                signers=[owner],
            )))
        if not ixs:
            return ""
        bh = await self.client.get_latest_blockhash()
        txn = txmod.Transaction.new_signed_with_payer(
            ixs, owner, [self.keypair], bh.value.blockhash)
        resp = await self.client.send_transaction(txn)
        return str(resp.value)

    def _system_program(self):
        from solders.pubkey import Pubkey
        return Pubkey.from_string("11111111111111111111111111111111")
def self_token_decimals(chain) -> int:
    return 9  # pump.fun tokens use 9 decimals



def Pubkey_from(s: str):
    from solders.pubkey import Pubkey
    return Pubkey.from_string(s)
