#!/usr/bin/env python3
"""Dry-run validation of the Jabba launch flow — ZERO SOL.

Validates against LIVE mainnet with the REAL treasury key and RPC, WITHOUT
sending anything:
  1. on-chain pump.fun curve params vs pump_curve.py
  2. Pinata metadata upload (only if PINATA_JWT is set and --pinata is given)
  3. dynamic dev buy plan for the current raise (or --raise SOL)
  4. our create_v2 + buy_v3 tx: build, sign, simulateTransaction
  5. distribution tx sizes vs the 1232-byte limit

Usage:
  python scripts/dryrun.py [--wallets N] [--raise SOL] [--pinata]

The mint keypair is throwaway: the create tx is simulated, NEVER sent, so
nothing is created on-chain. The simulation only passes if the treasury
already holds the SOL the plan needs (with an empty treasury it answers
AccountNotFound / insufficient funds: expected). For the full launch with
every account executed, run the mainnet fork instead:
  JABBA_FORK=1 pytest tests/test_fork_mainnet.py -s
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
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


async def main() -> int:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction

    from curvebot.chain import JITO_TIP_ACCOUNTS, Chain
    from curvebot.config import Config
    from curvebot.launch import (
        METADATA_URI_BUDGET,
        TX_SIZE_LIMIT,
        fetch_global_account,
        plan_chunks,
        plan_dev_buy,
        upload_pinata_metadata,
    )
    from curvebot.pump_curve import (
        max_sol_cost_lamports,
        params_match,
        tokens_out_for_sol,
        verify_params,
    )

    load_env()
    cfg = Config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallets", type=int, default=36,
                    help="number of recipients for the plan and size checks")
    ap.add_argument("--raise", dest="raised", type=float, default=None,
                    help="raise to plan for (default: the treasury balance)")
    ap.add_argument("--pinata", action="store_true",
                    help="really upload the metadata to Pinata")
    args = ap.parse_args()

    print("=== JABBA DRY-RUN (nothing will be sent) ===\n")
    ok = True
    chain = Chain(cfg.rpc_url, cfg.keypair_path)
    print(f"[1/5] RPC {cfg.rpc_url} · treasury {chain.pubkey}")
    try:
        onchain = verify_params(await fetch_global_account(chain))
    except Exception as e:  # noqa: BLE001 - dry-run reports every failure
        print(f"      ERRORE: {e}")
        return 1
    match = params_match(onchain)
    print(f"      pump.fun global: fee={onchain['fee_basis_points']} bps, "
          f"VTR={onchain['initial_virtual_token_reserves'] / 1e6:,.0f}, "
          f"VSR={onchain['initial_virtual_sol_reserves'] / 1e9} SOL "
          f"-> {'OK' if match else 'CAMBIATI: NON LANCIARE'}")
    ok = ok and match

    print("\n[2/5] metadati")
    uri = METADATA_URI_BUDGET
    if args.pinata and cfg.pinata_jwt:
        import aiohttp
        async with aiohttp.ClientSession() as http:
            try:
                uri = await upload_pinata_metadata(http, cfg)
                print(f"      Pinata OK: {uri}")
            except Exception as e:  # noqa: BLE001 - dry-run reports every failure
                print(f"      Pinata ERRORE: {e}")
                ok = False
    else:
        print(f"      uri di prova ({len(uri)} caratteri, come un CID Pinata)")

    treasury_sol = await chain.balance_sol()
    raised = args.raised if args.raised is not None else treasury_sol
    print(f"\n[3/5] piano dev buy dinamico · raccolta {raised:.6f} SOL, "
          f"{args.wallets} wallet, treasury {treasury_sol:.6f} SOL")
    try:
        dev, costs = await plan_dev_buy(chain, cfg, args.wallets, raised,
                                        round(max(raised, treasury_sol) * 1e9))
        for k, v in costs.items():
            print(f"      {k:16s} {v / 1e9:.6f} SOL")
        print(f"      dev buy {dev:.6f} SOL -> {tokens_out_for_sol(dev) / 1e6:,.2f} token")
    except RuntimeError as e:
        print(f"      RIFIUTATO: {e}")
        return 1

    print("\n[4/5] create_v2 + buy_v3 (costruita da noi): firma e simulazione")
    mint_kp = Keypair()
    await chain.refresh_blockhash()
    raw = chain.build_create_tx(mint_kp, cfg.token_name or "DryRun",
                                cfg.token_symbol or "TEST", uri,
                                tokens_out_for_sol(dev), max_sol_cost_lamports(dev))
    fits = len(raw) <= TX_SIZE_LIMIT
    print(f"      {len(raw)} byte {'OK' if fits else 'TROPPO GRANDE'} · mint {str(mint_kp.pubkey())[:8]}…")
    ok = ok and fits
    resp = await chain.client.simulate_transaction(
        VersionedTransaction.from_bytes(raw), sig_verify=True, replace_recent_blockhash=False)
    if resp.value.err:
        print(f"      simulazione: {resp.value.err}")
        print("      (atteso se il treasury non ha ancora i SOL del piano)")
        for line in (resp.value.logs or [])[-5:]:
            print(f"        | {line}")
        ok = ok and treasury_sol < costs["total"] / 1e9
    else:
        print(f"      simulazione OK · {resp.value.units_consumed} CU")

    print(f"\n[5/5] dimensioni tx di distribuzione ({args.wallets} destinatari)")
    recipients = [(str(Keypair().pubkey()), 1_000_000) for _ in range(args.wallets)]
    try:
        chunks = plan_chunks(recipients)
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            size = len(chain.build_distribution_tx(
                str(mint_kp.pubkey()), chunk, cfg.jito_tip_lamports if last else 0,
                JITO_TIP_ACCOUNTS[0]))
            print(f"      tx{i + 1}: {len(chunk)} destinatari, {size} byte "
                  f"{'OK' if size <= TX_SIZE_LIMIT else 'TROPPO GRANDE'}")
            ok = ok and size <= TX_SIZE_LIMIT
        print(f"      bundle: {1 + len(chunks)} tx (limite Jito: 5)")
    except ValueError as e:
        print(f"      RIFIUTATO: {e}")
        ok = False

    print(f"\n=== DRY-RUN {'PASSATO' if ok else 'FALLITO'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
