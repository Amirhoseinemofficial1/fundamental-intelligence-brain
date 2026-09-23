from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from v52.intelligence.service import IntelligenceService, FEATURE_ONLY_OBSERVATION_TYPES, live_news_freshness_ok
from v52.intelligence.features import FeatureStore, evidence_quality
from v52.intelligence.observation import CanonicalObservation, ObservationBridge
from v52.intelligence.store import BrainStore
from v52.metrics import Metrics
from v52.models import RawNewsItem
from v52.net.http import HttpResult
from v52.news.collector import AdaptiveNewsCollector, RuntimeSource
from v52.news.processor import NewsProcessor
from v52.news.relevance import admission_eligible, fundamental_relevant
from v52.news.registry import SourceRegistry
from v52.storage.sqlite import connect
from v52.structured.prediction import prediction_market_relevant
from scripts.news_sla_report import report


def test_topic_words_and_generic_categories_do_not_create_learning_events() -> None:
    for category, title in (
        ("CRYPTO", "Bitcoin prices today: what to know"),
        ("BUSINESS", "Bank shares and market commentary"),
        ("ECONOMY", "Car loan rates start at 7.35%: compare offers"),
        ("WORLD", "Iran executes man convicted of spying"),
        ("SANCTIONS", "No right to impose tariffs: envoy debates trade policy"),
        ("ECONOMY", "Week ahead: Fed signals and rate hike expectations"),
        ("ENERGY", "Refinery IPO: building the shareholder community"),
    ):
        assert not fundamental_relevant(category, title)
    for category, title in (
        ("MONETARY_POLICY", "Federal Reserve announces emergency interest rate cut"),
        ("CRYPTO", "SEC approves spot Bitcoin ETF"),
        ("ENERGY", "OPEC cuts oil production by one million barrels"),
        ("BUSINESS", "Company raises earnings guidance after quarterly results"),
    ):
        assert fundamental_relevant(category, title)


def test_admission_rejects_stale_future_and_untimed_noise() -> None:
    now = datetime.now(timezone.utc)

    def item(published: datetime | None) -> RawNewsItem:
        return RawNewsItem(
            raw_key="key", source_key="official", source_name="Official",
            source_url="https://federalreserve.gov/feed", item_url="https://federalreserve.gov/release",
            title="Federal Reserve announces emergency interest rate cut",
            summary="", category="MONETARY_POLICY", region="US", language="en",
            published_at=published.isoformat() if published else None,
            observed_at=now.isoformat(), priority=10, payload={"official": True},
        )

    assert admission_eligible(item(now - timedelta(seconds=10)), official=True)
    assert not admission_eligible(item(now - timedelta(hours=2)), official=True)
    assert not admission_eligible(item(now + timedelta(minutes=5)), official=True)
    generic = dataclasses.replace(item(None), title="8-K - Example Holdings (Filer)", summary="Company discusses a possible merger")
    assert not admission_eligible(generic, official=True)


def test_aggregator_or_stale_news_cannot_send_live_signal() -> None:
    now = datetime.now(timezone.utc)
    fresh = {
        "published_at": (now - timedelta(seconds=12)).isoformat(),
        "first_observed_at": (now - timedelta(seconds=4)).isoformat(),
        "source_url": "https://www.federalreserve.gov/feeds/press_all.xml",
    }
    assert live_news_freshness_ok(fresh, now=now.timestamp())
    assert not live_news_freshness_ok({**fresh, "source_url": "https://news.google.com/rss"}, now=now.timestamp())
    assert not live_news_freshness_ok({**fresh, "source_url": "https://api.gdeltproject.org/api/v2/doc/doc"}, now=now.timestamp())
    assert not live_news_freshness_ok({**fresh, "published_at": (now - timedelta(seconds=61)).isoformat()}, now=now.timestamp())
    assert not live_news_freshness_ok({**fresh, "published_at": None}, now=now.timestamp())
    assert live_news_freshness_ok({"_structured_observation": True}, now=now.timestamp())


def test_processor_does_not_persist_generic_crypto_headline(settings) -> None:
    bounded = dataclasses.replace(settings, translation_enabled=False, news_enrichment_enabled=False)
    processor = NewsProcessor(bounded, Metrics("quality-test", bounded.state_dir))
    now = datetime.now(timezone.utc)
    raw = RawNewsItem(
        raw_key="generic-bitcoin", source_key="google", source_name="Google",
        source_url="https://news.google.com/rss", item_url="https://example.com/generic-bitcoin",
        title="Bitcoin prices today: what to know", summary="Crypto market overview",
        category="CRYPTO", region="US", language="en",
        published_at=now.isoformat(), observed_at=now.isoformat(), priority=5,
        payload={"official": False},
    )

    async def run() -> None:
        assert await processor.news.insert_raw(raw)
        row = (await processor.news.lease_raw("quality-test", 1, 60))[0]
        await processor._process_row(row)

    asyncio.run(run())
    with connect(processor.control.path, bounded) as con:
        assert con.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0


def test_latency_report_excludes_social_and_structured_events(tmp_path) -> None:
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    now = datetime.now(timezone.utc)
    with sqlite3.connect(db_dir / "control.sqlite3") as con:
        con.execute("CREATE TABLE stories(published_at TEXT,first_observed_at TEXT,created_at REAL,source_url TEXT,source_name TEXT,materiality INTEGER)")
        rows = (
            ("https://www.federalreserve.gov/feeds/press_all.xml", "Federal Reserve Press", 5),
            ("https://api.gdeltproject.org/api/v2/doc/doc", "GDELT Mirror", 5),
            ("wss://jetstream.us-east.bsky.network/xrpc", "Bluesky Jetstream", 5),
            ("https://api.exchange.example/ticker", "BINANCE_FUTURES", 8),
        )
        for url, name, score in rows:
            con.execute("INSERT INTO stories VALUES(?,?,?,?,?,?)", (
                (now - timedelta(seconds=8)).isoformat(), now.isoformat(), time.time(), url, name, score,
            ))
    with sqlite3.connect(db_dir / "news.sqlite3") as con:
        con.execute("CREATE TABLE raw_news(state TEXT,ingested_at REAL)")
        con.execute("CREATE TABLE source_health(name TEXT,consecutive_failures INTEGER,last_status TEXT,last_seen_at REAL)")
    result = report(tmp_path, 1)
    assert result["all_control_stories"] == 4
    assert result["news_stories"] == 2
    assert result["social_or_structured_events_excluded"] == 2
    assert result["direct_source"]["count"] == 1
    assert result["aggregator"]["count"] == 1
    assert result["direct_source_24h_p95_target_met"] is False


def test_gdelt_mirror_disabled_in_direct_registry(settings) -> None:
    registry = SourceRegistry(dataclasses.replace(settings, gdelt_enabled=False))
    assert all("gdelt" not in (source.name + source.url).lower()
               for source in registry.direct_sources() + registry.priority_sources())


def test_failed_source_has_backoff_beyond_normal_hot_poll_cap(settings) -> None:
    async def run() -> None:
        collector = AdaptiveNewsCollector(settings, Metrics("quality-backoff", settings.state_dir), "hot")
        try:
            source = next(iter(collector.sources.values())).source
            runtime = RuntimeSource(source, 30.0, 0.0, failures=3)
            blocked = HttpResult(False, source.url, 403, b"", {}, 1.0, "blocked_or_auth")
            open_circuit = HttpResult(False, source.url, None, b"", {}, 1.0, "circuit_open")
            throttled = HttpResult(False, source.url, 429, b"", {}, 1.0, "rate_limited", retry_after=900.0)
            assert collector._next_interval(runtime, blocked, 0) >= 3600
            assert collector._next_interval(runtime, open_circuit, 0) >= 3600
            assert collector._next_interval(runtime, throttled, 0) >= 900
        finally:
            await collector.http.close()

    asyncio.run(run())


def test_prediction_market_topic_gate() -> None:
    assert prediction_market_relevant("Will the Fed cut interest rates in December?")
    assert prediction_market_relevant("Will Bitcoin spot ETF receive SEC approval?")
    assert prediction_market_relevant("Will US vs China tariffs rise?")
    assert not prediction_market_relevant("LSU vs Ole Miss football winner")
    assert not prediction_market_relevant("Who wins the Academy Awards?")
    assert not prediction_market_relevant("Weather in New York next week")
    assert "PREDICTION_MARKET_STATE" in FEATURE_ONLY_OBSERVATION_TYPES
    assert "SEC_CURRENT_FILING" in FEATURE_ONLY_OBSERVATION_TYPES


def test_generic_prediction_snapshot_cannot_raise_feature_quality(settings) -> None:
    BrainStore(settings)
    ObservationBridge(settings).persist_sync(CanonicalObservation(
        "generic-market", 100, 101, 102, "kalshi", .9, (), (),
        "PREDICTION_MARKET_STATE", 1000000, "USD", "NEUTRAL", .9,
        1, .9, "structured:1:0", {},
    ))
    feature = FeatureStore(settings).build_sync(
        {"id": None, "importance": .8, "novelty": .5, "source_credibility": .8},
        {"asset_id": "BTC", "mapping_confidence": .8},
        300, prediction_time=103,
    )
    assert feature["provenance"] == []
    assert feature["quality"] == evidence_quality({"source_credibility": .8}, {"mapping_confidence": .8}, [])


def test_direct_event_quality_is_not_capped_by_missing_optional_modalities() -> None:
    assert evidence_quality({'source_credibility':.9},{'mapping_confidence':.9},[]) == .81
    assert evidence_quality({'source_credibility':.5},{'mapping_confidence':.8},[]) == .4


def test_generic_prediction_snapshots_stay_in_source_store(settings) -> None:
    BrainStore(settings)
    bridge = ObservationBridge(settings)

    async def run() -> int:
        inserted = await bridge.structured.insert_snapshots(
            "kalshi", "PREDICTION_EVENT_MARKETS", "PREDICTION_MARKET_STATE",
            [{"title": "Will the Fed cut rates?", "volume_fp": "1000"}],
        )
        assert inserted == 1
        positioned = await bridge.structured.insert_snapshots(
            "cftc", "CFTC_POSITIONING", "POSITIONING_CIT",
            [{"contract_market_name": "Crude Oil", "open_interest_all": "50000"}],
        )
        assert positioned == 1
        ftd = await bridge.structured.insert_snapshots(
            "sec_ftd", "SETTLEMENT_PLUMBING_FTD", "FTD_RECORD",
            [{"cusip": "000000001", "quantity": "10000"}],
        )
        assert ftd == 1
        cursor, observations = await bridge.drain(0, 10)
        assert cursor > 0 and observations == []
        return cursor

    assert asyncio.run(run()) > 0
    with connect(bridge.path, settings) as con:
        assert con.execute("SELECT COUNT(*) FROM canonical_observations").fetchone()[0] == 0


def test_repeated_structured_event_does_not_repeat_prediction_fanout(settings) -> None:
    service = IntelligenceService(settings, Metrics("quality-brain", settings.state_dir))
    now = datetime.now(timezone.utc).isoformat()
    first = {
        "id": -101, "_change_id": 101, "fingerprint": "funding-one",
        "title": "Bitcoin futures funding spike", "summary": "BTC funding extreme",
        "category": "FUNDING_EXTREME", "materiality": 8,
        "source_name": "market", "source_url": "market://btc/funding",
        "published_at": now, "first_observed_at": now,
        "_structured_observation": True,
    }

    async def run() -> tuple[int, int]:
        event_id, _ = await service.process_story(first)
        second = {**first, "id": -102, "_change_id": 102, "fingerprint": "funding-two"}
        second_event_id, second_signals = await service.process_story(second)
        assert second_event_id == event_id
        assert second_signals == []
        return event_id, second_event_id

    asyncio.run(run())
    with connect(service.brain.path, settings) as con:
        assert con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM event_updates").fetchone()[0] == 2
