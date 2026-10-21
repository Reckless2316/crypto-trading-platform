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

