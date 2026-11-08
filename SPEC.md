# SPEC — Jabba: Presale Bot + Atomic Launch su pump.fun

## 1. Visione d'insieme

Sistema in due fasi per emettere un token community su Solana:

```
FASE 1 — PRESALE FLAT (bot Telegram, off-chain)
  Depositi SOL → ledger dei contributi (quota proporzionale ai SOL).
  Nessuna curva propria: il prezzo lo decide pump.fun al lancio.

FASE 2 — LAUNCH ATOMICO (bundle Jito, on-chain, zero-custodia)
  /launch → TEAM_KEEP_PERCENT% (default 10) dei SOL resta al treasury;
  il resto → crea token su pump.fun + dev buy + distribuzione
  nello STESSO slot → token 100% agli utenti (∝ SOL versati)

FASE 3 — MERCATO (pump.fun, automatico)
  Curva pump.fun → graduazione a ~$69k → pool PumpSwap → Jupiter
```

## 2. Architettura

### 2.1 Stack

| Componente | Tecnologia |
|---|---|
| Bot Telegram | Python 3.12, aiogram 3 |
| Blockchain | Solana (solana-py 0.41, solders) |
| Ledger | SQLite (file singolo) |
| Grafico | matplotlib (PNG renderizzato on-demand) |
| Launch API | PumpPortal local API (firma client-side) |
| Atomicità | Jito Block Engine (bundle atomici) |
| Test | pytest (42 test) |

### 2.2 Moduli

```
curvebot/
├── service.py   Ledger presale: depositi SOL, rimborsi con fee,
│                 quote e shares proporzionali
├── db.py         SQLite: users(wallet), ledger(buy/sell), launches,
│                 meta(state,mint), processed_sigs(idempotenza)
├── chain.py      Solana: keypair treasury, balance, send SOL,
│                 parsing depositi in ingresso, firma tx PumpPortal,
│                 build_distribution_tx (ATA idempotenti + transfer_checked
│                 + tip Jito), send_bundle, fetch_tip_accounts
├── monitor.py    Polling RPC depositi (ogni POLL_INTERVAL_S) → accredito
├── pump_curve.py Matematica curva pump.fun (VTR 1.073B/6dp, VSR 30 SOL,
│                 fee 1.25%) → output dev buy deterministico
├── launch.py     PumpPortal client + compute_distribution (proporzionale
│                 esatto, dust→ultimo utente) + run_launch (bundle atomico)
├── keyboards.py  Layout bottoni inline
├── handlers.py   Comandi + callback (vendita guidata con conferma,
│                 /launch admin-only con doppia conferma)
├── chart.py      PNG curva (tema scuro, marker supply attuale)
└── config.py     Tutto da env
```

## 3. FASE 1 — Presale (dettaglio)

### 3.1 Meccanica

- Un solo indirizzo Solana: il **treasury** (keypair file, mai nel codice)
- Il presale è **flat**: nessuna curva propria, nessun token off-chain.
  Il ledger registra solo chi ha versato quanti SOL (netto dei rimborsi)
- La quota di ciascuno = suoi SOL / SOL totali; il prezzo per token lo
  determina la curva pump.fun al dev buy (bottom, deterministico)
- **Rimborso libero**: `/refund` 25/50/75/100% → fee REFUND_FEE_BPS
  (default 0,30%) → SOL restituiti al wallet registrato; chiusi dopo il lancio
- Fairness: chi entra prima e chi entra dopo pagano lo stesso prezzo

### 3.2 Flusso utente

1. `/setwallet <pubkey>` → wallet registrato (per depositi e rimborsi)
2. Invio SOL al treasury → monitor (polling, dedup firme) registra il
   contributo entro ~POLL_INTERVAL_S; sotto MIN_DEPOSIT_SOL o da wallet
   sconosciuti: ignorati + loggati
3. `/position` → SOL contribuiti, % del presale, token stimati al lancio
4. `/refund` → bottoni % → quotazione con fee → conferma ✅/❌ → SOL indietro

### 3.3 Sicurezza

- Whitelist rigida `ALLOWED_TELEGRAM_IDS` (il bot non parte senza)
- Il primo ID della whitelist è l'**admin** (unico che può `/launch`)
- Chiave privata solo da file; `.env` e `treasury.json` in `.gitignore`
- Idempotenza depositi via firma transazione deduplicata
- Treasury **custodiale** durante il presale (fase 1) — dichiarato nel README

## 4. FASE 2 — Launch atomico (dettaglio)

### 4.1 Precondizioni

- `TOKEN_NAME`, `TOKEN_SYMBOL` configurati; immagine presente
- Posizioni presale > 0; ≤ 45 partecipanti (limite bundle)
- Treasury SOL sufficiente per dev buy + fee + tip

### 4.2 Sequenza (run_launch)

```
1. mint_kp = Keypair() casuale
2. PumpPortal /api/create (multipart: mint, name, symbol, desc, socials,
   image) → tx non firmata → firma client-side [treasury, mint_kp] → tx1
3. dev_buy_sol = treasury × (100 − TEAM_KEEP_PERCENT)  # default 90%
4. PumpPortal /api/trade-local {action:buy, denominatedInSol:true,
   pool:pump} → tx2 firmata
5. expected = pump_curve.tokens_out_for_sol(dev_buy_sol)   ← DETERMINISTICO
   (curva vergine: il token esiste solo dentro il bundle)
6. dist, team = compute_distribution(contributi SOL, expected, TEAM_SHARE_BPS)
   → proporzionale ai SOL versati, dust all'ultimo utente, team in treasury
7. chunks da 15 → build_distribution_tx per chunk:
   create_idempotent_ATA + transfer_checked (6 decimali) per utente
   + tip JITO_TIP_LAMPORTS nell'ULTIMA tx
8. bundle = [tx1, tx2, ...dist] (max 5) → POST sendBundle a
   https://mainnet.block-engine.jito.wtf/api/v1/bundles
9. Se qualsiasi tx fallisce → TUTTO il bundle scarta → stato invariato
```

### 4.3 Zero-custodia (per costruzione)

- Gli stati intermedi (dev buy eseguito, distribuzione pendente) **non
  esistono mai in un blocco confermato**: tutto nello stesso slot
- Se il bundle non atterra: nessun costo oltre fee di rete delle firme
  (tip pagato solo a bundle incluso)
- Tip accounts recuperati a runtime (`getTipAccounts`) con fallback
  alla lista ufficiale verificata

### 4.4 Vincoli noti

| Vincolo | Valore | Gestione |
|---|---|---|
| Tx per bundle | 5 | create + buy + 3 chunk → max 45 utenti |
| Transfer per tx | ~15 | chunk da 15 |
| Devnet | inesistente | primo lancio in piccolo (0.1 SOL) |
| Fee curva pump | 1.25%/lato | inclusa nella matematica |
| Creator fee | +0.30%/trade al treasury | ricavo passivo |

## 5. FASE 3 — Mercato (automatico, zero codice)

- Curva pump.fun: chiunque può comprare/vendere
- Graduazione a ~$69k (~85 SOL): pump.fun chiude la curva e crea la pool
  **PumpSwap** (LP bruciate, fee 0.015 SOL) — automatica e irreversibile
- Jupiter indicizza la pool → token scambiabile ovunque
- Rischio reputazionale: supply concentrato nel dev è marcato dagli
  screener → mitigato dalla distribuzione atomica immediata

## 6. Modello dati (SQLite)

```sql
users(telegram_id PK, wallet, created_at)
ledger(id, telegram_id, kind['deposit'|'refund'], amount_sol,
       fee_sol, signature, created_at)
launches(id, mint, sig_create, sig_buy, dev_buy_sol, tokens_bought,
         tokens_distributed, team_tokens, created_at)
meta(key PK, value)          -- state: launched, mint: <pubkey>
processed_sigs(signature PK) -- idempotenza depositi
```

- Contributo utente = Σ(deposit) − Σ(refund)
- Totale presale = Σ contributi di tutti

## 7. Configurazione (.env)

| Chiave | Default | Note |
|---|---|---|
| TELEGRAM_BOT_TOKEN | — | da BotFather |
| RPC_URL | devnet public | mainnet: Helius/QuickNode |
| KEYPAIR_PATH | treasury.json | keypair treasury |
| REFUND_FEE_BPS | 30 | fee rimborso 0.30% |
| ALLOWED_TELEGRAM_IDS | — | obbligatorio; [0]=admin |
| MIN_DEPOSIT_SOL | 0.01 | |
| POLL_INTERVAL_S | 5 | latenza accredito |
| TOKEN_NAME/SYMBOL/DESCRIPTION/IMAGE_PATH | — | launch |
| TEAM_KEEP_PERCENT | 10 | % SOL trattenuto dal team |
| TEAM_SHARE_BPS | 0 | quota token al team (0 = tutti agli utenti) |
| LAUNCH_SLIPPAGE / PRIORITY_FEE | 10 / 0.0005 | |
| JITO_TIP_LAMPORTS | 1000000 | 0.001 SOL |

## 8. Comandi

| Comando | Chi | Effetto |
|---|---|---|
| /start /menu | whitelist | pannello bottoni |
| /setwallet | whitelist | registra wallet |
| /info /position | whitelist | stato presale e quota |
| /refund | whitelist | rimborso guidato con conferma (pre-lancio) |
| /launch | admin | lancio atomico (doppia conferma) |

## 9. Test (27)

- **presale/service (5)**: contributi, rimborso parziale/totale con fee,
  rifiuti, shares
- **db (4)**: wallet, contributi, totale globale, dedup firme
- **launch.py (5)**: distribuzione proporzionale esatta (pesi SOL), team
  share, somma senza perdite, casi limite
- **pump_curve.py (6)**: monotonia, fee 1.25%, ~793M token a 85 SOL
  (soglia graduazione), roundtrip base/ui
- **batch transfer (3)**: costruzione+firma tx offline (RPC fake)
- **UI (4)**: PNG curva pump valido, layout tastiere

**Non coperto dai test** (mainnet-only, da collaudo in piccolo):
sequenza reale PumpPortal create→buy→bundle Jito; parsing depositi in
produzione; graduazione PumpSwap.

## 10. Rischi e mitigazioni

| Rischio | Mitigazione |
|---|---|
| Custodia presale (fase 1) | whitelist, chiave gestita da operatore |
| Bundle non atterra | atomico: riprova senza perdite |
| Dev concentration perception | distribuzione atomica immediata |
| PumpPortal down | fallback: costruire tx a mano (roadmap) |
| Parametri curva pump cambiano | `pump_curve.py` centralizzato, verificare pre-lancio |
| Legale (offerta asset) | responsabilità dell'operatore |

## 11. Roadmap

1. Collaudo mainnet in piccolo (0.1 SOL)
2. WebSocket per depositi (sub-secondo, via logsSubscribe)
3. Fallback launch senza PumpPortal (istruzioni pump.fun a mano)
4. Bundle multipli per >45 utenti (bundle concatenati stesso slot)
