import json
import threading

from data import stream


def test_stream_one_writes_jsonl(tmp_path):
    p = tmp_path / "BTC-USDT.log"
    stream.stream_one("BTC-USDT", p, 0.0, threading.Event(), max_ticks=2,
                      fetch=lambda i: {"instrument": i, "last": 1.0})
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["instrument"] == "BTC-USDT"


def test_stream_logs_errors(tmp_path):
    def boom(i):
        raise RuntimeError("x")
    p = tmp_path / "e.log"
    stream.stream_one("ETH-USDT", p, 0.0, threading.Event(), max_ticks=1, fetch=boom)
    assert json.loads(p.read_text())["error"] == "RuntimeError"
