from unittest.mock import patch, MagicMock
from data.collectors.okx_collector import OkxCollector


def test_okx_parses_confirmed_candles():
    resp = MagicMock()
    resp.json.return_value = {"code": "0", "data": [
        ["3600000", "1", "2", "0.5", "1.5", "10", "0", "0", "1"],
        ["0", "1", "2", "0.5", "1.5", "10", "0", "0", "1"]]}
    with patch("data.collectors.okx_collector.requests.get", return_value=resp):
        out = OkxCollector().fetch_candles("BTC-USDT", "1h", 0, 7200)
    assert [c.ts_open for c in out] == [0, 3_600_000]
    assert out[0].ts_close == 3_600_000
