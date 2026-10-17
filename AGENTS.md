# Repository Guidelines

## Project Structure & Module Organization

SURGE is a local-first, paper-trading dashboard. The FastAPI service and browser UI live in `dashboard/backend` and `dashboard/frontend`. Trading adapters and risk checks are in `bot/`; market data collectors, scheduling, and storage schema are in `data/`. Strategy code and JSON definitions are in `strategies/`, while experimental prediction code is in `prediction/`. Research notes are under `research/`, operations and Compose configuration under `deploy/`, and automated checks under `tests/`. Keep changes in the owning module and update relevant docs when behavior or operations change.

## Build, Test, and Development Commands

- `python -m pip install -r requirements.txt` installs runtime dependencies; `requirements-dev.txt` adds the API test client.
- `./deploy/deploy.sh start` builds and starts the Docker Compose service at `http://127.0.0.1:8000`.
- `./deploy/deploy.sh logs` follows service logs; `./deploy/deploy.sh stop` stops the service while preserving its data volume.
- `python -m unittest discover -s tests` runs the unit tests using the standard library test runner (install `requirements-dev.txt` first for API tests).
- `python tests/smoke_deployment.py` checks the health endpoint, dashboard, status, and stored candles of an already running service.

## Coding Style & Naming Conventions

Use Python 3.12-compatible code, four-space indentation, descriptive `snake_case` names for modules, functions, and variables, and `PascalCase` for classes. Keep API and risk behavior explicit and fail closed when required state is missing. Tests use `unittest.TestCase` and `test_*.py` filenames. No formatter or linter is configured; follow nearby code and standard Python conventions. Keep strategy definitions valid JSON and consistent with `strategies/dsl/strategy_schema.json`.

## Testing Guidelines

Add or update focused unit tests in `tests/` for changes to strategy signals, order controls, and risk gates. Run the unittest discovery command above. For deployment or API changes, start the service and run the read-only smoke check. Tests that depend on network market data should not be required for core logic checks.

## Commit & Pull Request Guidelines

This checkout has no Git history, so repository-specific commit conventions cannot be confirmed. Use a short imperative commit subject, such as `Add order validation tests`. Pull requests should explain the behavior change, list relevant checks, link related issues when available, and include screenshots for visible dashboard changes.

## Security & Configuration

Keep secrets and local settings out of commits; `.env` is local configuration. The supported app flow is paper-only and binds to loopback. Do not expose the unauthenticated API publicly or add live order submission without updating the security and deployment documentation.

## Cursor Cloud specific instructions

Paper mode needs no exchange secrets. Leave live trading disabled, and do not create `.env` unless you are intentionally testing that path.

Cloud Agents install dependencies with `python3 -m pip install --user -r requirements-dev.txt` and symlink `uvicorn` into `/usr/local/bin`, because `~/.local/bin` is not on the default PATH. `python3 -m uvicorn` works without that symlink.

The boot service is host Python, not Docker Compose. It listens at `http://127.0.0.1:8000`. SQLite is created at `data/surge.sqlite3`, which is gitignored. Fetching candles calls public Coinbase, Binance, or Kraken endpoints and needs outbound network. `python3 -m unittest discover -s tests` does not. `python3 tests/smoke_deployment.py` expects the service to already be running.

`./deploy/deploy.sh` remains the Docker Compose path for machines that have Docker. This environment does not start a Docker daemon.
