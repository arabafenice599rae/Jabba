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
- **Lancio atomico**: create_v2 + buy_v3 costruite **direttamente sul
  programma pump.fun** (solders, IDL ufficiale — niente intermediari,
  niente fee di terzi, nessun programma esterno nel percorso dei fondi),
  poi la distribuzione in chunk — tutto in un bundle Jito: o tutto
  riesce, o tutto torna indietro
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
- **Preflight economico dinamico**: `dev_buy + fee (1,75%) + rent create +
  rent_ATA×N + fee_rete + priority_fee + tip` calcolato prima di firmare;
  rent ATA Token-2022 per 36 utenti ≈ 0.075 SOL; se il treasury non copre
  tutto, rifiuto esplicito
- **Tetto del dev buy**: oltre ~86 SOL nella curva (raccolta ~95,6 SOL) la
  distribuzione supererebbe le riserve reali: lancio rifiutato
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

## Test e validazione

```bash
pip install -r requirements-dev.txt
pytest -q                                   # unit, property-based, LiteSVM
ruff check . && mypy                        # lint + tipi
JABBA_FORK=1 pytest tests/test_fork_mainnet.py -s   # lancio su fork mainnet
```

| Livello | Cosa verifica |
|---|---|
| property-based (hypothesis) | curva, distribuzione in base-unit interi, chunking, ledger a stati |
| handler | flussi Telegram, race (doppio rimborso), validazione wallet |
| **LiteSVM** (`tests/test_svm.py`) | il codice vero di `Chain` eseguito su una SVM reale: rimborsi, depositi rilevati end-to-end, distribuzione Token-2022 / SPL a 36 utenti con tip |
| **fork mainnet** (opt-in) | `run_launch` vero: tx create_v2+buy_v3 reali, programmi pump.fun clonati da mainnet in LiteSVM, bundle eseguito in ordine. Nulla viene inviato a mainnet |

Il fork gira ogni notte in CI e a richiesta (`workflow_dispatch`): rilancialo
sempre prima di un lancio vero, intercetta cambi di pump.fun
(fee, formato tx, token program). RPC configurabile con il secret
`JABBA_FORK_RPC`.

I bug noti e non ancora corretti sono test `xfail(strict=True)` con motivo
`BUG: ...`: quando vengono sistemati il test diventa rosso e il marcatore
va tolto.

Prima del lancio vero, con la chiave e l'RPC reali e senza spendere SOL:
`python scripts/dryrun.py --wallets 36 --dev-buy <SOL>` (parametri curva,
create_v2+buy_v3 costruite e simulate, dimensioni tx, preventivo costi).

**Stati che proteggono i fondi**
- rimborso: preventivo (nessuna scrittura) → conferma = prenotazione
  atomica e durevole → invio → registrato, oppure `unknown` se l'esito è
  incerto (resta trattenuto fino a `/reconcile`). Al riavvio ogni
  prenotazione interrotta diventa `unknown`, mai rilasciata alla cieca.
- lancio: lock persistente; durante il lancio rimborsi sospesi e
  depositi non accreditati; esito sconosciuto → `launch_unknown`, chiuso
  solo da `/launchresolve`.
- depositi: solo finalized, uno per trasferimento, accredito e firma
  vista nella stessa transazione SQLite.

Fatti misurati sul fork (2026-11): il mint pump.fun è **Token-2022**;
`amount` è SOL che entra nella curva e i token sono esattamente
`floor(VTR0·S/(VSR0+S))`; fee **in aggiunta**: 0,95% protocollo + 0,30%
creator; ~0,0122 SOL di rent una tantum per la create.

## Struttura

```
curvebot/
  service.py    ledger presale: depositi, rimborsi con fee, shares
  db.py         SQLite: users, ledger(deposit/refund), launches, meta,
                processed_sigs (idempotenza)
  chain.py      Solana: treasury, depositi, invio tx generiche,
                build_distribution_tx (ATA idempotenti + transfer_checked
                + tip Jito), send_bundle, tip accounts
  monitor.py    polling RPC depositi → accredito contributi
  pump_curve.py matematica curva pump.fun (parametri VERIFICATI on-chain)
  launch.py     dev buy dinamico, distribuzione (invariante), chunking,
                preflight costi, macchina a stati bundle
  keyboards.py  layout bottoni inline
  handlers.py   comandi + callback (rimborso guidato, /launch admin)
  chart.py      PNG curva pump.fun attesa (dev buy marcato)
  config.py     configurazione da env
  main.py       entry point
tests/          suite completa (vedi Test e validazione)
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
| `/sweep` | (solo admin) riscuote le creator fee dai vault (curva + PumpSwap) |
| `/reconcile` | (solo admin) riconciliazione ledger ↔ blockchain |
| `/reconcile` | (solo admin) confronto ledger ↔ chain in 6 categorie + risoluzione dei rimborsi `unknown`; non corregge nulla da solo |
| `/launchresolve` | (solo admin) chiude un lancio con esito sconosciuto leggendo la chain: mint presente = atterrato, assente dopo 5 min = fallito |

## Attenzione

- Il presale è **custodiale**: gestisci la chiave del treasury come una
  chiave di produzione
- pump.fun e Jito sono **solo mainnet**: nessun devnet per collaudare
- Gli screener (trench.bot/GMGN) marchiano il supply concentrato nel
  wallet del dev: la distribuzione atomica immediata è la mitigazione
- Offrire token contro SOL ha implicazioni legali/regolamentari:
  valuta l'aspetto prima di andare live
