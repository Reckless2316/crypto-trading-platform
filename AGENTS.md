# AgenKit

This project uses **AgenKit** — a specialist-driven engineering workflow. Run `/agenkit`
to start the guided pipeline (Orient → Brainstorm → Architecture → Plan → Build → Review
→ Ship). Invoke a relevant AgenKit skill before acting; the `using-agenkit` skill explains
how the skills and specialist agents fit together.

## Query local memory first

Before reading or grepping source to locate code, consult AgenKit local memory:

- Run `agenkit memory status` (then `agenkit memory build` if it's missing, or
  `agenkit memory update` if it reports drift).
- Ask `agenkit memory query "<question>"` for scoped `file:line` hits, or
  `agenkit memory explain "<Symbol>"` for where a symbol is defined and how it connects.
- Open only the exact `file:line` the memory points to — not the whole file, and not a
  broad grep, unless the memory genuinely lacks the answer.

If the `agenkit` CLI is unavailable, read `.agenkit/memory/MEMORY.md` first, then fall
back to targeted reads. See the `codebase-memory` skill for the full workflow.

## Cursor Cloud specific instructions

Paper mode needs no exchange secrets. Leave live trading disabled, and do not create `.env` unless you are intentionally testing that path.

Cloud Agents install dependencies with `python3 -m pip install --user -r requirements-dev.txt` and symlink `uvicorn` into `/usr/local/bin`, because `~/.local/bin` is not on the default PATH. `python3 -m uvicorn` works without that symlink.

The boot service is host Python, not Docker Compose. It listens at `http://127.0.0.1:8000`. SQLite is created at `data/surge.sqlite3`, which is gitignored. Fetching candles calls public Coinbase, Binance, or Kraken endpoints and needs outbound network. `python3 -m unittest discover -s tests` does not. `python3 tests/smoke_deployment.py` expects the service to already be running.

`./deploy/deploy.sh` remains the Docker Compose path for machines that have Docker. This environment does not start a Docker daemon.


## Operator's exchange: OKX

The operator has an OKX account and API keys. **OKX US (CCXT `okxus`, `us.okx.com`) is the
primary trading venue** (`SURGE_LIVE_EXCHANGE=okxus`). It is the lowest-fee US-accessible exchange
on the operator's source list that has a trading API; see
`docs/research/2026-10-25-venue-selection.md`. It is spot only and excludes NY, TX and US
territories. Build and test with that in mind:

- **Secrets stay out of the repo.** Keys belong only in the local, gitignored `.env`, as
  `SURGE_OKX_API_KEY`, `SURGE_OKX_API_SECRET` and `SURGE_OKX_API_PASSPHRASE` (see `.env.example`).
  As of 2026-10-25 the main checkout's `.env` does not contain them yet. Never commit, log, print
  or echo key values. Never ask the operator to paste keys into a chat. If code needs them, read
  them from the environment.
- **Key permissions:** Read + Trade only, never Withdraw, with an IP allowlist where possible.
  Test against an OKX demo-trading key (CCXT `set_sandbox_mode(True)`) before any real-funds key.
- **Instrument names:** spot `BTC-USDT` / `BTC-USDC` (OKX US has no `BTC-USD` book); global-OKX
  perpetual swaps `BTC-USDT-SWAP` are data-only for a US operator. Public candles, funding
  and OI need no credentials (`data/collectors/okx_collector.py`, `data/public_metrics.py`).
- **Cost models:** use OKX US fees (entry tier 0.20% maker / 0.35% taker), not Binance's.
  Prefer maker orders and low-turnover strategies. Read the account's actual fee tier from OKX's
  read-only trade-fee endpoint rather than assuming one, and read the funding interval per
  instrument from the API.
- **Research data:** the long 5m OI/funding history in `data/binance_archive.py` comes from
  Binance's public archive. Before trading a result on OKX, confirm it holds on OKX's own data.
- **Hard rules are unchanged.** The web dashboard never loads keys. No live orders or arming
  without explicit operator approval for that specific step (`LIVE_ARM_REQUIRES_TASK22`).

## Data sources

`data/sources/` registers all 38 sources from the operator's API-status list
(`python -m data.sources status`, dashboard panel, `docs/DATA_SOURCES.md`). Add a new source as a
`Source` entry with a probe, and add a test that uses a fake session. Tests must not touch the
network. Never route around a geo-block; use Coinalyze or the Binance archive instead.

## Research record

- `docs/research/2026-10-25-fade-forced-flush/`: sweep + OI-flush + funding-crowding reversal,
  pre-registered and killed (−0.2R gross per trade on 11 of 12 coins; the filters were no better
  than random). Both samples are spent. Don't re-propose it unchanged.
