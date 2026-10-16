-- SQLite provenance schema used by the local SURGE paper-trading deployment.
PRAGMA foreign_keys = ON;
BEGIN;

CREATE TABLE IF NOT EXISTS tick_provenance (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  ts_source INTEGER NOT NULL,
  ts_fetch INTEGER NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume REAL NOT NULL,
  raw_response_hash TEXT,
  fetch_latency_ms INTEGER,
  http_status INTEGER,
  error_message TEXT,
  UNIQUE(source_id, symbol, timeframe, ts_source)
);
CREATE INDEX IF NOT EXISTS idx_tick_symbol_time ON tick_provenance(symbol,timeframe,ts_source);
CREATE INDEX IF NOT EXISTS idx_tick_source_symbol ON tick_provenance(source_id,symbol);

CREATE TABLE IF NOT EXISTS order_provenance (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('paper')),
  strategy_id TEXT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  order_type TEXT NOT NULL DEFAULT 'market' CHECK(order_type='market'),
  qty REAL NOT NULL CHECK(qty > 0),
  price REAL NOT NULL CHECK(price > 0),
  status TEXT NOT NULL CHECK(status IN ('filled','rejected')),
  ts_created INTEGER NOT NULL,
  fill_qty REAL,
  fill_price REAL,
  fill_fees REAL NOT NULL DEFAULT 0,
  gate_checks_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_order_account_time ON order_provenance(account_id,ts_created);
CREATE INDEX IF NOT EXISTS idx_order_symbol_time ON order_provenance(symbol,ts_created);

COMMIT;
