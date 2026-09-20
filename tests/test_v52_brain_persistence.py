from __future__ import annotations

import asyncio
import time

from v52.intelligence.service import IntelligenceService
from v52.intelligence.store import BrainStore
from v52.intelligence.types import EventUnderstanding
from v52.metrics import Metrics
from v52.models import StoryCandidate
from v52.storage.control import ControlStore
from v52.storage.sqlite import connect


def _candidate(title: str, summary: str, raw_id: int = 1) -> StoryCandidate:
    return StoryCandidate(
        fingerprint="same-url-fingerprint",
        title=title,
        summary=summary,
        canonical_url="https://example.com/fed-decision",
        category="MONETARY_POLICY",
        region="US",
        language="en",
        published_at="2026-08-25T10:00:00Z",
        observed_at="2026-08-25T10:00:01Z",
        materiality=9,
        reasons=["official"],
        source_name="Federal Reserve Official",
        source_url="https://federalreserve.gov",
        raw_id=raw_id,
    )


def test_story_revision_feed_distinguishes_duplicate_and_development(settings) -> None:
    async def run() -> None:
        store = ControlStore(settings)
        first = _candidate("Federal Reserve cuts interest rate", "Initial statement", 1)
        second = _candidate("Federal Reserve cuts interest rate", "Confirmed update with implementation details", 2)
        sid, created = await store.record_story(first, first.canonical_url, False, None, 0, None, False)
        assert created
        _sid, created_again = await store.record_story(first, first.canonical_url, False, None, 0, None, False)
        assert not created_again
        sid2, new_story = await store.record_story(second, second.canonical_url, False, None, 0, None, False)
        assert sid2 == sid and not new_story
        changes = await store.story_changes_after(0, 10)
        assert len(changes) == 2
        assert [x["_change_type"] for x in changes] == ["INITIAL", "FOLLOW_UP"]
    asyncio.run(run())


def test_intelligence_memory_is_idempotent_and_restart_persistent(settings, monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setenv("OPENAI_SEMANTICS_ENABLED", "0")
        control = ControlStore(settings)
        candidate = _candidate("Federal Reserve cuts interest rate", "Policy decision affects yields and gold")
        await control.record_story(candidate, candidate.canonical_url, False, None, 0, None, False)
        change = (await control.story_changes_after(0, 10))[0]
        service = IntelligenceService(settings, Metrics("intelligence-test", settings.state_dir))
        try:
            event_id, signals = await service.process_story(change)
            assert event_id > 0 and signals
            event_id2, _signals2 = await service.process_story(change)
            assert event_id2 == event_id
        finally:
            await service.semantics.close()
        first = BrainStore(settings)._summary_sync()
        restarted = BrainStore(settings)._summary_sync()
        assert first == restarted
        assert first["counts"]["events"] == 1
        assert first["counts"]["event_updates"] == 1
        assert first["counts"]["signals"] > 0
        assert await BrainStore(settings).mode() == "OBSERVATION"
    asyncio.run(run())


def test_duplicate_update_key_from_new_change_preserves_lineage_without_pinning_cursor(settings) -> None:
    async def run() -> None:
        brain = BrainStore(settings)
        understanding = EventUnderstanding(
            event_type="CENTRAL_BANK_DECISION",
            domain="MACRO",
            event_state="CONFIRMED",
            cluster_key="fed-decision-cluster",
            update_key="fed-decision-identical-update",
            title="Federal Reserve policy decision",
            summary="Official policy update",
            occurred_at="2026-09-20T12:00:00Z",
            observed_at="2026-09-20T12:00:01Z",
            entities=(),
            assets=(),
            relations=(),
            importance=0.9,
            novelty=0.8,
            surprise=0.7,
            source_credibility=1.0,
            priority="P0",
            expected_horizons=(),
            rationale=("official",),
        )
        first = await brain.ingest_event(
            understanding,
            {"id": 1, "_change_id": 100, "source_name": "Official A", "source_url": "https://a.example"},
        )
        duplicate = await brain.ingest_event(
            understanding,
            {"id": 2, "_change_id": 101, "source_name": "Official B", "source_url": "https://b.example"},
        )
        replay = await brain.ingest_event(
            understanding,
            {"id": 2, "_change_id": 101, "source_name": "Official B", "source_url": "https://b.example"},
        )
        assert first[1:] == (True, False)
        assert duplicate == replay == (first[0], False, False)
        with connect(brain.path, settings) as con:
            assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
            assert con.execute("SELECT COUNT(*) FROM event_updates").fetchone()[0] == 1
            assert con.execute("SELECT COUNT(*) FROM event_source_lineage").fetchone()[0] == 2
            assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='EVENT_UPDATE_DEDUPLICATED'").fetchone()[0] == 1

    asyncio.run(run())


def test_48_hour_blackout_survives_restart(settings) -> None:
    async def run() -> None:
        brain = BrainStore(settings)
        from v52.intelligence.warmup import RuntimeWarmup
        warmup = RuntimeWarmup(settings, max_tick_gap=200000)
        await brain.set_meta("signals_enabled", "1")
        await brain.set_meta("last_runtime_tick", "1000")
        await warmup.tick(1000 + 86399)
        assert await BrainStore(settings).mode() == "OBSERVATION"
        await warmup.tick(1000 + 86401)
        assert await BrainStore(settings).mode() == "SHADOW"
        await warmup.tick(1000 + 172802)
        assert await BrainStore(settings).mode() == "LIVE"
    asyncio.run(run())
