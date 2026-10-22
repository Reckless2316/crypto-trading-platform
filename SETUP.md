# Setup for Beginners (the easy way)

This app is **fake-money practice trading**. No real money is used. You can't lose anything. 🎮

## What you need
- A computer (Windows, Mac or Linux)
- **Python 3.12 or newer** — get it at <https://www.python.org/downloads/>
- An internet connection

## Step 1: Open a terminal
- **Windows:** press the Windows key, type `cmd`, press Enter.
- **Mac:** press Cmd+Space, type `Terminal`, press Enter.
- Use the `cd` command to go into this project's folder, e.g. `cd crypto-trading-platform`.

## Step 2: Install (copy and paste each line, press Enter)
```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
(On Windows use `.venv\Scripts\pip` and `.venv\Scripts\python` instead of `.venv/bin/...`.)

## Step 3: Start it
```sh
.venv/bin/python -m uvicorn dashboard.backend.app:app --host 127.0.0.1 --port 8000
```
Leave this window open. To stop the app later, press **Ctrl+C**.

## Step 4: Open it
Go to <http://127.0.0.1:8000> in your web browser.

## Step 5: Click around
Use the **Quick start** box on the page:
1. **Get prices** – downloads real market prices.
2. **Ask a strategy** – shows a buy/sell idea.
3. **Test on history** – pretend-trades the past to see what would have happened.
4. **Arm paper trading** – turns on fake-money trading for 30 minutes.

## Is it working? Look at the health box
The **Observability** panel tells you if things are OK:
- 🟢 All good = everything works.
- 🔴 Problem = read the message next to it. Common fix: check your internet.

It refreshes by itself every 5 seconds. It also lists recent errors and actions.
You can see the same data at <http://127.0.0.1:8000/api/observability>.

## Safety rules
- **Never** make a `.env` file or type in exchange keys unless an adult helps you. The app doesn't need them.
- Don't turn on "live trading". It stays off by default.
- The big red **Kill switch** button stops new fake orders.

## Stuck?
- `command not found`: Python isn't installed or isn't on your PATH.
- Page won't open: is the terminal from Step 3 still running?
- "Address already in use": change `--port 8000` to `--port 8001` and open that port.
