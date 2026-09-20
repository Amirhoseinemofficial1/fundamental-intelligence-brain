from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

from ..config import Settings
from ..metrics import Metrics
from ..storage.control import ControlStore
from ..storage.news import NewsStore
from ..timeutil import age_seconds, parse_datetime
from .relevance import candidate_from_raw, is_fundamental

logger = logging.getLogger(__name__)


class NewsProcessor:
    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.news = NewsStore(settings)
        self.control = ControlStore(settings)
        self.owner = f"news-processor:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.durable_materiality_min = max(3, min(10, int(os.getenv("NEWS_DURABLE_MATERIALITY_MIN", "4"))))
        self.max_story_age_seconds = max(60, int(os.getenv("NEWS_MAX_STORY_AGE_SECONDS", "3600")))
        self.untimed_materiality_min = max(
            self.durable_materiality_min,
            min(10, int(os.getenv("NEWS_UNTIMED_MATERIALITY_MIN", "7"))),
        )

    def _alert_payload(self, c, age: float | None) -> dict[str, Any]:
        return {
            "kind": "news",
            "story_fingerprint": c.fingerprint,
            "title": c.title,
            "summary": c.summary[:1500],
            "source": c.source_name,
            "url": c.canonical_url,
            "category": c.category,
            "region": c.region,
            "published_at": c.published_at,
            "observed_at": c.observed_at,
            "detection_age_seconds": age,
            "materiality": c.materiality,
            "reasons": c.reasons,
        }

    async def _process_row(self, row: dict[str, Any]) -> None:
        payload = json.loads(row.get("payload_json") or "{}")
        official = bool(payload.get("official"))
        candidate, lang = candidate_from_raw(row, official=official)
        if not is_fundamental(candidate.materiality, candidate.title, candidate.category, candidate.summary, official=official and bool(candidate.published_at)):
            self.metrics.inc("news_irrelevant")
            await self.news.ack_raw(int(row["id"]))
            return
        pub = parse_datetime(candidate.published_at)
        obs = parse_datetime(candidate.observed_at)
        age = age_seconds(pub, obs) if pub else None
        if pub is not None and obs is not None and (obs - pub).total_seconds() < -30:
            self.metrics.inc("news_future_timestamp_filtered")
            await self.news.ack_raw(int(row["id"]))
            return
        if candidate.materiality < self.durable_materiality_min:
            self.metrics.inc("news_below_durable_materiality")
            await self.news.ack_raw(int(row["id"]))
            return
        if pub is None and candidate.materiality < self.untimed_materiality_min:
            self.metrics.inc("news_untimed_filtered")
            await self.news.ack_raw(int(row["id"]))
            return
        if age is not None and age > self.max_story_age_seconds:
            self.metrics.inc("news_stale_filtered")
            await self.news.ack_raw(int(row["id"]))
            return
        alert_eligible = (
            pub is not None
            and age is not None
            and age <= self.settings.news_alert_max_age_seconds
            and candidate.materiality >= self.settings.news_materiality_threshold
        )
        if pub is None:
            self.metrics.inc("news_untimed_archived")
        elif age is not None:
            self.metrics.inc("news_timed")
            if age <= 5:
                self.metrics.inc("news_age_le_5")
            if age <= 15:
                self.metrics.inc("news_age_le_15")
            if age <= 60:
                self.metrics.inc("news_age_le_60")
            if age <= self.settings.news_alert_max_age_seconds:
                self.metrics.inc("news_live_window")
        translation_needed = self.settings.translation_enabled and lang != "en"
        # V52 never sends raw news to Telegram. Intelligence consumes the durable story
        # ledger independently and may later emit a qualified, evidence-backed signal.
        alert_payload = None
        expires = None
        story_id, is_new = await self.control.record_story(
            candidate,
            str(row.get("item_url") or ""),
            translation_needed,
            alert_payload,
            candidate.materiality,
            expires,
            self.settings.news_enrichment_enabled and alert_eligible,
        )
        self.metrics.inc("story_new" if is_new else "story_duplicate")
        if is_new and candidate.materiality >= self.settings.news_materiality_threshold:
            self.metrics.inc("story_material_new")
        if is_new and translation_needed:
            self.metrics.inc("translation_enqueued")
        await self.news.ack_raw(int(row["id"]))

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info("news processor started owner=%s", self.owner)
        while not stop_event.is_set():
            rows = await self.news.lease_raw(self.owner, self.settings.news_raw_batch, self.settings.news_raw_lease_seconds)
            if not rows:
                await asyncio.sleep(0.15)
                continue
            for row in rows:
                try:
                    await self._process_row(row)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.metrics.inc("news_processing_error")
                    logger.exception("news row processing failed raw_id=%s", row.get("id"))
                    attempts = int(row.get("attempts") or 1)
                    delay = min(300.0, 2 ** min(8, attempts))
                    await self.news.retry_raw(int(row["id"]), f"{type(exc).__name__}: {exc}", delay)
            self.metrics.set("raw_pending", await self.news.pending_count())
            self.metrics.set("control_queues", await self.control.queue_counts())
        logger.info("news processor stopping; leased rows remain durable and will be reclaimed after lease expiry")
