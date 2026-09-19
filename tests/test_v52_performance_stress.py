from __future__ import annotations

import asyncio
import time

from v52.intelligence.service import IntelligenceService
from v52.metrics import Metrics
from v52.models import RawNewsItem
from v52.storage.news import NewsStore


def _raw(index: int) -> RawNewsItem:
    return RawNewsItem(
        raw_key=f"burst:{index}", source_key="stress", source_name="Stress", source_url="https://example.com/feed",
        item_url=f"https://example.com/{index}", title=f"Event {index}", summary="fixture", category="GENERAL",
        region="GLOBAL", language="en", published_at=None, observed_at="2026-08-25T00:00:00Z", priority=5,
        payload={"sequence": index},
    )


def test_news_burst_has_no_silent_cap_or_loss(settings) -> None:
    store = NewsStore(settings)
    items = [_raw(i) for i in range(5000)]
    started = time.perf_counter()
    inserted = store._insert_raw_many_sync(items)
    elapsed = time.perf_counter() - started
    assert inserted == 5000
    assert store._pending_count_sync() == 5000
    assert elapsed < 10.0


def test_paid_llm_path_is_disabled_and_raw_news_persistence_stays_fast(settings, monkeypatch) -> None:
    async def run() -> None:
        service = IntelligenceService(settings, Metrics("slow-llm-test", settings.state_dir))

        async def forbidden_extract(_story, _base):
            raise AssertionError("paid semantic extraction must never be called")

        monkeypatch.setattr(service.semantics, "extract", forbidden_extract)
        story = {"id": 1, "_change_id": 1, "_change_type": "INITIAL", "fingerprint": "slow", "title": "Federal Reserve cuts rates",
                 "summary": "fixture", "category": "ECONOMY", "materiality": 10, "source_name": "Official", "source_url": "https://example.com",
                 "published_at": "2026-08-25T00:00:00Z", "first_observed_at": "2026-08-25T00:00:01Z"}
        task = asyncio.create_task(service.process_story(story))
        before = time.perf_counter()
        assert await NewsStore(settings).insert_raw(_raw(99999))
        assert time.perf_counter() - before < 1.0
        await asyncio.wait_for(task, 5.0)
        await service.semantics.close()
    asyncio.run(run())
