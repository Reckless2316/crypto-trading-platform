#!/usr/bin/env python3
"""Abstract collector interface for crypto trading platform.
Every collector implements fetch_candles(symbol, timeframe, start, end) -> List[Candle]."""
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
import hashlib, json

@dataclass
class Candle:
    open: float; high: float; low: float; close: float
    volume: float; ts_open: int; ts_close: int  # unix ms

@dataclass
class TickProvenance:
    source_id: str; symbol: str; timeframe: str
    ts_source: str  # ISO 8601 from exchange
    ts_fetch: str   # ISO 8601 when fetched
    candle: Candle
    raw_hash: str   # SHA256 of raw JSON response
    latency_ms: int; status_code: int; error: Optional[str]

class Collector:
    """Base collector. Subclass per exchange."""
    def __init__(self, source_id: str, auth_config: Dict[str, Any]):
        self.source_id = source_id
        self.auth = auth_config

    def fetch_candles(self, symbol: str, timeframe: str, start: int, end: Optional[int] = None) -> List[Candle]:
        raise NotImplementedError("Subclasses must implement fetch_candles.")

    def hash_raw(self, raw: Any) -> str:
        return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:32]
