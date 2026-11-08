#!/usr/bin/env python3
"""Dry-run validation of the Jabba launch flow — ZERO SOL.

Validates against LIVE mainnet WITHOUT sending anything:
  1. On-chain pump.fun curve params vs pump_curve.py
  2. Pinata metadata upload (if PINATA_JWT set)
  3. PumpPortal create+devbuy tx build + client-side signing
  4. simulateTransaction on the signed create tx (no state change)
  5. Distribution tx sizes vs the 1232-byte limit
  6. Full launch cost preflight

Usage:
  python scripts/dryrun.py [--wallets N] [--dev-buy SOL]

The mint keypair is throwaway: the create tx is built but NEVER sent,
so nothing is created on-chain.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k, v)


async def main() -> int:
    from solders.keypair import Keypair

    from curvebot.chain import Chain
    from curvebot.config import Config
    from curvebot.launch import (PumpPortal, TX_SIZE_LIMIT,
                                 estimate_launch_costs, fetch_global_account,
                                 plan_chunks, upload_pinata_metadata)
    from curvebot.pump_curve import params_match, tokens_out_for_sol, verify_params

    load_env()
    cfg = Config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallets", type=int, default=5,
                    help="number of dummy recipients for size checks")
    ap.add_argument("--dev-buy", type=float, default=0.01,
                    help="dev buy SOL used for the simulated create tx")
    args = ap.parse_args()

    print("=== JABBA DRY-RUN (nothing will be sent) ===\n")
    ok = True

    # 1. RPC + on-chain params
    print(f"[1/6] RPC: {cfg.rpc_url}")
    chain = Chain(cfg.rpc_url, cfg.keypair_path)
    print(f"      treasury: {chain.pubkey}")
    try:
        raw = await fetch_global_account(chain)
        onchain = verify_params(raw)
        match = params_match(onchain)
        print(f"      pump.fun global account: fee={onchain['fee_basis_points']} bps, "
              f"VTR={onchain['initial_virtual_token_reserves'] / 1e6:,.0f} tok, "
              f"VSR={onchain['initial_virtual_sol_reserves'] / 1e9} SOL")
        print(f"      params match pump_curve.py: {'YES' if match else 'NO — ABORT'}")
        ok = ok and match
    except Exception as e:
        print(f"      ERRORE: {e}")
        return 1

    # 2. Pinata
    print("\n[2/6] Pinata metadata upload")
    import aiohttp
    async with aiohttp.ClientSession() as http:
        if not cfg.pinata_jwt:
            print("      SKIP: PINATA_JWT non configurato")
        elif not os.path.exists(cfg.token_image_path):
            print(f"      SKIP: immagine {cfg.token_image_path} non trovata")
        else:
            try:
                uri = await upload_pinata_metadata(http, cfg)
                print(f"      OK: {uri}")
            except Exception as e:
                print(f"      ERRORE: {e}")
                ok = False

        # 3. PumpPortal create+devbuy build+sign
        print("\n[3/6] PumpPortal create+devbuy (build + sign, NO send)")
        mint_kp = Keypair()
        mint = str(mint_kp.pubkey())
        token_metadata = {"name": cfg.token_name or "DryRun",
                          "symbol": cfg.token_symbol or "TEST",
                          "uri": "https://ipfs.io/ipfs/bafkreibtestdryrun"}
        portal = PumpPortal(http)
        try:
            create_b58 = await portal.create_with_devbuy(
                chain.pubkey, mint, token_metadata["uri"],
                token_metadata["name"], token_metadata["symbol"],
                args.dev_buy, cfg.launch_slippage, cfg.launch_priority_fee)
            signed = chain.sign_b58(create_b58, extra_signers=[mint_kp])
            print(f"      OK: tx firmata, {len(signed)} byte, mint={mint[:8]}…")

            # 4. simulate
            print("\n[4/6] simulateTransaction (create+devbuy, no state change)")
            from solders.transaction import VersionedTransaction
            txn = VersionedTransaction.from_bytes(signed)
            resp = await chain.client.simulate_transaction(
                txn, sig_verify=False, replace_recent_blockhash=True)
            value = resp.value
            if value.err:
                print(f"      SIMULAZIONE FALLITA: {value.err}")
                print(f"      logs: {value.logs[-5:] if value.logs else []}")
                ok = False
            else:
                print("      SIMULAZIONE OK (la tx e' valida contro lo stato mainnet)")
                for log in (value.logs or [])[-6:]:
                    print(f"        | {log}")
        except Exception as e:
            print(f"      ERRORE: {e}")
            ok = False

    # 5. distribution tx sizes
    print(f"\n[5/6] Distribution tx sizes ({args.wallets} destinatari fittizi)")
    await chain.refresh_blockhash()
    recipients = [(str(Keypair().pubkey()), 1_000_000) for _ in range(args.wallets)]
    try:
        chunks = plan_chunks(recipients)
        print(f"      chunk: {len(chunks)} tx (limite bundle: 5)")
        for i, chunk in enumerate(chunks):
            signed_size = len(chain.build_distribution_tx(mint, chunk))
            fits = signed_size <= TX_SIZE_LIMIT
            print(f"      tx{i}: {len(chunk)} destinatari, {signed_size} byte "
                  f"{'OK' if fits else 'TROPPO GRANDE'}")
            ok = ok and fits
    except ValueError as e:
        print(f"      RIFIUTO ATTESO: {e}")
        ok = False

    # 6. cost preflight
    print("\n[6/6] Launch cost preflight")
    costs = await estimate_launch_costs(chain, cfg, args.wallets, args.dev_buy)
    for k in ("dev_buy", "ata_rent", "network_fees", "priority_fee", "jito_tip", "total"):
        print(f"      {k:14s} {costs[k] / 1e9:.6f} SOL")
    treasury = await chain.balance_sol()
    print(f"      {'treasury':14s} {treasury:.6f} SOL")
    expected = tokens_out_for_sol(args.dev_buy)
    print(f"      token attesi per {args.dev_buy} SOL: {expected / 1e6:,.0f}")
    if treasury * 1e9 < costs["total"]:
        print("      ATTENZIONE: treasury insufficiente per questo dev buy")
        ok = False

    print(f"\n=== DRY-RUN {'PASSATO' if ok else 'FALLITO'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
