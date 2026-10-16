#!/usr/bin/env python3
"""Scheduler skeleton: cron/APScheduler for real-time collection + hourly gap detection + backfill."""
import croniter, time
from datetime import datetime

# Real-time: every minute at :05 (after candle close for most exchanges)
# Gap detection: hourly at :30
# Backfill: manual trigger

REALTIME_SCHEDULE = "*/1 * * * *"  # Every minute — could be tuned to :05 for candle-close alignment
GAP_DETECTION_SCHEDULE = "30 * * * *"  # At minute 30 past the hour

class CryptoScheduler:
    def __init__(self, collectors_config: dict):
        self.config = collectors_config  # {"coinbase": {"sources": {"BTCUSDT": {"timeframe": "1m"}}}, ...}

    def realtime_tick(self):
        """Called by scheduler. Runs all collectors for active symbols."""
        # Implementation: for each collector, fetch latest candle for each (symbol, timeframe)
        # Insert into provenance DB (idempotent via UNIQUE constraint)
        # Log metrics: latency, status code, errors, duplicates detected
        pass

    def gap_check_tick(self):
        """Called hourly. Runs gap detector per source/symbol/timeframe."""
        pass

    def backfill_trigger(self, source_id: str, symbol: str, timeframe: str, start: str, end: str):
        """Manual trigger. Downloads historical candles and inserts."""
        pass
