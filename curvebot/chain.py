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
        bh = await self.client.get_latest_blockhash()
        txn = Transaction.new_signed_with_payer(
            [ix], self.keypair.pubkey(), [self.keypair],
            bh.value.blockhash)
        resp = await self.client.send_transaction(txn)
        return str(resp.value)

    async def incoming_sol_transfers(self, max_signatures: int = 25) -> list[dict]:
        """FINALIZED SOL transfers TO the treasury. One entry per
        incoming transfer: dicts with signature, index, slot, sender,
        amount_sol. A tx with multiple transfers yields many entries."""
        from solana.rpc.core import Finalized

        sigs = await self.client.get_signatures_for_address(
            self.keypair.pubkey(), limit=max_signatures,
            commitment=Finalized,
        )
        out = []
        for s in sigs.value:
            if s.err:
                continue
            tx = await self.client.get_transaction(
                s.signature, encoding="base64", commitment=Finalized)
            if tx.value is None:
                continue
            for i, transfer in enumerate(
                    self._find_incoming_transfers(tx.value)):
                out.append({
                    "signature": str(s.signature),
                    "index": i,
                    "slot": s.slot,
                    "sender": transfer["sender"],
                    "amount_sol": transfer["amount_sol"],
                })
        return out

    async def transfers_to_wallet(self, wallet: str,
                                 max_signatures: int = 25) -> list[dict]:
        """FINALIZED SOL transfers from the treasury TO `wallet`.
        Used to resolve refunds with uncertain outcome."""
        from solana.rpc.core import Finalized
        from solders.pubkey import Pubkey

        target = Pubkey.from_string(wallet)
        me = self.keypair.pubkey()
        sigs = await self.client.get_signatures_for_address(
            target, limit=max_signatures, commitment=Finalized)
        out = []
        for s in sigs.value:
            if s.err:
                continue
            tx = await self.client.get_transaction(
                s.signature, encoding="base64", commitment=Finalized)
            if tx.value is None:
                continue
            try:
                msg = tx.value.transaction.transaction.message
                keys = msg.account_keys
                for instr in msg.instructions:
                    data = bytes(instr.data)
                    if len(data) < 12 or data[:4] != b"\x02\x00\x00\x00":
                        continue
                    lamports = int.from_bytes(data[4:12], "little")
                    if lamports <= 0:
                        continue
                    src = keys[instr.accounts[0]]
                    dst = keys[instr.accounts[1]]
                    if src == me and dst == target:
                        out.append({"signature": str(s.signature),
                                    "amount_sol": lamports / LAMPORTS})
            except Exception:
                continue
        return out

    def _find_incoming_transfers(self, tx_value) -> list[dict]:
        """ALL incoming SOL transfers in a finalized tx (a tx may contain
        more than one transfer to us)."""
        try:
            msg = tx_value.transaction.transaction.message
            keys = msg.account_keys
            me = self.keypair.pubkey()
            found = []
            for instr in msg.instructions:
                data = bytes(instr.data)
                if len(data) < 12 or data[:4] != b"\x02\x00\x00\x00":
                    continue
                lamports = int.from_bytes(data[4:12], "little")
                if lamports <= 0:
                    continue
                src = keys[instr.accounts[0]]
                dst = keys[instr.accounts[1]]
                if dst == me:
                    found.append({"sender": str(src),
                                  "amount_sol": lamports / LAMPORTS})
            return found
        except Exception:
            return []
        return None

    # --- launch support (pump.fun) ---

    def sign_b58(self, tx_data, extra_signers=None) -> bytes:
        """Sign a PumpPortal transaction. Accepts raw tx BYTES (the usual
        trade-local single-tx response) or a base58 string (bundle format).
        Returns signed tx bytes."""
        import base58
        from solders.transaction import VersionedTransaction

        if isinstance(tx_data, str):
            raw = base58.b58decode(tx_data)
        elif isinstance(tx_data, (bytes, bytearray)):
            raw = bytes(tx_data)
        else:
            raise TypeError(f"formato tx non supportato: {type(tx_data)}")
        txn = VersionedTransaction.from_bytes(raw)
        signers = [self.keypair] + list(extra_signers or [])
        return bytes(VersionedTransaction(txn.message, signers))

    def build_distribution_tx(self, mint: str,
                              recipients: list[tuple[str, int]],
                              tip_lamports: int = 0,
                              tip_account: str = "") -> bytes:
        """All transfers in ONE transaction: create destination ATAs
        (idempotent) and transfer tokens to every recipient. Amounts are
        in BASE UNITS (10**DECIMALS, i.e. 10**6 for pump.fun tokens).
        Optionally appends a Jito tip transfer. Returns signed tx bytes."""
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
                amount=amount,
                decimals=6,
                signers=[owner],
            )))
        if tip_lamports > 0 and tip_account:
            from solders.system_program import TransferParams, transfer
            ixs.append(transfer(TransferParams(
                from_pubkey=owner,
                to_pubkey=Pubkey_from(tip_account),
                lamports=tip_lamports,
            )))
        if not ixs:
            return b""
        txn = txmod.Transaction.new_signed_with_payer(
            ixs, owner, [self.keypair], self._blockhash)
        return bytes(txn)

    async def refresh_blockhash(self):
        bh = await self.client.get_latest_blockhash()
        self._blockhash = bh.value.blockhash

    async def transfer_tokens_batch(self, mint: str,
                                    recipients: list[tuple[str, float]]) -> str:
        await self.refresh_blockhash()
        signed = self.build_distribution_tx(mint, recipients)
        if not signed:
            return ""
        from solders.transaction import Transaction
        resp = await self.client.send_transaction(Transaction.from_bytes(signed))
        return str(resp.value)

    async def send_bundle(self, http, txs_bytes: list[bytes]) -> str:
        """Submit signed txs as an ATOMIC Jito bundle. Returns bundle id."""
        import base64

        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "sendBundle",
            "params": [[base64.b64encode(t).decode() for t in txs_bytes],
                       {"encoding": "base64"}],
        }
        url = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
        async with http.post(url, json=payload) as r:
            data = await r.json()
        if "error" in data:
            raise RuntimeError(f"jito bundle rejected: {data['error']}")
        return data.get("result", "")

    async def fetch_tip_accounts(self, http) -> list[str]:
        """Tip accounts can rotate: fetch them live, fall back to the list."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts",
                   "params": []}
        url = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
        try:
            async with http.post(url, json=payload) as r:
                data = await r.json()
            if data.get("result"):
                return data["result"]
        except Exception:
            pass
        return JITO_TIP_ACCOUNTS

    def _system_program(self):
        from solders.pubkey import Pubkey
        return Pubkey.from_string("11111111111111111111111111111111")


def Pubkey_from(s: str):
    from solders.pubkey import Pubkey
    return Pubkey.from_string(s)


JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
]
