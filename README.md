# solana-curve-bot

Bot Telegram minimale che gestisce una bonding curve lineare, **tutto off-chain**:
esiste un solo indirizzo Solana (il treasury) dove arrivano i SOL; il "token"
è un credito nel database del bot, non un asset on-chain.

## Come funziona

- Un solo keypair: l'**indirizzo treasury** (visibile on-chain) dove si accumulano i SOL.
- Curva lineare: `prezzo(supply) = P0 + M * supply`.
- **Buy**: l'utente registra il wallet con `/setwallet`, poi invia SOL al
  treasury. Il monitor rileva il deposito, trattiene la **fee buy (0,30%)**,
  calcola i token sul netto secondo la curva e accredita il saldo nel ledger.
- **Sell**: `/sell <importo|%>` — il bot verifica il saldo, sottrae la
  **fee sell (0,30%)** dai proventi, invia i SOL al wallet dell'utente e
  azzera i token nel ledger. Le fee restano nel treasury come ricavo.
- Nessun token SPL: nessun mint, burn, ATA. Solo SOL in ingresso, SOL in uscita.
- Idempotenza: ogni deposito è processato una sola volta (dedup firme tx).
- Whitelist rigida `ALLOWED_TELEGRAM_IDS`: chiunque altro viene ignorato.

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

## Mini App (simulatore interattivo)

`miniapp/index.html` è una Telegram Mini App self-contained (nessun backend,
nessun build): grafico della curva esplorabile al tocco + simulatore
buy/sell con fee in tempo reale.

### Hosting (GitHub Pages)

1. Fork/clona il repo e attiva Pages: **Settings → Pages → Deploy from a
   branch → main → / (root)** oppure metti `miniapp/` in una cartella
   `docs/` e seleziona quella.
2. L'URL sarà tipo `https://<utente>.github.io/jabba/miniapp/`
   (o `/jabba/` se pubblichi la root).
3. Inseriscilo in `WEBAPP_URL` nel `.env` del bot: apparirà il bottone
   **🧮 Simulatore** nel menu, che apre la Mini App dentro Telegram.
4. Opzionale: da BotFather, `/setmenubutton` per avere la Mini App anche
   nel menu del bot.

La app riceve i parametri della curva (P0, slope, supply, fee) via query
string quando il bot la apre: i dati sono sempre quelli attuali.

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
