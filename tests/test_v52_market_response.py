from __future__ import annotations

import json
import time

import pytest

from v52.intelligence.market_response import SemanticMarketReader
from v52.storage.sqlite import connect


def test_market_response_uses_only_time_aligned_prices(settings) -> None:
    path = settings.data_dir / "market" / "market_binance" / "semantic.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path, settings) as con:
        con.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,event_type TEXT,venue TEXT,symbol TEXT,exchange_ts TEXT,observed_at TEXT,lane TEXT,payload_json TEXT)")
        base = 1_788_000_000.0
        for i, price in enumerate((100.0, 101.0, 103.0, 102.0, 104.0)):
            observed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + i * 30))
            con.execute("INSERT INTO events(event_type,venue,symbol,observed_at,lane,payload_json) VALUES('trade','BINANCE','BTCUSDT',?,'FLOW',?)", (observed, json.dumps({"price": price, "volume": 10 + i})))
    reader = SemanticMarketReader(settings)
    points = reader.series_sync("BTCUSDT", base, base + 120)
    measured = reader.measure(points, base, base + 120)
    assert measured is not None
    assert measured["start_price"] == 100.0
    assert measured["end_price"] == 104.0
    assert round(measured["return_value"], 6) == .04
    assert measured["observed_samples"] == 5
    assert measured["mfe"] == pytest.approx(.04)


def test_market_response_refuses_missing_end_observation(settings) -> None:
    reader = SemanticMarketReader(settings)
    assert reader.measure([], 100.0, 160.0) is None
