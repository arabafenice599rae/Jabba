# SPEC — Jabba: Presale Bot + Atomic Launch su pump.fun

## 1. Visione d'insieme

Sistema in due fasi per emettere un token community su Solana:

```
FASE 1 — PRESALE (bot Telegram, off-chain)
  Depositi SOL → crediti token su curva lineare propria (ledger SQLite)

FASE 2 — LAUNCH ATOMICO (bundle Jito, on-chain, zero-custodia)
  /launch → crea token su pump.fun + dev buy + distribuzione
  nello STESSO slot → token nei wallet degli utenti

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
├── curve.py      Curva lineare presale: p(s)=P0+M·s
│                 Buy = ∫ p ds (soluzione quadratica), sell = ∫ con clamp
├── service.py   Buy/sell ledger con fee (buy fee pre-calcolo token,
│                 sell fee sui proventi); separato dalla matematica pura
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
- Il "token" presale è un **credito nel ledger**, non un asset on-chain
- Prezzo: `p(supply) = P0 + M·supply` (default P0=0.00001, M=5e-10)
- **Buy**: fee 0,30% trattenuta prima; token = soluzione di
  `P0·x + M·s·x + (M/2)x² = netto` (quadratica, integrazione esatta)
- **Sell**: provento = `P0·x + M(s·x − x²/2)`, fee 0,30% sul lordo
- Proprietà: vendere x token comprati a supply s+x restituisce lo stesso
  valore di acquisto (pre-fee) → **treasury sempre solvente per costruzione**

### 3.2 Flusso utente

1. `/setwallet <pubkey>` → wallet registrato (per depositi e rimborsi)
2. Invio SOL al treasury → monitor (polling, dedup firme) accredita token
   entro ~POLL_INTERVAL_S; depositi sotto MIN_DEPOSIT_SOL o da wallet
   sconosciuti: ignorati + loggati
3. `/sell <x|%>` → bottoni 25/50/75/100% → quotazione con fee →
   conferma ✅/❌ → invio SOL al wallet → debito ledger

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
3. dev_buy_sol = treasury × DEV_BUY_PERCENT (95% default)
4. PumpPortal /api/trade-local {action:buy, denominatedInSol:true,
   pool:pump} → tx2 firmata
5. expected = pump_curve.tokens_out_for_sol(dev_buy_sol)   ← DETERMINISTICO
   (curva vergine: il token esiste solo dentro il bundle)
6. dist, team = compute_distribution(posizioni, expected, TEAM_SHARE_BPS)
   → proporzionale esatto, dust all'ultimo utente, team in treasury
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
ledger(id, telegram_id, kind['buy'|'sell'], amount_sol, amount_tokens,
       fee_sol, signature, created_at)
launches(id, mint, sig_create, sig_buy, dev_buy_sol, tokens_bought,
         tokens_distributed, team_tokens, created_at)
meta(key PK, value)          -- state: launched, mint: <pubkey>
processed_sigs(signature PK) -- idempotenza depositi
```

- `supply = Σ(buy) − Σ(sell)` (corretto dopo bug-fix da test)
- Posizione utente = Σ(buy) − Σ(sell) per telegram_id

## 7. Configurazione (.env)

| Chiave | Default | Note |
|---|---|---|
| TELEGRAM_BOT_TOKEN | — | da BotFather |
| RPC_URL | devnet public | mainnet: Helius/QuickNode |
| KEYPAIR_PATH | treasury.json | keypair treasury |
| PRICE_BASE_SOL / SLOPE_SOL | 1e-5 / 5e-10 | curva presale |
| BUY_FEE_BPS / SELL_FEE_BPS | 30 / 30 | 0.30% |
| ALLOWED_TELEGRAM_IDS | — | obbligatorio; [0]=admin |
| MIN_DEPOSIT_SOL | 0.01 | |
| POLL_INTERVAL_S | 5 | latenza accredito |
| TOKEN_NAME/SYMBOL/DESCRIPTION/IMAGE_PATH | — | launch |
| DEV_BUY_PERCENT | 95 | % treasury nel dev buy |
| TEAM_SHARE_BPS | 500 | 5% team, resto utenti |
| LAUNCH_SLIPPAGE / PRIORITY_FEE | 10 / 0.0005 | |
| JITO_TIP_LAMPORTS | 1000000 | 0.001 SOL |

## 8. Comandi

| Comando | Chi | Effetto |
|---|---|---|
| /start /menu | whitelist | pannello bottoni |
| /setwallet | whitelist | registra wallet |
| /info /price /position /chart | whitelist | stato |
| /sell | whitelist | vendita guidata con conferma |
| /launch | admin | lancio atomico (doppia conferma) |

## 9. Test (42)

- **curve.py (10)**: inversione buy/costo, coerenza sell≈buy, fee, clamp,
  monotonia prezzo
- **service.py (8)**: fee buy trattenuta, roundtrip supply→0, rifiuti
- **db.py (4)**: wallet, posizioni, supply globale, dedup firme
- **launch.py (5)**: distribuzione proporzionale esatta, team share,
  somma senza perdite, casi limite
- **pump_curve.py (6)**: monotonia, fee 1.25%, ~793M token a 85 SOL
  (soglia graduazione), roundtrip base/ui
- **batch transfer (3)**: costruzione+firma tx offline (RPC fake),
  skip importi zero
- **UI (6)**: PNG valido, layout tastiere

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
