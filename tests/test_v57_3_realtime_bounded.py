from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

from v52.intelligence.openai_semantics import OpenAISemanticExtractor
from v52.metrics import Metrics
from v52.models import RawNewsItem
from v52.news.processor import NewsProcessor
from v52.storage.news import NewsStore
from v52.storage.sqlite import connect
from v52.storage.structured import StructuredStore


def test_structured_state_persists_only_real_transitions(settings, monkeypatch):
    monkeypatch.setenv("STRUCTURED_STATE_BUCKET_SECONDS", "60")
    store = StructuredStore(settings)
    clock = {"now": 1_000.0}
    monkeypatch.setattr("v52.storage.structured.time.time", lambda: clock["now"])

    state_a = {"symbol": "XAUUSD", "value": 100.0}
    state_b = {"symbol": "XAUUSD", "value": 101.0}
    assert asyncio.run(store.insert_snapshot("fixture", "macro", "STATE", state_a))
    clock["now"] += 120
    assert not asyncio.run(store.insert_snapshot("fixture", "macro", "STATE", state_a))
    clock["now"] += 120
    assert asyncio.run(store.insert_snapshot("fixture", "macro", "STATE", state_b))
    clock["now"] += 120
    assert asyncio.run(store.insert_snapshot("fixture", "macro", "STATE", state_a))

    with connect(store.path, settings) as con:
        rows = con.execute("SELECT payload_hash FROM snapshots ORDER BY id").fetchall()
    assert len(rows) == 3
    assert rows[0]["payload_hash"] == rows[2]["payload_hash"]
    assert rows[0]["payload_hash"] != rows[1]["payload_hash"]


def test_news_admission_quota_is_shared_per_source(settings, monkeypatch):
    monkeypatch.setenv("NEWS_MAX_ROWS_PER_SOURCE_WINDOW", "3")
    monkeypatch.setenv("NEWS_SOURCE_WINDOW_SECONDS", "300")
    store = NewsStore(settings)

    def item(index: int, source: str) -> RawNewsItem:
        return RawNewsItem(
            raw_key=f"{source}-{index}", source_key=source, source_name=source,
            source_url=f"https://{source}.example/feed", item_url=f"https://{source}.example/{index}",
            title=f"Central bank policy decision {index}", summary="rate decision",
            category="ECONOMY", region="GLOBAL", language="en",
            published_at="2026-09-19T00:00:00Z", observed_at="2026-09-19T00:00:01Z",
            priority=9, payload={"index": index},
        )

    assert asyncio.run(store.insert_raw_many([item(index, "wire-a") for index in range(10)])) == 3
    assert asyncio.run(store.insert_raw_many([item(index, "wire-b") for index in range(2)])) == 2
    with connect(store.path, settings) as con:
        counts = dict(con.execute("SELECT source_name,COUNT(*) FROM raw_news GROUP BY source_name").fetchall())
    assert counts == {"wire-a": 3, "wire-b": 2}


def test_paid_openai_semantics_cannot_be_enabled_by_server_environment(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_SEMANTICS_ENABLED", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    client = OpenAISemanticExtractor(settings)
    try:
        assert client.enabled is False
    finally:
        asyncio.run(client.close())


def test_stale_news_never_enters_story_or_learning_ledger(settings, monkeypatch):
    monkeypatch.setenv("NEWS_MAX_STORY_AGE_SECONDS", "3600")
    bounded = dataclasses.replace(settings, translation_enabled=False, news_enrichment_enabled=False)
    processor = NewsProcessor(bounded, Metrics("freshness-test", bounded.state_dir))
    now = datetime.now(timezone.utc)

    def item(key: str, published: datetime) -> RawNewsItem:
        return RawNewsItem(
            raw_key=key, source_key="official", source_name="Official",
            source_url="https://example.com/feed", item_url=f"https://example.com/{key}",
            title="Federal Reserve emergency interest rate cut after inflation surprise",
            summary="Official monetary policy decision changes the target rate.",
            category="MONETARY_POLICY", region="US", language="en",
            published_at=published.isoformat(), observed_at=now.isoformat(), priority=10,
            payload={"official": True},
        )

    async def run() -> None:
        assert await processor.news.insert_raw(item("stale", now - timedelta(hours=2)))
        stale = (await processor.news.lease_raw("test", 1, 60))[0]
        await processor._process_row(stale)
        with connect(processor.control.path, bounded) as con:
            assert con.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0

        assert await processor.news.insert_raw(item("fresh", now - timedelta(seconds=5)))
        fresh = (await processor.news.lease_raw("test", 1, 60))[0]
        await processor._process_row(fresh)
        with connect(processor.control.path, bounded) as con:
            assert con.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 1

    asyncio.run(run())
