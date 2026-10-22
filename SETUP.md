# Setup, step by step

SURGE runs on your own computer. By default it does **paper trading**, with simulated money and no
real orders. Live trading exists, but it is off unless you configure and arm it on purpose (see
[deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md)).

## What you need
- **Python 3.12 or newer**: <https://www.python.org/downloads/>
- An internet connection. Market data comes from public exchange APIs.

## 1. Open a terminal in this folder
- **Windows:** to skip the steps below, install Docker Desktop and double-click `launch-windows.bat`.
- **Mac / Linux:** open Terminal and `cd` into the project folder.

## 2. Install
```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
On Windows, use `.venv\Scripts\pip` and `.venv\Scripts\python` instead of `.venv/bin/...`.

## 3. Start it
```sh
.venv/bin/python -m uvicorn dashboard.backend.app:app --host 127.0.0.1 --port 8000
```
Leave this window open. Press **Ctrl+C** to stop.

## 4. Open it
Go to <http://127.0.0.1:8000>.

## 5. Click through
The **Quick start** box walks you through it:
1. **Get prices** downloads candles from the selected exchange.
2. **Ask a strategy** shows the current signal. It is not advice.
3. **Test on history** backtests the strategy on stored candles.
4. **Arm paper trading** asks for an approved release hash, then allows simulated orders for 30 minutes.

## Is it working?
- **Observability** shows server health, request counts and recent errors. It refreshes every
  5 seconds. Raw data: <http://127.0.0.1:8000/api/observability>.
- **API status** shows every market-data source and whether it answered. See
  [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) for sources that need a free key.

## Keys and safety
- Exchange and data keys go in a local `.env` file. It is gitignored: never commit it, and
  never paste keys anywhere public.
- The web dashboard refuses to start if exchange **trading** keys are in its environment. It
  needs none to run.
- Exchange keys should have Read + Trade permission only, never Withdraw.
- The red **Kill switch** blocks new paper orders.

## Stuck?
- `command not found`: Python isn't installed or isn't on your PATH.
- The page won't open: is the terminal from step 3 still running?
- "Address already in use": use `--port 8001` and open that port instead.
