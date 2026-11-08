# Jabba — Presale Bot + Atomic Launch su pump.fun

Bot Telegram che gestisce un **presale flat** (contributi in SOL, quota
proporzionale) e lancia il token su pump.fun con un **bundle atomico Jito**:
creazione + dev buy + distribuzione ai wallet nello stesso slot.

## Come funziona

```
FASE 1 — PRESALE (custodiale)
   /setwallet → deposito SOL al treasury → ledger dei contributi
   Rimborsi liberi (fee 0,30%) fino al lancio

FASE 2 — LAUNCH ATOMICO (zero-custodia)
   /launch → 10% SOL al team (TEAM_KEEP_PERCENT)
           → 90% → bundle Jito: create+devbuy (1 tx) + distribuzione
             → 100% dei token agli utenti, ∝ SOL versati, stesso slot

FASE 3 — MERCATO (automatico)
   Curva pump.fun → graduazione ~$69k → pool PumpSwap → Jupiter
```

- **Presale flat**: nessuna curva nostra, nessun token off-chain — il
  prezzo lo decide la curva pump.fun al dev buy (bottom, deterministico)
- **Rimborso libero**: `/refund` 25/50/75/100% con fee `REFUND_FEE_BPS`
  (default 0,30%), chiuso dopo il lancio
- **Quota**: `/position` mostra SOL contribuiti, % del totale e token
  stimati al lancio (matematica pump.fun deterministica)
- **Lancio atomico**: create + dev buy in **una** transazione (PumpPortal
  `trade-local`), poi la distribuzione in chunk — tutto in un bundle
  Jito: o tutto riesce, o tutto torna indietro
- **Zero token al team**: `TEAM_SHARE_BPS=0`; il team trattiene solo il
  10% dei SOL (`TEAM_KEEP_PERCENT`)

## Sicurezza e garanzie

- **Custodia — distinzione fondamentale**: il presale è **custodiale**
  (l'operatore tiene i SOL durante la raccolta); solo il lancio è
  zero-custodia (l'atomicità elimina gli stati intermedi di
  distribuzione, non la custodia della raccolta). Comunicalo agli utenti.
- **Verifica on-chain a runtime**: prima di lanciare, il bot legge
  l'account global del programma pump.fun e confronta i parametri della
  curva (VTR/VSR/RTR/supply/fee); se non matchano, il lancio abortisce
- **Preflight economico dinamico**: `dev_buy + rent_ATA×N + fee_rete +
  priority_fee + tip` calcolato prima di firmare; rent per 36 utenti
  ≈ 0.073 SOL; se il treasury non copre tutto, rifiuto esplicito
- **Esiti bundle mai ambigui**: `getInflightBundleStatuses` fino a
  Landed/Failed, poi `getBundleStatuses` per la conferma; timeout =
  esito sconosciuto → riconciliazione manuale su Solscan, **mai retry
  automatico** (rischio doppio lancio)
- **Limite atomico rigido**: massimo **36 partecipanti** (chunk da 9
  misurati sui 1232 byte/tx, 5 tx per bundle); oltre, il lancio è
  rifiutato — più bundle NON sono atomici tra loro
- Whitelist rigida `ALLOWED_TELEGRAM_IDS` (admin = primo ID), chiave
  privata solo da file, idempotenza depositi via dedup firme

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # compila i valori
solana-keygen new -o treasury.json  # indirizzo treasury
python -m curvebot.main
```

Per il lancio servono anche: `PINATA_JWT` (metadati IPFS),
`TOKEN_NAME`/`TOKEN_SYMBOL`/`TOKEN_IMAGE_PATH`, e un RPC mainnet
(Helius/QuickNode).

## Test e validazione (toolchain)

**1. Test locali (52)** — su ogni push/PR via GitHub Actions (`.github/workflows/tests.yml`):

```bash
pytest tests/ -q
```

**2. Dry-run mainnet (ZERO SOL)** — valida l'intero flusso contro lo
stato mainnet SENZA inviare nulla: verifica parametri on-chain, upload
Pinata, build+firma della create+devbuy via PumpPortal,
`simulateTransaction`, dimensioni tx di distribuzione, preflight costi:

```bash
python scripts/dryrun.py --wallets 36 --dev-buy 0.01
```

Il mint keypair è usa-e-getta: la tx viene costruita e simulata ma mai
inviata. Nota: la simulazione richiede un RPC mainnet (la tx usa address
lookup table mainnet; su devnet fallisce per questo, non per errori).

**3. Collaudo finale in piccolo** — primo lancio reale con 0.1 SOL di
dev buy (mainnet-only, nessun devnet per PumpPortal/Jito).

52 test: presale (contributi, rimborsi, shares), invariante distribuzione
in base-unit interi (Σ quote + team + dust == distribuito, per
1/15/30/36 utenti), chunking (rifiuto per 37/45/46/100), parametri curva
pump.fun verificati on-chain, macchina a stati Jito (inflight + conferme,
casi malformati), preflight costi (rent ATA), costruzione/firma tx
offline, UI.

**Non coperto** (mainnet-only): atterraggio reale del bundle e
comportamento PumpPortal in produzione → primo lancio in piccolo
(0.1 SOL di dev buy).

## Struttura

```
curvebot/
  service.py    ledger presale: depositi, rimborsi con fee, shares
  db.py         SQLite: users, ledger(deposit/refund), launches, meta,
                processed_sigs (idempotenza)
  chain.py      Solana: treasury, depositi, firma tx PumpPortal,
                build_distribution_tx (ATA idempotenti + transfer_checked
                + tip Jito), send_bundle, tip accounts
  monitor.py    polling RPC depositi → accredito contributi
  pump_curve.py matematica curva pump.fun (parametri VERIFICATI on-chain)
  launch.py     PumpPortal client, distribuzione (invariante), chunking,
                preflight costi, macchina a stati bundle
  keyboards.py  layout bottoni inline
  handlers.py   comandi + callback (rimborso guidato, /launch admin)
  chart.py      PNG curva pump.fun attesa (dev buy marcato)
  config.py     configurazione da env
  main.py       entry point
tests/          52 test
SPEC.md         specifica tecnica completa
```

## Interfaccia

`/start` apre il pannello con bottoni inline (Come partecipare ·
Rimborso · Posizione · Curva · Info). Il rimborso è guidato: scelta
percentuale → quotazione con fee → conferma ✅/❌. `/launch` (admin)
mostra il riepilogo (raccolto, split team/dev buy, partecipanti) con
doppia conferma.

## Comandi

| Comando | Effetto |
|---|---|
| `/start` `/menu` | pannello con bottoni inline |
| `/setwallet <pubkey>` | registra il wallet per depositi e rimborsi |
| `/info` | raccolto, partecipanti, token stimati, fee, treasury |
| `/position` | SOL contribuiti, % del presale, token stimati |
| `/refund` | rimborso guidato con conferma (solo pre-lancio) |
| `/launch` | (solo admin) lancio atomico su pump.fun |

## Attenzione

- Il presale è **custodiale**: gestisci la chiave del treasury come una
  chiave di produzione
- pump.fun e Jito sono **solo mainnet**: nessun devnet per collaudare
- Gli screener (trench.bot/GMGN) marchiano il supply concentrato nel
  wallet del dev: la distribuzione atomica immediata è la mitigazione
- Offrire token contro SOL ha implicazioni legali/regolamentari:
  valuta l'aspetto prima di andare live
