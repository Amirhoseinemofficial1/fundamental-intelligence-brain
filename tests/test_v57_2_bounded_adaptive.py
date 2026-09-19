import asyncio
import dataclasses
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from v52.intelligence.neural_bandit import HIDDEN_DIM, INPUT_DIM, NeuralBandit, vector
from v52.intelligence.maintenance import MaintenanceService
from v52.intelligence.openai_semantics import OpenAISemanticExtractor
from v52.intelligence.store import BrainStore
from v52.intelligence.telegram_producers import ADAPTIVE_INTELLIGENCE_KINDS, TelegramIntelligenceProducers
from v52.market.writer import RawWireJournal
from v52.metrics import Metrics
from v52.models import MarketEvent
from v52.models import RawNewsItem
from v52.storage.control import ControlStore
from v52.storage.market import MarketSemanticStore
from v52.storage.news import NewsStore
from v52.storage.sqlite import connect
from v52.storage.structured import StructuredStore


def test_structured_state_is_bucketed_deduplicated_and_bounded(settings, monkeypatch):
    monkeypatch.setenv("STRUCTURED_STATE_BUCKET_SECONDS", "300")
    monkeypatch.setenv("STRUCTURED_MAX_PAYLOAD_BYTES", "16384")
    store = StructuredStore(settings)
    payloads = [
        {"symbol": "XAUUSD", "value": index, "rows": ["x" * 1000] * 100}
        for index in range(100)
    ]
    assert asyncio.run(store.insert_snapshots("official", "macro", "STATE", payloads)) == 1
    with connect(store.path, settings) as con:
        row = con.execute("SELECT payload_json,payload_hash,entity_key,time_bucket FROM snapshots").fetchone()
        assert con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    compact = json.loads(row["payload_json"])
    assert compact["_compacted"] is True
    assert compact["original_bytes"] > 16_384
    assert len(row["payload_json"].encode("utf-8")) <= 16_384


def test_market_semantics_keep_latest_state_per_bucket(settings, monkeypatch):
    monkeypatch.setenv("MARKET_SEMANTIC_BUCKET_SECONDS", "60")
    store = MarketSemanticStore(settings, "market_test")
    rows = [
        MarketEvent("BOOK", "TEST", "BTCUSDT", str(index), "2026-01-01T00:00:00Z", "market", {"mid": index})
        for index in range(250)
    ]
    assert store.insert_many(rows) == 250
    with connect(store.path, settings) as con:
        saved = con.execute("SELECT payload_json FROM events").fetchall()
    assert len(saved) == 1
    assert json.loads(saved[0]["payload_json"])["mid"] == 249


def test_news_transport_payload_is_bounded_but_evidence_fields_survive(settings, monkeypatch):
    monkeypatch.setenv("NEWS_MAX_PAYLOAD_BYTES", "8192")
    store = NewsStore(settings)
    item = RawNewsItem(
        raw_key="unique-story",
        source_key="wire",
        source_name="Wire",
        source_url="https://example.com/feed",
        item_url="https://example.com/story",
        title="Material central-bank decision",
        summary="Policy evidence retained outside the transport envelope",
        category="MACRO",
        region="GLOBAL",
        language="en",
        published_at="2026-01-01T00:00:00Z",
        observed_at="2026-01-01T00:00:01Z",
        priority=9,
        payload={"documents": ["x" * 1000] * 100},
    )
    assert asyncio.run(store.insert_raw(item)) is True
    assert asyncio.run(store.insert_raw(item)) is False
    with connect(store.path, settings) as con:
        initial = con.execute("SELECT payload_json FROM raw_news").fetchone()
    assert json.loads(initial["payload_json"])["_compacted"] is True
    assert len(initial["payload_json"].encode("utf-8")) <= 8192
    asyncio.run(store.ack_raw(1))
    with connect(store.path, settings) as con:
        row = con.execute("SELECT title,summary,item_url,payload_json FROM raw_news").fetchone()
    assert row["title"] == item.title and row["summary"] == item.summary and row["item_url"] == item.item_url
    assert json.loads(row["payload_json"])["_released_after_processing"] is True
    assert len(row["payload_json"].encode("utf-8")) <= 8192


def test_news_durable_queue_has_one_atomic_cap_for_all_collectors(settings):
    limited = dataclasses.replace(settings, news_raw_backpressure_rows=2)
    store = NewsStore(limited)

    def item(index: int) -> RawNewsItem:
        return RawNewsItem(
            raw_key=f"story-{index}",
            source_key="wire",
            source_name="Wire",
            source_url="https://example.com/feed",
            item_url=f"https://example.com/story/{index}",
            title=f"Material event {index}",
            summary="Evidence",
            category="MACRO",
            region="GLOBAL",
            language="en",
            published_at="2026-01-01T00:00:00Z",
            observed_at="2026-01-01T00:00:01Z",
            priority=9,
            payload={"index": index},
        )

    assert asyncio.run(store.insert_raw_many([item(index) for index in range(5)])) == 2
    assert asyncio.run(store.pending_count()) == 2
    asyncio.run(store.ack_raw(1))
    assert asyncio.run(store.insert_raw_many([item(5), item(6)])) == 1
    assert asyncio.run(store.pending_count()) == 2


def test_maintenance_prunes_transport_copies_and_old_reference_prices(settings, monkeypatch):
    monkeypatch.setenv("OPERATIONAL_DB_RETENTION_DAYS", "1")
    monkeypatch.setenv("REFERENCE_PRICE_RETENTION_DAYS", "3")
    service = MaintenanceService(settings, Metrics("maintenance-test", settings.state_dir))
    now = time.time()
    old = now - 10 * 86400

    with connect(service.telegram.control.path, settings) as con:
        values = (
            "test", "old-story", "Old", "Old", "https://example.com/old", "MACRO",
            "GLOBAL", "en", None, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
            5, "[]", "Wire", "https://example.com/feed", "Old", "Old", old, old,
        )
        story_id = con.execute(
            "INSERT INTO stories(run_id,fingerprint,title,summary,canonical_url,category,region,language,"
            "published_at,first_observed_at,last_observed_at,materiality,reasons_json,source_name,source_url,"
            "original_title,original_summary,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        ).lastrowid
        con.execute(
            "INSERT INTO occurrences(story_id,raw_id,source_name,source_url,item_url,observed_at) "
            "VALUES(?,1,'Wire','https://example.com/feed','https://example.com/old','2026-01-01T00:00:00Z')",
            (story_id,),
        )
    with connect(service.brain.path, settings) as con:
        con.execute(
            "INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) "
            "VALUES('fixture','OLD',?,?,1,NULL,.9,'{}')",
            (old, old),
        )
        con.execute(
            "INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) "
            "VALUES('fixture','NEW',?,?,1,NULL,.9,'{}')",
            (now, now),
        )

    counts = service._prune_databases_sync()
    assert counts["stories"] == 1
    assert counts["reference_prices"] == 1
    with connect(service.telegram.control.path, settings) as con:
        assert con.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] == 0
    with connect(service.brain.path, settings) as con:
        assert [row[0] for row in con.execute("SELECT asset_id FROM reference_prices")] == ["NEW"]


def test_raw_wire_can_be_disabled_without_creating_segments(settings, monkeypatch):
    monkeypatch.setenv("MARKET_RAW_ENABLED", "0")

    async def run():
        metrics = Metrics("raw-disabled", settings.state_dir)
        journal = RawWireJournal(settings, metrics, "market_test")
        await journal.start()
        await journal.append("TEST", "trades", "BTCUSDT", '{"price":1}')
        await journal.stop()
        return journal

    journal = asyncio.run(run())
    assert journal.enabled is False
    assert list(journal.dir.glob("wire-*.jsonl")) == []


def test_semantic_inputs_reach_connected_adaptive_network():
    values = {"event_importance": 0.8, "source_quality": 0.9, "semantic_available": 1.0}
    values.update({f"semantic_{index:02d}": (index - 31.5) / 64 for index in range(64)})
    x = vector(values, 1, 3600)
    model = NeuralBandit()
    before = model.forward(x)[1][4]
    for _ in range(200):
        model.update(x, 4, 0.75)
    after = model.forward(x)[1][4]
    assert len(x) == INPUT_DIM == 76
    assert len(model.state["hidden"]) == HIDDEN_DIM == 32
    assert abs(after - 0.75) < abs(before - 0.75)


def test_shared_brain_schema_initialization_is_serialized(settings):
    def initialize(_):
        return BrainStore(settings).path

    with ThreadPoolExecutor(max_workers=12) as pool:
        paths = list(pool.map(initialize, range(36)))
    assert len(set(paths)) == 1
    assert not paths[0].with_name(paths[0].name + ".init.lock").exists()
    with connect(paths[0], settings) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] >= 1


def test_adaptive_learning_report_remains_typed_and_renderable(settings):
    async def run():
        producer = TelegramIntelligenceProducers(ControlStore(settings))
        return await producer.produce({
            "learning_report_ready": True,
            "event_id": "learning:100",
            "asset_id": "MULTI_ASSET",
            "summary": "evidence based adaptive update",
        })

    produced = asyncio.run(run())
    assert ADAPTIVE_INTELLIGENCE_KINDS == ("adaptive_learning_report",)
    assert produced and produced[0]["rendered"]


def test_openai_failure_cooldown_suppresses_embedding_amplification(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_SEMANTICS_ENABLED", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")

    async def run():
        client = OpenAISemanticExtractor(settings)
        client._failure_cooldown_until = __import__("time").time() + 60
        try:
            result = await client.embed({"title": "material news"}, type("D", (), {"event_type": "GENERAL_EVENT", "domain": "GENERAL", "assets": ()})())
            assert result is None
        finally:
            await client.close()

    asyncio.run(run())


def test_local_semantic_embedding_is_stable_normalized_and_text_sensitive(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_EMBEDDING_DIMENSIONS", "64")

    async def run():
        client = OpenAISemanticExtractor(settings)
        deterministic = type("D", (), {"event_type": "CENTRAL_BANK_DECISION", "domain": "MACRO", "assets": ({"asset_id": "XAUUSD"},)})()
        try:
            first = client.local_embed({"title": "Federal Reserve cuts rates", "summary": "lower real yields support gold"}, deterministic)
            repeated = client.local_embed({"title": "Federal Reserve cuts rates", "summary": "lower real yields support gold"}, deterministic)
            different = client.local_embed({"title": "Oil inventories surge", "summary": "crude supply exceeds demand"}, deterministic)
        finally:
            await client.close()
        return first, repeated, different

    first, repeated, different = asyncio.run(run())
    assert first == repeated
    assert first[0] != different[0]
    assert len(first[1]) == 64
    assert sum(value * value for value in first[1]) == pytest.approx(1.0)
    assert first[1] != different[1]
