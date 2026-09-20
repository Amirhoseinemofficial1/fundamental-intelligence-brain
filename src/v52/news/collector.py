from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
import time
from urllib.parse import urlsplit
from typing import Any

from ..config import Settings
from ..metrics import Metrics
from ..models import NewsSource
from ..net.http import HttpResult, ResilientHttpClient
from ..storage.news import NewsStore
from .parse import parse_feed, parse_priority_page
from .relevance import admission_eligible
from .registry import SourceRegistry

logger = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True)
class RuntimeSource:
    source: NewsSource
    interval: float
    next_due: float
    failures: int = 0
    polls: int = 0
    new_items: int = 0
    in_flight: bool = False


class AdaptiveNewsCollector:
    def __init__(self, settings: Settings, metrics: Metrics, role: str):
        if role not in {"hot", "coverage"}:
            raise ValueError(role)
        self.settings = settings
        self.metrics = metrics
        self.role = role
        self.registry = SourceRegistry(settings)
        self.store = NewsStore(settings)
        base = self.registry.hot_sources() if role == "hot" else self.registry.coverage_sources()
        now = time.monotonic()
        self.sources: dict[str, RuntimeSource] = {}
        for idx, src in enumerate(base):
            minimum, maximum = self._bounds(src)
            interval = min(maximum, max(minimum, src.base_poll_seconds))
            self.sources[src.key] = RuntimeSource(src, interval, now + (idx % 20) * 0.05 + random.random() * min(2.0, interval))
        concurrency = settings.news_hot_concurrency if role == "hot" else settings.news_coverage_concurrency
        self.queue: asyncio.Queue[RuntimeSource] = asyncio.Queue(maxsize=max(100, len(self.sources) * 2))
        self.http = ResilientHttpClient(
            settings,
            name=f"news-{role}",
            concurrency=concurrency,
            per_host_limit=2 if role == "coverage" else 4,
            retries=2,
        )
        self._workers: list[asyncio.Task[Any]] = []
        self._scheduler: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._last_discovered_refresh = 0.0

    def _bounds(self, src: NewsSource) -> tuple[float, float]:
        if self.role == "hot":
            return self.settings.news_hot_min_poll_seconds, self.settings.news_hot_max_poll_seconds
        return self.settings.news_coverage_min_poll_seconds, self.settings.news_coverage_max_poll_seconds

    def _next_interval(self, rt: RuntimeSource, result: HttpResult, new_count: int) -> float:
        minimum, maximum = self._bounds(rt.source)
        if result.ok and new_count > 0:
            target = max(minimum, min(rt.source.base_poll_seconds, rt.interval) * 0.80)
        elif result.ok:
            target = min(maximum, max(minimum, rt.interval * 1.12))
        elif result.classification == "rate_limited":
            # The normal adaptive cap is only 120 seconds for a hot source.
            # That must never override a publisher's Retry-After response.
            return min(21_600.0, max(60.0, rt.interval * 2.0, result.retry_after or 0.0))
        elif result.classification in {"blocked_or_auth", "not_found", "too_many_redirects", "circuit_open"}:
            # A permanent 403/404 must not be retried every two minutes.
            # Probe again later in case the publisher repairs its feed.
            return min(21_600.0, max(600.0 if rt.failures < 3 else 3600.0, rt.interval * 3.0))
        else:
            target = rt.interval * min(4.0, 1.5 + 0.25 * rt.failures)
        return min(maximum * 4.0, max(minimum, target))

    async def _fetch_source(self, rt: RuntimeSource) -> None:
        src = rt.source
        start = time.perf_counter()
        conditional_headers = await self.store.http_cache_headers(src.key)
        result = await self.http.get(src.url, headers=conditional_headers or None, allow_status={304})
        parsed_count = 0
        new_count = 0
        error = ""
        if result.ok and result.status == 304:
            self.metrics.inc("news_http_not_modified")
        elif result.ok:
            try:
                await self.store.update_http_cache(src.key, result.headers)
                profile = self.registry.parser_profile(result.url or src.url)
                if profile:
                    self.metrics.inc("parser_profile_applied")
                else:
                    self.metrics.inc("parser_profile_missing")
                if src.mode in {"feed", "google_feed"}:
                    items = await asyncio.to_thread(parse_feed, src, result.body, profile)
                else:
                    items = await asyncio.to_thread(parse_priority_page, src, result.body, result.url, profile)
                parsed_count = len(items)
                # Reject obvious non-fundamental transport noise before it can
                # occupy the durable processing queue.  Official sources retain
                # their provider trust boost; the same canonical materiality
                # gate is applied again by the processor before model ingestion.
                accepted = [
                    item for item in items
                    if admission_eligible(item, official=src.official)
                ]
                self.metrics.inc("news_ingest_noise_filtered", len(items) - len(accepted))
                for item in accepted:
                    item.payload["official"] = src.official
                    item.payload["tier"] = src.tier
                new_count = await self.store.insert_raw_many(accepted)
                self.metrics.inc("news_parsed_items", parsed_count)
                self.metrics.inc("news_raw_inserted", new_count)
                self.metrics.inc("news_raw_duplicates", max(0, len(accepted) - new_count))
                rt.failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("parse failure source=%s url=%s", src.name, src.url)
                self.metrics.inc("news_parse_error")
                error = f"parse:{type(exc).__name__}:{exc}"
                result = HttpResult(False, result.url, result.status, result.body[:1000], result.headers, result.latency_ms, "parse_error", error)
                rt.failures += 1
        else:
            rt.failures += 1
            error = result.error
            self.metrics.inc(f"http_{result.classification}")

        rt.polls += 1
        rt.new_items += new_count
        rt.interval = self._next_interval(rt, result, new_count)
        jitter = random.random() * min(1.0, rt.interval * 0.05)
        rt.next_due = time.monotonic() + rt.interval + jitter
        rt.in_flight = False
        await self.store.source_result(
            src.key, src.name, src.url,
            ok=result.ok,
            status=result.classification,
            http_status=result.status,
            latency_ms=result.latency_ms or ((time.perf_counter() - start) * 1000.0),
            parsed=parsed_count,
            new_items=new_count,
            rate_limited=result.classification == "rate_limited",
            timeout=result.classification == "timeout",
            error=error,
            next_due=time.time() + rt.interval,
            adaptive_poll_seconds=rt.interval,
        )

    async def _worker(self, idx: int) -> None:
        while not self._stop.is_set():
            try:
                rt = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._fetch_source(rt)
            except asyncio.CancelledError:
                raise
            except Exception:
                rt.in_flight = False
                rt.failures += 1
                rt.next_due = time.monotonic() + min(300.0, 2 ** min(8, rt.failures))
                self.metrics.inc("collector_worker_error")
                logger.exception("collector worker failed worker=%s source=%s", idx, rt.source.name)
            finally:
                self.queue.task_done()

    async def _refresh_discovered(self) -> None:
        if self.role != "coverage":
            return
        rows = await self.store.discovered_feeds(limit=5000)
        now = time.monotonic()
        added = 0
        for row in rows:
            url = str(row.get("url") or "")
            if not url:
                continue
            key = "discovered:" + __import__("hashlib").sha1(url.encode()).hexdigest()
            if key in self.sources:
                continue
            src = NewsSource(key=key,name=f"Discovered feed {urlsplit(url).netloc}",url=url,category="GLOBAL_NEWS",region="GLOBAL",language="",official=False,tier=2,mode="feed",base_poll_seconds=120.0)
            minimum, maximum = self._bounds(src)
            interval = min(maximum,max(minimum,src.base_poll_seconds))
            self.sources[key] = RuntimeSource(src,interval,now + random.random()*5.0)
            added += 1
        if added:
            self.metrics.inc("discovered_sources_added", added)

    async def _scheduler_loop(self) -> None:
        last_backpressure_check = 0.0
        pending = 0
        while not self._stop.is_set():
            now = time.monotonic()
            if self.role == "coverage" and now - self._last_discovered_refresh >= 300.0:
                try:
                    await self._refresh_discovered()
                except Exception:
                    logger.exception("failed to refresh discovered feeds")
                    self.metrics.inc("discovered_refresh_error")
                self._last_discovered_refresh = now
            if now - last_backpressure_check >= 1.0:
                pending = await self.store.pending_count()
                self.metrics.set("raw_pending", pending)
                last_backpressure_check = now
            if pending >= self.settings.news_raw_backpressure_rows:
                self.metrics.inc("news_backpressure_cycles")
                await asyncio.sleep(1.0)
                continue
            due = [rt for rt in self.sources.values() if not rt.in_flight and rt.next_due <= now]
            due.sort(key=lambda x: (x.source.tier, x.next_due))
            for rt in due[: max(1, self.queue.maxsize // 4)]:
                rt.in_flight = True
                await self.queue.put(rt)
            self.metrics.set("collector_queue", self.queue.qsize())
            self.metrics.set("sources_total", len(self.sources))
            await asyncio.sleep(0.10 if self.role == "hot" else 0.25)

    async def run(self, stop_event: asyncio.Event) -> None:
        concurrency = self.settings.news_hot_concurrency if self.role == "hot" else self.settings.news_coverage_concurrency
        self.metrics.set("registry", self.registry.summary())
        self._workers = [asyncio.create_task(self._worker(i), name=f"news-{self.role}-worker-{i}") for i in range(concurrency)]
        self._scheduler = asyncio.create_task(self._scheduler_loop(), name=f"news-{self.role}-scheduler")
        stop_task = asyncio.create_task(stop_event.wait(), name=f"news-{self.role}-stop-wait")
        logger.info("news collector started role=%s sources=%s workers=%s", self.role, len(self.sources), concurrency)
        try:
            watched = [stop_task, self._scheduler, *self._workers]
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            if stop_task not in done:
                failed = next(iter(done))
                exc = None
                if not failed.cancelled():
                    exc = failed.exception()
                self.metrics.inc("collector_internal_task_fatal")
                raise RuntimeError(f"collector internal task terminated unexpectedly task={failed.get_name()} error={exc}") from exc
        finally:
            self._stop.set()
            stop_task.cancel()
            if self._scheduler:
                self._scheduler.cancel()

            # Scheduler queue entries are *poll intents*, not accepted news data.  Once shutdown
            # begins no new HTTP work should start.  Give currently executing fetches a bounded
            # opportunity to finish and persist any already received observations, then cancel
            # only the remaining network tasks.  Never queue.join() after setting _stop: workers
            # intentionally stop taking new poll intents, so waiting for unfinished queue items
            # would deadlock shutdown.
            worker_wait = min(5.0, max(1.0, self.settings.shutdown_grace_seconds * 0.25))
            try:
                await asyncio.wait_for(asyncio.gather(*self._workers, return_exceptions=True), timeout=worker_wait)
            except asyncio.TimeoutError:
                self.metrics.inc("collector_shutdown_inflight_cancel")
                logger.warning("collector in-flight fetch shutdown window expired role=%s", self.role)
                for task in self._workers:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*self._workers, return_exceptions=True)

            discarded = 0
            while True:
                try:
                    rt = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                rt.in_flight = False
                self.queue.task_done()
                discarded += 1
            if discarded:
                self.metrics.inc("collector_shutdown_poll_intents_discarded", discarded)
                logger.info("collector discarded unsent poll intents during shutdown role=%s count=%s", self.role, discarded)
            self.metrics.set("collector_queue_final", self.queue.qsize())

            results = await asyncio.gather(*(([self._scheduler] if self._scheduler else []) + [stop_task]), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    logger.error("collector task ended with error role=%s error=%s", self.role, result)
            await self.http.close()
