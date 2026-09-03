from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any

try:
    from opencc import OpenCC
except ImportError:
    OpenCC = None  # type: ignore[assignment]

from ..config import Settings
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..net.rate import AsyncRateLimiter
from ..storage.control import ControlStore

logger = logging.getLogger(__name__)


class TranslationService:
    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.store = ControlStore(settings)
        self.owner = f"translation:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.http = ResilientHttpClient(settings, name="translation", concurrency=settings.translation_concurrency, per_host_limit=settings.translation_concurrency, retries=2, total_timeout=45.0, max_body_bytes=2_000_000)
        self.limiter = AsyncRateLimiter(settings.translation_min_gap_seconds)
        self.zh_normalizer = OpenCC("t2s") if OpenCC is not None else None

    async def health(self) -> bool:
        result = await self.http.get(f"{self.settings.libretranslate_url}/languages", retries=0, max_body_bytes=200_000)
        return result.ok

    async def _translate_text(self, text: str) -> tuple[str, tuple[str, str, str] | None]:
        text = text.strip()
        if not text:
            return "", None
        h = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
        cached = await self.store.translation_cache_get(h)
        if cached is not None:
            self.metrics.inc("translation_cache_hit")
            return cached, None
        await self.limiter.wait()
        result = await self.http.post(
            f"{self.settings.libretranslate_url}/translate",
            json_body={"q": text, "source": "auto", "target": "en", "format": "text"},
            max_body_bytes=2_000_000,
        )
        if not result.ok:
            raise RuntimeError(f"LibreTranslate {result.classification} status={result.status} error={result.error}")
        try:
            payload = json.loads(result.body)
            translated = str(payload.get("translatedText") or "").strip()
        except Exception as exc:
            raise RuntimeError(f"invalid LibreTranslate JSON: {exc}") from exc
        if not translated:
            raise RuntimeError("LibreTranslate returned empty translatedText")
        self.metrics.inc("translation_http_call")
        return translated, (h, text, translated)

    async def _job(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        story_id = int(job["story_id"])
        story = await self.store.get_story(story_id)
        if not story:
            await self.store.retry_job("translation_jobs", job_id, "story_missing", 0.0, dead=True)
            self.metrics.inc("translation_story_missing")
            return
        cache_rows: list[tuple[str, str, str]] = []
        original_title = str(story.get("original_title") or "")
        original_summary = str(story.get("original_summary") or "")
        if str(story.get("language") or "").lower().startswith("zh") and self.zh_normalizer is not None:
            original_title = self.zh_normalizer.convert(original_title)
            original_summary = self.zh_normalizer.convert(original_summary)
        title, c1 = await self._translate_text(original_title)
        if c1:
            cache_rows.append(c1)
        summary, c2 = await self._translate_text(original_summary)
        if c2:
            cache_rows.append(c2)
        await self.store.complete_translation(job_id, story_id, title or str(story.get("original_title") or ""), summary or str(story.get("original_summary") or ""), cache_rows)
        self.metrics.inc("translation_completed")

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.translation_enabled:
            logger.info("translation disabled")
            await self.http.close()
            await stop_event.wait()
            return
        if self.settings.translation_require_healthy_on_start and not await self.health():
            raise RuntimeError("LibreTranslate health check failed and TRANSLATION_REQUIRE_HEALTHY_ON_START=1")
        sem = asyncio.Semaphore(self.settings.translation_concurrency)
        active: set[asyncio.Task[Any]] = set()

        async def wrapped(job: dict[str, Any]) -> None:
            async with sem:
                try:
                    await self._job(job)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    attempts = int(job.get("attempts") or 1)
                    dead = attempts >= self.settings.translation_max_attempts
                    delay = min(3600.0, 2.0 * (2 ** min(10, attempts)))
                    await self.store.retry_job("translation_jobs", int(job["id"]), f"{type(exc).__name__}: {exc}", delay, dead)
                    self.metrics.inc("translation_failure")
                    logger.warning("translation failed job=%s attempts=%s dead=%s error=%s", job.get("id"), attempts, dead, exc)

        try:
            while not stop_event.is_set():
                active = {t for t in active if not t.done()}
                capacity = max(0, self.settings.translation_concurrency - len(active))
                if capacity:
                    jobs = await self.store.lease_translations(self.owner, min(self.settings.translation_batch, capacity), self.settings.translation_lease_seconds)
                    for job in jobs:
                        active.add(asyncio.create_task(wrapped(job), name=f"translation-job-{job['id']}"))
                counts = await self.store.queue_counts()
                self.metrics.set("translation_pending", counts.get("translation_jobs_pending", 0) + counts.get("translation_jobs_leased", 0))
                if not active:
                    await asyncio.sleep(0.5)
                else:
                    await asyncio.sleep(0.05)
        finally:
            if active:
                try:
                    await asyncio.wait_for(asyncio.gather(*active, return_exceptions=True), timeout=20.0)
                except asyncio.TimeoutError:
                    for task in active:
                        task.cancel()
            await self.http.close()
