"""A tiny AsyncClient stand-in backed by LiteSVM: the bot's real `Chain`
code builds, signs and sends transactions that are EXECUTED by a real SVM
(system, SPL Token, Token-2022 and ATA programs), offline.

Only the methods the bot calls are implemented, returning the same shapes
solana-py returns (checked against real solders RPC objects in
test_stress_monitor.py)."""
from __future__ import annotations

from types import SimpleNamespace

from solders.litesvm import LiteSVM
from solders.pubkey import Pubkey
from solders.transaction import Transaction, VersionedTransaction


class TxFailed(RuntimeError):
    pass


def _libm():
    import ctypes
    import ctypes.util
    try:
        return ctypes.CDLL(ctypes.util.find_library("m") or "libm.so.6")
    except OSError:
        return None


_LIBM = _libm()
_FENV = None
if _LIBM is not None:                   # captured at import, before any LiteSVM
    import ctypes as _ct
    _FENV = _ct.create_string_buffer(64)  # >= sizeof(fenv_t) on every libc
    _LIBM.fegetenv(_FENV)


def _restore_fp_rounding() -> None:
    """Executing SBF programs in LiteSVM leaves the floating-point environment
    (SSE control register) changed for this process: numpy's floor_divide
    then returns nan, which breaks e.g. matplotlib's tick locator. Restore
    the environment captured at import time."""
    if _LIBM is not None and _FENV is not None:
        _LIBM.fesetenv(_FENV)


class LiteRPC:
    def __init__(self, svm: LiteSVM | None = None):
        self.svm = svm or LiteSVM()
        self.history: list[tuple[str, VersionedTransaction, object]] = []  # newest last

    # --- solana-py AsyncClient surface used by curvebot ---

    async def get_latest_blockhash(self):
        return SimpleNamespace(value=SimpleNamespace(blockhash=self.svm.latest_blockhash()))

    async def get_balance(self, pk: Pubkey):
        return SimpleNamespace(value=self.svm.get_balance(pk) or 0)

    async def get_minimum_balance_for_rent_exemption(self, size: int):
        return SimpleNamespace(value=self.svm.minimum_balance_for_rent_exemption(size))

    async def send_transaction(self, tx):
        return SimpleNamespace(value=self.execute(tx))

    async def get_signatures_for_address(self, pk: Pubkey, limit=None, **kw):
        rows = [SimpleNamespace(signature=sig, slot=i, err=err)
                for i, (sig, tx, err) in enumerate(self.history)
                if pk in tx.message.account_keys]
        rows.reverse()                                   # newest first, like RPC
        return SimpleNamespace(value=rows[:limit] if limit else rows)

    async def get_transaction(self, sig, encoding="json",
                              max_supported_transaction_version=None, **kw):
        assert encoding == "base64" and max_supported_transaction_version == 0
        for s, tx, _ in self.history:
            if s == sig:
                # same nesting as EncodedConfirmedTransactionWithStatusMeta
                return SimpleNamespace(value=SimpleNamespace(
                    transaction=SimpleNamespace(transaction=tx, meta=None)))
        return SimpleNamespace(value=None)

    # --- helpers ---

    def execute(self, tx, raise_on_error: bool = True):
        if isinstance(tx, (bytes, bytearray)):
            tx = VersionedTransaction.from_bytes(bytes(tx))
        elif isinstance(tx, Transaction):
            tx = VersionedTransaction.from_bytes(bytes(tx))
        res = self.svm.send_transaction(tx)
        _restore_fp_rounding()
        sig = str(tx.signatures[0])
        err = getattr(res, "err", None)
        err = err() if callable(err) else err
        self.history.append((sig, tx, err))
        if err is not None and raise_on_error:
            meta = res.meta() if callable(res.meta) else res.meta
            logs = meta.logs() if callable(meta.logs) else meta.logs
            raise TxFailed(f"{err}\n" + "\n".join(logs[-8:]))
        return sig

    def token_balance(self, ata: Pubkey) -> int:
        acc = self.svm.get_account(ata)
        if acc is None:
            return 0
        return int.from_bytes(bytes(acc.data)[64:72], "little")


class MainnetFork(LiteRPC):
    """LiteRPC whose missing accounts are cloned from mainnet on first use
    (programs with their programdata, address lookup tables included).
    Read-only towards mainnet: nothing is ever sent there."""

    UPGRADEABLE = "BPFLoaderUpgradeab1e11111111111111111111111"

    def __init__(self, url: str = "https://api.mainnet-beta.solana.com"):
        super().__init__(LiteSVM().with_blockhash_check(False))
        self.url = url
        self.tried: set[str] = set()
        self._load_sysvars()

    def _load_sysvars(self) -> None:
        """Mainnet's Rent and Clock, not LiteSVM's defaults: since 2026
        mainnet rent is ~27% lower than the classic 3480 x 2 years."""
        from solders.clock import Clock
        from solders.rent import Rent
        got = self._fetch(["SysvarRent111111111111111111111111111111111",
                           "SysvarC1ock11111111111111111111111111111111"])
        rent = got["SysvarRent111111111111111111111111111111111"]
        clock = got["SysvarC1ock11111111111111111111111111111111"]
        if rent:
            self.svm.set_rent(Rent.from_bytes(rent["raw"]))
        if clock:
            self.svm.set_clock(Clock.from_bytes(clock["raw"]))

    def _rpc(self, method, params):
        import json
        import urllib.request
        req = urllib.request.Request(
            self.url, headers={"Content-Type": "application/json"},
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                             "params": params}).encode())
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read())
        if "error" in out:
            raise RuntimeError(out["error"])
        return out["result"]

    def _fetch(self, keys: list[str]) -> dict:
        import base64
        res = {}
        for i in range(0, len(keys), 100):
            chunk = keys[i:i + 100]
            vals = self._rpc("getMultipleAccounts", [chunk, {"encoding": "base64"}])["value"]
            for k, v in zip(chunk, vals):
                if v:
                    v["raw"] = base64.b64decode(v["data"][0])
                res[k] = v
        return res

    def clone(self, keys) -> None:
        from solders.account import Account
        todo = [str(k) for k in keys
                if str(k) not in self.tried and self.svm.get_account(Pubkey.from_string(str(k))) is None
                and not str(k).startswith("Sysvar")]
        if not todo:
            return
        self.tried.update(todo)
        got = {k: v for k, v in self._fetch(todo).items() if v}
        extra = [str(Pubkey.from_bytes(v["raw"][4:36])) for v in got.values()
                 if v["owner"] == self.UPGRADEABLE and v["executable"]]
        got.update({k: v for k, v in self._fetch(extra).items() if v})
        self.tried.update(extra)
        for k, v in sorted(got.items(), key=lambda kv: kv[1]["executable"]):  # programdata first
            self.svm.set_account(Pubkey.from_string(k), Account(
                lamports=v["lamports"], data=v["raw"], owner=Pubkey.from_string(v["owner"]),
                executable=v["executable"], rent_epoch=0))

    def clone_for_tx(self, tx: VersionedTransaction) -> None:
        from solders.address_lookup_table_account import AddressLookupTable
        msg = tx.message
        self.clone(msg.account_keys)
        for lookup in getattr(msg, "address_table_lookups", None) or []:
            self.clone([lookup.account_key])
            acc = self.svm.get_account(lookup.account_key)
            table = AddressLookupTable.deserialize(bytes(acc.data))
            idx = list(lookup.writable_indexes) + list(lookup.readonly_indexes)
            self.clone([table.addresses[i] for i in idx])

    async def get_account_info(self, pk: Pubkey, **kw):
        self.clone([pk])
        return SimpleNamespace(value=self.svm.get_account(pk))

    def execute(self, tx, raise_on_error: bool = True):
        if isinstance(tx, (bytes, bytearray)):
            tx = VersionedTransaction.from_bytes(bytes(tx))
        self.clone_for_tx(tx)
        return super().execute(tx, raise_on_error)
