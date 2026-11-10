"""Solana chain operations: one treasury address, SOL in / SOL out, and the
token distribution transactions of the atomic launch.

Imports solana libs lazily so pure logic is testable offline.

pump.fun mints are Token-2022 (create_v2): every token instruction and every
ATA derivation must use the mint's own token program, never a hardcoded one.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("chain")

LAMPORTS = 1_000_000_000
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_DECIMALS = 6

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
JITO_URL = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"


def Pubkey_from(s: str):
    from solders.pubkey import Pubkey
    return Pubkey.from_string(s)


def is_valid_wallet(s: str) -> bool:
    """A base58 32-byte public key (on- or off-curve, PDAs allowed)."""
    try:
        return len(bytes(Pubkey_from(s))) == 32 and s != SYSTEM_PROGRAM
    except Exception:  # noqa: BLE001 - any parse error means "not a pubkey"
        return False


class Chain:
    def __init__(self, rpc_url: str, keypair_path: str):
        from solana.rpc.async_api import AsyncClient
        from solders.keypair import Keypair

        with open(keypair_path) as f:
            self.keypair = Keypair.from_json(f.read())
        self.client: Any = AsyncClient(rpc_url)
        self._blockhash: Any = None

    @property
    def pubkey(self) -> str:
        return str(self.keypair.pubkey())

    async def balance_sol(self) -> float:
        resp = await self.client.get_balance(self.keypair.pubkey())
        return resp.value / LAMPORTS

    async def refresh_blockhash(self) -> None:
        bh = await self.client.get_latest_blockhash()
        self._blockhash = bh.value.blockhash

    def _sign(self, ixs: list, extra_signers=None):
        """Signed tx for `ixs` with the cached blockhash, treasury as payer."""
        from solders.message import Message
        from solders.transaction import VersionedTransaction

        msg = Message.new_with_blockhash(ixs, self.keypair.pubkey(), self._blockhash)
        return VersionedTransaction(msg, [self.keypair, *(extra_signers or [])])

    async def send_sol(self, destination_wallet: str, amount_sol: float) -> str:
        """Send SOL from the treasury address. Returns the tx signature."""
        from solders.system_program import TransferParams, transfer

        lamports = round(amount_sol * LAMPORTS)
        if lamports <= 0:
            raise ValueError("importo nullo")
        ix = transfer(TransferParams(
            from_pubkey=self.keypair.pubkey(),
            to_pubkey=Pubkey_from(destination_wallet),
            lamports=lamports,
        ))
        await self.refresh_blockhash()
        resp = await self.client.send_transaction(self._sign([ix]))
        return str(resp.value)

    # --- deposits & refunds seen on chain ---

    async def incoming_sol_transfers(self, max_pages: int = 10,
                                     is_seen=None) -> list[dict]:
        """See scan_incoming(); returns only the transfers."""
        transfers, _ = await self.scan_incoming(max_pages, is_seen)
        return transfers

    async def scan_incoming(self, max_pages: int = 10,
                            is_seen=None) -> tuple[list[dict], list[str]]:
        """FINALIZED SOL transfers TO the treasury, one entry per transfer
        (a tx may carry several): dicts with signature, index, slot, sender,
        amount_sol; plus the signatures fully examined (to mark as seen).

        Walks the signature history back page by page. `is_seen(signature)`
        lets the caller skip transactions it has already processed: their
        get_transaction is not repeated, and paging stops at the first page
        made only of seen signatures (history is newest-first)."""
        from solana.rpc.core import Finalized

        sigs: list = []
        seen_here: set[str] = set()
        before = None
        for _ in range(max_pages):
            page = (await self.client.get_signatures_for_address(
                self.keypair.pubkey(), before=before, commitment=Finalized,
            )).value or []
            new = [s for s in page if str(s.signature) not in seen_here]
            if not new:
                break
            seen_here.update(str(s.signature) for s in new)
            fresh = [s for s in new if not (is_seen and is_seen(str(s.signature)))]
            sigs.extend(fresh)
            if not fresh or len(page) < 2:
                break                       # reached already-processed history
            before = page[-1].signature
        out: list[dict] = []
        examined: list[str] = []            # fully read: safe to mark as seen
        for s in sigs:
            if s.err:
                examined.append(str(s.signature))
                continue
            tx = await self.client.get_transaction(
                s.signature, encoding="base64",
                max_supported_transaction_version=0, commitment=Finalized)
            if tx.value is None:
                continue                    # not readable yet: retried next poll
            examined.append(str(s.signature))
            for i, t in enumerate(self._find_incoming_transfers(tx.value)):
                out.append({"signature": str(s.signature), "index": i,
                            "slot": s.slot, "sender": t["sender"],
                            "amount_sol": t["amount_sol"]})
        return out, examined

    async def transfers_to_wallet(self, wallet: str,
                                  max_signatures: int = 25) -> list[dict]:
        """FINALIZED SOL transfers from the treasury TO `wallet`: used to
        resolve refunds whose outcome is unknown."""
        from solana.rpc.core import Finalized

        target = Pubkey_from(wallet)
        sigs = await self.client.get_signatures_for_address(
            target, limit=max_signatures, commitment=Finalized)
        out = []
        for s in sigs.value or []:
            if s.err:
                continue
            tx = await self.client.get_transaction(
                s.signature, encoding="base64",
                max_supported_transaction_version=0, commitment=Finalized)
            if tx.value is None:
                continue
            me = self.keypair.pubkey()
            for src, dst, lamports in self._system_transfers(tx.value):
                if src == me and dst == target:
                    out.append({"signature": str(s.signature),
                                "amount_sol": lamports / LAMPORTS})
        return out

    def _find_incoming_transfers(self, tx_value) -> list[dict]:
        """All top-level System Program transfers TO the treasury."""
        me = self.keypair.pubkey()
        return [{"sender": str(src), "amount_sol": lamports / LAMPORTS}
                for src, dst, lamports in self._system_transfers(tx_value)
                if dst == me]

    @staticmethod
    def _system_transfers(tx_value) -> list[tuple]:
        """(src, dst, lamports) of every top-level System Program Transfer.

        `tx_value` is the RPC `value` (EncodedConfirmedTransactionWithStatusMeta):
        the message lives at value.transaction.transaction.message. Works for
        base64 (VersionedTransaction) and json (UiTransaction) encodings.
        Only the System Program counts: any other program with the same data
        layout is ignored."""
        import base58
        from solders.pubkey import Pubkey

        system_program = Pubkey.from_string(SYSTEM_PROGRAM)
        found = []
        try:
            inner = tx_value.transaction
            tx = getattr(inner, "transaction", inner)
            message = tx.message
            keys = [k if isinstance(k, Pubkey) else Pubkey.from_string(str(k))
                    for k in message.account_keys]
            for instr in message.instructions:
                if keys[instr.program_id_index] != system_program:
                    continue
                data = instr.data
                data = base58.b58decode(data) if isinstance(data, str) else bytes(data)
                # System program Transfer: instruction id 2 (u32 LE) + u64 lamports
                if len(data) != 12 or data[:4] != b"\x02\x00\x00\x00":
                    continue
                lamports = int.from_bytes(data[4:12], "little")
                if lamports > 0:
                    found.append((keys[instr.accounts[0]], keys[instr.accounts[1]], lamports))
        except (AttributeError, IndexError, TypeError, ValueError):
            log.exception("tx non interpretabile, ignorata")
            return []
        return found

    # --- launch support (pump.fun) ---

    def build_create_tx(self, mint_kp, name: str, symbol: str, uri: str,
                        amount: int, max_sol_cost: int) -> bytes:
        """create_v2 + treasury ATA + buy_v3 of EXACTLY `amount` base units,
        signed by the treasury (payer, creator, buyer) and the mint keypair.
        Built directly on the pump.fun program (see pump_ix.py)."""
        from .pump_ix import create_and_buy_ixs

        ixs = create_and_buy_ixs(mint_kp.pubkey(), self.keypair.pubkey(),
                                 name, symbol, uri, amount, max_sol_cost)
        return bytes(self._sign(ixs, extra_signers=[mint_kp]))

    def build_distribution_tx(self, mint: str,
                              recipients: list[tuple[str, int]],
                              tip_lamports: int = 0,
                              tip_account: str = "",
                              token_program: str = TOKEN_2022_PROGRAM) -> bytes:
        """All transfers in ONE transaction: create destination ATAs
        (idempotent) and transfer tokens to every recipient. Amounts are
        INTEGER base units (10**6). `token_program` must be the mint's
        program (Token-2022 for pump.fun). Optionally appends a Jito tip
        transfer. Returns signed tx bytes."""
        from spl.token.instructions import (
            create_idempotent_associated_token_account,
            get_associated_token_address,
            models,
            transfer_checked,
        )

        program = Pubkey_from(token_program)
        mint_pk = Pubkey_from(mint)
        owner = self.keypair.pubkey()
        src_ata = get_associated_token_address(owner, mint_pk, program)
        ixs = []
        for wallet, amount in recipients:
            if type(amount) is not int:
                raise TypeError(f"importo non intero per {wallet}: {amount!r}")
            if amount <= 0:
                continue
            dest_pk = Pubkey_from(wallet)
            dest_ata = get_associated_token_address(dest_pk, mint_pk, program)
            ixs.append(create_idempotent_associated_token_account(
                payer=owner, owner=dest_pk, mint=mint_pk, token_program_id=program,
            ))
            ixs.append(transfer_checked(models.TransferCheckedParams(
                program_id=program,
                source=src_ata,
                mint=mint_pk,
                dest=dest_ata,
                owner=owner,
                amount=amount,
                decimals=TOKEN_DECIMALS,
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
        return bytes(self._sign(ixs))

    async def transfer_tokens_batch(self, mint: str,
                                    recipients: list[tuple[str, int]],
                                    token_program: str = TOKEN_2022_PROGRAM) -> str:
        await self.refresh_blockhash()
        signed = self.build_distribution_tx(mint, recipients,
                                            token_program=token_program)
        if not signed:
            return ""
        from solders.transaction import VersionedTransaction
        resp = await self.client.send_transaction(VersionedTransaction.from_bytes(signed))
        return str(resp.value)

    async def send_bundle(self, http, txs_bytes: list[bytes]) -> str:
        """Submit signed txs as an ATOMIC Jito bundle. Returns bundle id."""
        import base64

        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "sendBundle",
            "params": [[base64.b64encode(t).decode() for t in txs_bytes],
                       {"encoding": "base64"}],
        }
        async with http.post(JITO_URL, json=payload) as r:
            data = await r.json()
        if "error" in data:
            raise RuntimeError(f"jito bundle rejected: {data['error']}")
        return data.get("result", "")

    async def fetch_tip_accounts(self, http) -> list[str]:
        """Tip accounts can rotate: fetch them live, fall back to the list."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts",
                   "params": []}
        try:
            async with http.post(JITO_URL, json=payload) as r:
                data = await r.json()
            if data.get("result"):
                return data["result"]
        except Exception:  # noqa: BLE001 - network fallback is intended
            log.warning("getTipAccounts non disponibile, uso la lista statica")
        return JITO_TIP_ACCOUNTS
