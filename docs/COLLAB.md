# Collaborazione tra agenti

Su questo repo lavorano più agenti (Claude, Vibe/Mistral) per il proprietario.
Il canale di coordinamento è l'**issue #7**: lì si scrive prima di iniziare
un lavoro e si riportano i risultati.

## Regole
- **Un branch per lavoro**, mai commit diretti su `main`. Il nome dice chi
  lo porta avanti (per esempio `claude/...`).
- **Prima di toccare file condivisi** (`launch.py`, `handlers.py`,
  `service.py`, `chain.py`), un commento nell'issue #7 con l'obiettivo, il
  branch e i file. Se un altro agente li ha in mano, ci si accorda lì: è
  quello che ha generato i conflitti tra #4 e #5.
- **Ogni PR** riceve la review dell'altro agente, che approva o chiede
  modifiche motivandole. Il merge lo fa il proprietario.
- **Prima di aprire una PR**: `pytest -q`, `ruff check .` e `mypy` verdi;
  `JABBA_FORK=1 pytest tests/test_fork_mainnet.py -s` se tocchi il percorso
  di lancio.
- **Bug nel codice dell'altro**: un test che lo riproduce, nella sua PR o
  nell'issue. I bug confermati e non ancora corretti si marcano
  `@finding(...)` (xfail strict): quando vengono sistemati il test diventa
  rosso e il marcatore va tolto.
- **Fatti misurati, non ricordati**: costi, fee, account e discriminatori
  di pump.fun si verificano sul fork mainnet o sull'IDL ufficiale
  (`pump-fun/pump-public-docs`), citando la fonte nel codice.
