# Jabba — Presale Bot + Atomic Launch su pump.fun

Bot Telegram che gestisce un **presale flat** (contributi in SOL, quota
proporzionale) e lancia il token su pump.fun con un **bundle atomico Jito**:
creazione + dev buy + distribuzione ai wallet nello stesso slot.

## Come funziona

- **Presale flat**: l'utente registra il wallet (`/setwallet`) e invia SOL al
  treasury. Il ledger registra solo i SOL versati: nessuna curva nostra,
  nessun token off-chain — il prezzo lo decide la curva pump.fun al lancio.
- **Rimborso libero**: `/refund` (25/50/75/100%) prima del lancio, con fee
  `REFUND_FEE_BPS` (default 0,30%).
- **Quota**: `/position` mostra SOL contribuiti, % del totale e token stimati
  al lancio (matematica pump.fun deterministica).
- **Lancio atomico**: `/launch` (admin) → il team trattiene
  `TEAM_KEEP_PERCENT%` dei SOL (default 10%); il resto → bundle Jito con
  create + dev buy + distribuzione **100% agli utenti** (proporzionale ai
  SOL versati), stesso slot.
- Idempotenza depositi (dedup firme), whitelist rigida, admin = primo ID.

## Attenzione

- Il saldo token esiste **solo nel DB del bot**: non è visibile in Phantom,
  non è trasferibile fuori dal bot, e un errore nel DB può perderlo.
- Il treasury è **custodiale**: i SOL degli utenti stanno nel wallet del bot.
  Gestisci la chiave privata come una chiave di produzione, e valuta gli
  aspetti legali/regolamentari prima di andare live.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # compila i valori
solana-keygen new -o treasury.json  # indirizzo treasury
python -m curvebot.main
```

Su devnet: `solana airdrop 2` per finanziare il treasury di test.

## Test

```bash
pytest tests/ -q
```

22 test: matematica della curva, fee buy/sell, ledger SQLite e servizio
buy/sell off-chain (incluso roundtrip: chi compra e rivende subito recupera
il deposito al netto delle due fee).

## Struttura

```
curvebot/
  curve.py     matematica della curva lineare
  service.py   operazioni ledger buy/sell (testabili offline)
  db.py        SQLite: utenti/wallet, ledger, supply, dedup firme
  chain.py     Solana minima: balance, invio SOL, parsing depositi
  monitor.py   watcher depositi in background
  handlers.py  comandi Telegram (aiogram 3)
  config.py    configurazione da env
  main.py      entry point
tests/         test curve + db + service
```

## Interfaccia

`/start` apre il menu con bottoni inline:

- 💰 Come comprare · 📤 Vendi
- 📊 Posizione · 📈 Curva
- ℹ️ Info

La vendita è guidata: scelta percentuale (25/50/75/100%) →
quotazione con fee → conferma con due bottoni (✅/❌).
`/chart` (o il bottone 📈) invia il grafico PNG della curva con il
supply attuale segnato.

## Lancio su pump.fun (presale → curva → pool)

Quando il presale è pronto, l'admin lancia con `/launch`:

1. **Creazione** del token su pump.fun via PumpPortal local API
   (firma client-side: le chiavi non lasciano mai il bot)
2. **Dev buy** col SOL del treasury (`DEV_BUY_PERCENT`, default 95%)
   — acquisto al bottom della curva
3. **Distribuzione atomica** ai wallet registrati, in proporzione alle
   posizioni del presale (`TEAM_SHARE_BPS` al team, default 5%)
   — create + dev buy + distribuzione viaggiano in un **bundle atomico
   Jito** (stesso slot): o tutto riesce, o tutto torna indietro. Il
   treasury non detiene mai i token in nessuno stato confermato on-chain.
   La distribuzione è pre-computabile perché la curva è vergine nel
   bundle stesso (matematica in `pump_curve.py`, verificata da test).

   Limite bundle: 5 tx → max 45 partecipanti per lancio atomico
   (15 transfer per tx). Tip Jito: `JITO_TIP_LAMPORTS` (default 0.001 SOL),
   pagato solo se il bundle atterra.
4. Da lì la curva vive su pump.fun: alla graduazione (~85 SOL) pump.fun
   crea la pool su PumpSwap e Jupiter la indicizza, automaticamente

⚠️ pump.fun e Jito sono solo mainnet: il primo lancio si collauda con
cifre piccole (es. 0.1 SOL di dev buy).
Distribuire subito è importante: screener come trench.bot/GMGN marchiano
i token con supply concentrato nel wallet del dev.

## Comandi

| Comando | Effetto |
|---|---|
| `/start` `/menu` | pannello con bottoni inline |
| `/setwallet <pubkey>` | registra il wallet per depositi e rimborsi |
| `/info` | indirizzo treasury, prezzo, supply, SOL accumulati, fee buy/sell |
| `/price` | prezzo attuale |
| `/position` | token posseduti e valore netto se vendi ora |
| `/chart` `/curva` | grafico PNG della curva |
| `/sell` | apre il menu vendita a bottoni |
| `/launch` | (solo admin) lancia il presale su pump.fun |
