"""Registry of external market-data sources, their probes, and a snapshot store.

`python -m data.sources status` probes every source once and prints a status table.
`python -m data.sources collect --every 300` keeps snapshots flowing into SQLite.
"""
from .catalog import SOURCES, Fetcher, Source, probe, probe_all
from .store import SnapshotStore

__all__ = ["SOURCES", "Fetcher", "Source", "SnapshotStore", "probe", "probe_all"]
