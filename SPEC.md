# Jabba — Technical Specification

Presale bot with an atomic token launch on pump.fun (Solana).

| | |
|---|---|
| Status | Validated on a mainnet fork; not yet launched on mainnet |
| Last updated | 2026-11-10 |
| Source of truth | The code in `curvebot/`. Every number marked *measured* comes from `tests/test_fork_mainnet.py` or from the official pump.fun IDL (`pump-fun/pump-public-docs`) |

## 1. Overview

Jabba lets a closed group pool SOL and enter a new pump.fun token **together, at the same price**.

1. **Presale.** Whitelisted users send SOL to one treasury address. An off-chain ledger records who contributed how much. There is no price and no token yet. Contributions can be refunded until launch.
2. **Launch.** One atomic Jito bundle creates the token, buys it on the untouched bonding curve with the pooled SOL (the *dev buy*), and distributes the bought tokens to every contributor pro rata. Either all of it lands in one slot or none of it does.
3. **Market.** From then on the token trades on pump.fun like any other. Jabba plays no further part, except collecting the creator fee.

### Design goals

- **One price for everyone.** All contributors get tokens from a single buy at the bottom of the curve.
- **No intermediate custody of tokens.** The treasury never holds users' tokens in a confirmed block.
- **No on-chain program of our own.** Only existing programs are used: System, Token-2022, Associated Token Account, pump.fun.
- **Funds are never guessed about.** Any operation with an uncertain outcome is held until the chain settles it.

### Non-goals

- Trustless presale. The presale is custodial (section 2).
- More than 36 participants per launch (section 6.6).
- Trading, market making or sniping after launch.

## 2. Trust model

| Phase | Who holds the funds | What users must trust |
|---|---|---|
| Presale | The treasury key, held by the operator | The operator: refunds and launch are executed by the bot, not enforced by a contract |
| Launch | Nobody: one atomic bundle | Nothing beyond Solana, Jito and pump.fun |
| After launch | Each user's own wallet | Nothing |

The ledger that decides each user's share lives in the operator's database. Deposits and refunds are ordinary on-chain transfers, so the ledger can be re-derived from the treasury's history (`/reconcile` does this for a recent window).

Access is restricted to `ALLOWED_TELEGRAM_IDS`. The first id is the **admin**, the only one who can launch, reconcile, resolve or sweep. The bot refuses to start with an empty whitelist.

## 3. Lifecycle

```
            /launch (admin)              bundle landed
  OPEN ───────────────────► LAUNCHING ─────────────────► LAUNCHED
   ▲                            │
   │  bundle failed             │ outcome unknown (timeout)
   ├────────────────────────────┤
   │                            ▼
   │  /launchresolve:      LAUNCH_UNKNOWN ──/launchresolve: mint exists──► LAUNCHED
   └── mint absent after 5 min ─┘
```

| State | Stored as | Deposits | Refunds |
|---|---|---|---|
| OPEN | no `launch_lock` | credited | allowed |
| LAUNCHING | `launch_lock` set | not credited | suspended |
| LAUNCH_UNKNOWN | `launch_lock` set, `state = launch_unknown` | not credited | suspended |
| LAUNCHED | `launch_lock` set, `state = launched` | not credited | closed |

The launch lock is a row in the `meta` table, taken with an atomic check-and-set. It survives restarts, so two launches can never run. It is released only when a launch is known to have failed.

## 4. Architecture

A single Python process (`python -m curvebot.main`) runs two concurrent tasks that share one SQLite database and the treasury key.

| Task | Role |
|---|---|
| Telegram polling (aiogram 3) | Commands and buttons. Sends refunds, runs the launch |
| Deposit monitor | Every `POLL_INTERVAL_S`, credits new finalized deposits |

```
curvebot/
  main.py        entry point; restart recovery
  config.py      settings from the environment
  db.py          SQLite schema and queries; launch lock
  service.py     ledger rules: deposits, refund reservations, reconciliation
  monitor.py     deposit polling loop
  chain.py       RPC access, deposit parsing, transaction building, Jito bundles
  pump_ix.py     pump.fun instructions: create_v2, buy_v3, creator-fee collection
  pump_curve.py  bonding-curve math, fee cap, account sizes
  launch.py      dev-buy planning, cost estimate, distribution math, bundle state machine
  handlers.py    Telegram commands and callbacks
  keyboards.py   inline keyboards
  chart.py       PNG of the curve with the planned dev buy
scripts/dryrun.py  zero-SOL check of the launch against live mainnet
```

External services: a Solana RPC endpoint, Pinata (token metadata on IPFS), the Jito block engine. No other third party is in the path of the funds.

SQLite runs in WAL mode. The handler connection is autocommit; every multi-step change that must be atomic runs in an explicit `BEGIN IMMEDIATE` transaction.

## 5. Presale

### 5.1 Wallet registration

`/setwallet <pubkey>` binds one Solana address to the Telegram user. It is both the only address deposits are accepted from and the address that receives refunds and tokens.

- The address must be a valid 32-byte public key. Program-derived addresses are accepted.
- An address can belong to one user only.
- It cannot be changed once the user has a contribution.

### 5.2 Deposits

A deposit is credited when all of these hold:

1. The transaction is **finalized** and succeeded.
2. It contains a top-level System Program `Transfer` to the treasury. Each such transfer in a transaction counts separately and is identified as `<signature>:<index>`.
3. The sender is a registered wallet.
4. The amount is at least `MIN_DEPOSIT_SOL`.
5. The presale is OPEN.

Transfers that fail rule 3, 4 or 5 are logged and never credited. They are not refunded automatically: they show up in `/reconcile` as *missing in ledger* for the admin to handle. This includes withdrawals sent straight from an exchange and transfers made through another program (for example a multisig), which rule 2 does not see.

**Idempotency.** Crediting a deposit and marking it as processed happen in one database transaction, so a crash can neither lose a deposit nor credit it twice. A transaction that the RPC cannot return yet is left unmarked and retried on the next poll.

**Scanning.** The monitor walks the treasury's signature history newest-first, skips signatures already processed without fetching them again, and stops at the first page that contains nothing new (up to 10 pages per poll).

### 5.3 Refunds

Available until launch, for 25, 50, 75 or 100% of the user's *available* contribution (contribution minus refunds in progress). The fee is `REFUND_FEE_BPS` (default 0.30%) and stays in the treasury.

```
quote (no write) ──confirm──► reserved ──SOL sent──► recorded in ledger
                                 │
                                 └─ send error or crash ──► UNKNOWN (amount stays held)
                                                              │  /reconcile
                                          matching transfer found ──► recorded
                                          none after 10 minutes   ──► released
```

- **Quote.** A preview only. Nothing is written, so an abandoned quote locks nothing.
- **Reserve.** On confirmation, inside a per-user lock: the amount is re-quoted against the ledger and reserved in one atomic transaction. Concurrent confirmations cannot both pass.
- **Send.** A System transfer of the net amount to the registered wallet.
- **Unknown.** If sending raises, the transfer may still have landed. The reservation is kept and marked `unknown`. Reservations found in progress at startup are also marked `unknown`, never released blindly.
- **Resolve.** `/reconcile` looks for a finalized transfer from the treasury to the user's wallet with exactly the net amount. A transfer already recorded as a refund can never settle another one. If none is found 10 minutes after the reservation, it is released; this is safe because a transaction cannot land after its blockhash expires.

Refunds below one lamport net are rejected.

### 5.4 Shares

A user's share is their net contribution divided by the total. `/position` shows the contribution, the share and an estimate of tokens at launch, computed as that share of one pooled dev buy.

## 6. Launch

### 6.1 Preconditions

`/launch` is admin-only and asks for a second confirmation. It refuses unless:

- the presale is OPEN and at least one user has a positive contribution;
- there are at most 36 contributors;
- `TOKEN_NAME`, `TOKEN_SYMBOL` and `PINATA_JWT` are set;
- the on-chain pump.fun global account still matches the curve parameters in section 7.2.

### 6.2 Sequence

1. Take the launch lock.
2. Read the pump.fun global account and compare its parameters.
3. Plan the dev buy and the full cost against the treasury's real balance (6.3).
4. Generate the mint keypair. Upload image and metadata to Pinata.
5. Build transaction 1: `create_v2` + treasury token account + `buy_v3` for an **exact** token amount (6.4).
6. Compute each user's token amount (6.5).
7. Build up to 4 distribution transactions, 9 recipients each; the last one carries the Jito tip (6.6).
8. Send the bundle and wait for the outcome (6.7).
9. On success, record the launch and set the state to LAUNCHED.

Any failure before step 8 signs nothing that can land and releases the lock.

### 6.3 Dynamic dev buy and cost model

The target dev buy is `raised × (100 − TEAM_KEEP_PERCENT)%`. It is lowered, if needed, to the largest amount the treasury can pay after every other cost. Whatever SOL is left belongs to the team.

All costs are computed in lamports from **live rent** (`getMinimumBalanceForRentExemption`); if the RPC call fails, the classic rent is used, which is an upper bound.

| Cost | Amount | Notes |
|---|---|---|
| Dev buy | curve SOL + 1.75% | Fee cap: actual fees are 1.25% (7.3) |
| Accounts opened by transaction 1 | ≈ 0.0086 SOL | Mint (316 bytes + name + symbol + URI), bonding curve (166), its token account (170), treasury token account (170), volume accumulator (137) |
| Recipient token accounts | ≈ 0.0015 SOL each | 170 bytes; assumes none exists yet |
| Signatures | 5,000 lamports each | 2 for transaction 1, 1 per distribution transaction |
| Jito tip | `JITO_TIP_LAMPORTS` | Default 0.001 SOL |
| Treasury reserve | ≈ 0.00065 SOL | The treasury must stay rent-exempt or the last transaction fails |

SOL figures are *measured* with mainnet rent in November 2026. There is no priority fee: inside a bundle the tip pays for inclusion.

The launch is refused when:

- the affordable dev buy is below `MIN_DEV_BUY_PERCENT` (default 50%) of the raise — the raise is too small for the fixed costs;
- the dev buy would reach the curve's real token reserves, 85.005 SOL into the curve (a raise of about 94.45 SOL at the default 10% team share).

| | 3 contributors | 36 contributors |
|---|---|---|
| Fixed costs | 0.0148 SOL | 0.0648 SOL |
| Smallest raise that can launch | ≈ 0.030 SOL | ≈ 0.132 SOL |
| Raise above which the full 90% target is used | ≈ 0.176 SOL | ≈ 0.769 SOL |

*Measured on the fork:* 36 × 0.25 SOL → dev buy 8.1 SOL, 228,118,110 tokens distributed, 0.735 SOL left to the team. 3 × 0.0333 SOL → dev buy 0.0837 SOL, 2,987,032 tokens.

### 6.4 Transaction 1: create and buy

Built directly on the pump.fun program from the official IDL, signed by the treasury (payer, creator, buyer) and the mint keypair. No aggregator or router program is involved.

| # | Instruction | Purpose |
|---|---|---|
| 1 | `create_v2` | Token-2022 mint with metadata, bonding curve. SOL-paired, mayhem mode off, creator = treasury |
| 2 | Create associated token account (idempotent) | Treasury's Token-2022 account; `buy_v3` does not create it |
| 3 | `buy_v3(amount, max_sol_cost)` | Buys exactly `amount` base units, paying at most `max_sol_cost` |

`amount = floor(VTR0 × S / (VSR0 + S))` for a dev buy of `S` lamports, capped at the real reserves. `max_sol_cost = S + ceil(S × 1.75%) + 1`. If pump.fun's fees ever exceed the cap, the buy fails and the whole bundle reverts.

Limits: name ≤ 32 bytes, symbol ≤ 13, URI ≤ 200; the transaction must fit 1,232 bytes (*measured:* 1,151 bytes with an 80-character URI).

### 6.5 Distribution math

Token amounts are integers in base units (6 decimals). Weights are the users' net contributions, converted to exact rationals.

```
team_i  = floor(bought × TEAM_SHARE_BPS / 10000)
pool    = floor(bought × (10000 − TEAM_SHARE_BPS − SAFETY_BPS) / 10000)
share_u = floor(pool × weight_u / Σ weights)
dust    = pool − Σ share_u            (less than one base unit per user)
```

Invariant: `Σ share_u + team + dust ≤ bought`. Because `buy_v3` buys an exact amount, `SAFETY_BPS` defaults to 0 and, with `TEAM_SHARE_BPS = 0`, users receive everything bought except the dust. Team tokens and dust stay in the treasury's token account.

### 6.6 Distribution transactions and the 36-participant limit

For each recipient: create their Token-2022 associated account if missing, then `transfer_checked`.

| Limit | Value | Source |
|---|---|---|
| Transactions per Jito bundle | 5 | Jito; one is transaction 1 |
| Recipients per transaction | 9 | 92 bytes per recipient against the 1,232-byte limit, *measured* |
| Participants per launch | 36 | 4 × 9 |

Address lookup tables would not raise this meaningfully: the 64-entry instruction trace limit caps a transaction at 10 recipients (*measured*), so 40 at most. Bundles are not atomic with each other, so a launch with more than 36 contributors is refused rather than split.

### 6.7 Bundle outcome

The bundle is sent to the Jito block engine and followed in two phases: `getInflightBundleStatuses` until it lands or fails (up to 90 s), then `getBundleStatuses` until confirmed (up to 30 s).

| Outcome | Meaning | Action |
|---|---|---|
| Landed | Everything executed in one slot | Record launch, state LAUNCHED |
| Failed | Nothing executed | Release the lock; the presale is OPEN again and the launch can be retried |
| Timeout | Unknown | State LAUNCH_UNKNOWN, lock kept. **Never retried automatically** |

`/launchresolve` settles an unknown outcome from the chain. The bundle is atomic, so the mint account exists if and only if it landed:

- mint exists (finalized) → LAUNCHED, launch recorded;
- mint absent and more than 5 minutes have passed (the blockhash is long expired) → lock released, OPEN;
- otherwise → still unknown, try again later.

## 7. pump.fun integration

### 7.1 Programs and instructions

| Program | Address |
|---|---|
| Pump | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` |
| Pump AMM (PumpSwap) | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` |
| Pump Fees | `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ` |
| Mayhem | `MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e` |
| Global account | `4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf` |

| Instruction | Discriminator | Accounts |
|---|---|---|
| `create_v2` | `d6904cec5f8b31b4` | 16 |
| `buy_v3` | `07051dc4f5176550` | 17 |
| `collect_creator_fee_v2` | `cf118af204221338` | 10 |
| `collect_coin_creator_fee` (AMM) | `a039592ab58b2b42` | 8 |

Account order follows the IDL. The nightly fork test executes the launch against the real programs, so a change in accounts, discriminators or fees is caught before a launch.

### 7.2 Curve parameters

Checked against the global account before every launch; a mismatch aborts.

| Parameter | Value |
|---|---|
| Initial virtual token reserves (`VTR0`) | 1,073,000,000 tokens |
| Initial virtual SOL reserves (`VSR0`) | 30 SOL |
| Initial real token reserves | 793,100,000 tokens |
| Total supply | 1,000,000,000 tokens |
| Decimals | 6 |
| Protocol fee | 95 bps |

### 7.3 Fees

Fees are charged **on top** of the SOL that enters the curve: 0.95% protocol + 0.30% creator = 1.25%. *Measured:* a 1 SOL dev buy costs the treasury 1.0125 SOL plus rent and returns exactly 34,612,903.225806 tokens.

The creator of the token is the treasury, so it earns 0.30% of all later trading volume. Fees from `buy_v3` / `sell_v3` trades are not paid out per trade: they wait on the bonding curve (`BondingCurve.creator_fee`) until `sweep_creator_fee` moves them to the creator vault. Fees from legacy trade instructions go to the vault directly. After graduation the same applies to the PumpSwap pool and its own vault.

### 7.4 Creator-fee collection

`/sweep` (admin) reports the curve vault balance and sends `collect_creator_fee_v2` (bonding-curve vault) and `collect_coin_creator_fee` (PumpSwap vault; fails harmlessly before graduation). Collected SOL arrives in the treasury. See open item 1 in section 12.

## 8. Data model

SQLite, one file (`DB_PATH`).

| Table | Columns | Purpose |
|---|---|---|
| `users` | `telegram_id` PK, `wallet`, `created_at` | Wallet registration |
| `ledger` | `id`, `telegram_id`, `kind` (`deposit` \| `refund`), `amount_sol`, `fee_sol`, `signature`, `created_at` | Append-only record of contributions |
| `pending_refunds` | `id`, `telegram_id`, `amount_sol`, `fee_sol`, `status` (`pending` \| `unknown`), `created_at` | Refund reservations |
| `processed_sigs` | `signature` PK, `created_at` | Signatures and `<signature>:<index>` keys already examined |
| `launches` | `id`, `mint`, `sig_create`, `sig_buy`, `dev_buy_sol`, `tokens_bought`, `tokens_distributed`, `team_tokens`, `created_at` | One row per landed launch |
| `meta` | `key` PK, `value` | `state`, `mint`, `launch_lock`, `pending_mint`, `pending_bundle`, `pending_at`, `pending_report` |

- Contribution of a user = Σ deposits − Σ refunds.
- Available contribution = contribution − Σ pending refunds (both statuses).
- Total raised = Σ contributions.

## 9. Configuration

Environment variables (see `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Required |
| `ALLOWED_TELEGRAM_IDS` | — | Required. Comma-separated; the first is the admin |
| `RPC_URL` | devnet | Use a mainnet endpoint for real launches |
| `KEYPAIR_PATH` | `treasury.json` | Treasury keypair file; never committed |
| `DB_PATH` | `curvebot.db` | |
| `MIN_DEPOSIT_SOL` | 0.01 | Smaller deposits are not credited |
| `POLL_INTERVAL_S` | 5 | Deposit polling period |
| `REFUND_FEE_BPS` | 30 | Refund fee, 0.30% |
| `TOKEN_NAME`, `TOKEN_SYMBOL` | — | Required to launch |
| `TOKEN_DESCRIPTION`, `TOKEN_TWITTER`, `TOKEN_TELEGRAM`, `TOKEN_WEBSITE` | empty | Metadata |
| `TOKEN_IMAGE_PATH` | `token.png` | Uploaded to IPFS at launch |
| `PINATA_JWT` | — | Required to launch |
| `TEAM_KEEP_PERCENT` | 10 | Target share of the raised SOL kept by the team |
| `MIN_DEV_BUY_PERCENT` | 50 | Launch refused if the dev buy would fall below this share of the raise |
| `TEAM_SHARE_BPS` | 0 | Share of the bought tokens kept by the team |
| `SAFETY_BPS` | 0 | Share of the bought tokens left undistributed |
| `JITO_TIP_LAMPORTS` | 1,000,000 | Bundle tip |

## 10. Commands

| Command | Who | Effect |
|---|---|---|
| `/start`, `/help`, `/menu` | whitelist | Status panel with buttons |
| `/info` | whitelist | Raised SOL, participants, deposit address |
| `/setwallet <pubkey>` | whitelist | Register the wallet |
| `/position` | whitelist | Contribution, share, estimated tokens |
| `/refund` | whitelist | Guided refund with quote and confirmation |
| `/launch` | admin | Atomic launch, with confirmation |
| `/launchresolve` | admin | Settle a launch with unknown outcome from the chain |
| `/reconcile` | admin | Compare ledger and chain; resolve unknown refunds |
| `/sweep` | admin | Collect creator fees |

Buttons: how to participate, refund, position, curve chart, info. Messages from users outside the whitelist are ignored.

### Reconciliation

`/reconcile` compares the ledger with the finalized deposits among the treasury's 50 most recent signatures and reports six categories. It never changes the ledger on its own; the only automatic step is resolving unknown refunds (5.3).

| Category | Meaning |
|---|---|
| Missing in ledger | On-chain deposit that was not credited (unknown sender, below minimum, or after the launch snapshot) |
| Duplicate in ledger | Same deposit credited twice |
| Amount mismatch | Ledger and chain disagree |
| Unverifiable | Ledger entry older than the window; not an error |
| Unknown refunds | Reservations still held |
| Refunds without signature | Recorded without an on-chain reference |

## 11. Validation

| Level | What it proves |
|---|---|
| Unit and property-based (hypothesis) | Curve math, cost inversion, exact distribution, chunking, ledger state machine against an exact model |
| Handlers | Telegram flows with fake updates, including concurrent refunds and launches |
| LiteSVM (`tests/test_svm.py`) | The real `Chain` code executed by a real SVM: refunds, deposit detection end to end, distribution to 36 users on Token-2022 |
| Mainnet fork (`JABBA_FORK=1 pytest tests/test_fork_mainnet.py -s`) | The real `run_launch` against pump.fun, Token-2022 and fee programs cloned from mainnet with mainnet rent. Nothing is sent to mainnet |
| Dry run (`python scripts/dryrun.py --wallets N --raise SOL`) | With the real key and RPC: curve parameters, dev-buy plan, transaction 1 signed and simulated, transaction sizes. Sends nothing |

208 offline tests and 3 fork scenarios. CI runs ruff, mypy, the test suite on Python 3.10 and 3.12, pip-audit and gitleaks on every push and pull request, and the fork test nightly and on demand.

**Before a real launch:** run the fork test, then the dry run with the funded treasury. The simulation in the dry run only passes once the treasury holds the SOL the plan needs.

Known bugs that are not fixed yet are kept as strict expected-failure tests (`@finding`), so fixing one turns its test red until the marker is removed.

## 12. Open items and limitations

1. **`/sweep` misses fees waiting on the curve.** It calls the two `collect_*` instructions but not `sweep_creator_fee` first, so creator fees from `buy_v3` / `sell_v3` trades, including our own dev buy, stay on the curve (and on the pool after graduation). The sweep instructions are not yet covered by the fork test.
2. **Ledger amounts are floating-point SOL.** Shares are computed exactly and drift stays below one lamport, but integer lamports would be cleaner.
3. **Unattributed deposits need manual handling.** Transfers from unregistered wallets, from exchanges, through other programs, below the minimum or after the launch snapshot are reported by `/reconcile` but not refunded automatically.
4. **`/reconcile` sees a 50-signature window.** Older ledger entries are reported as unverifiable, not checked.
5. **The `/launch` confirmation shows the target split** (`TEAM_KEEP_PERCENT`), not the planned dev buy, which can be lower for small raises. The final report shows the real figures.
6. **Hot key, single process.** The treasury key sits in a file on the bot's host; there is no multisig and no standby instance.
7. **Polling.** Deposits are credited after finality plus up to one polling period.
8. **Market behaviour is out of scope.** All participants hold tokens at the same low price at launch and can sell immediately; wallets funded in one bundle are flagged as bundled by screeners.

## 13. Roadmap

1. First mainnet launch at small size (about 0.1 SOL from 3 wallets).
2. `sweep_creator_fee` in `/sweep`, validated on the fork.
3. Ledger in integer lamports.
4. WebSocket deposit detection.
5. Launches beyond 36 participants as an explicit, declared non-atomic mode — never implicit.
