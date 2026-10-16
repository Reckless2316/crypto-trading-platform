# SURGE execution scope

The running dashboard supports one local paper account. Orders are simulated at the latest stored Coinbase 1h close and stored with fees in SQLite. Its controls are implemented by `dashboard/backend/app.py`; `bot/risk_gates.py` contains the shared fail-closed risk checks.

The database starts with the kill switch on and no arm lease. Arming grants a time-limited paper-trading lease (up to 60 minutes); the dashboard uses 30 minutes. The configured limits are 5% of starting equity per symbol, 3% daily realized loss, and three open positions. Buy orders are blocked when any global gate is closed. Sell and flatten orders only reduce existing long positions and remain available while disarmed or killed.

`bot/paper_coinbase_adapter.py` is a separate account-scoped helper and requires an injected price provider. `bot/live_coinbase_adapter.py` fails closed because signed live account and order operations have not been implemented. The web app has no live mode.
