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
