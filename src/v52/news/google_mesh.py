from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import random
import time
from collections import defaultdict
from urllib.parse import quote_plus

from ..config import Settings
from ..metrics import Metrics
from ..models import NewsSource
from ..net.http import ResilientHttpClient
from ..storage.news import NewsStore
from .parse import parse_feed
from .relevance import admission_eligible
from .registry import SourceRegistry, _domain

logger = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True)
class _State:
    source: NewsSource
    interval: float
    next_due: float
    failures: int = 0
    in_flight: bool = False


def _key(name: str, url: str) -> str:
    return "google-mesh:" + hashlib.sha1(f"{name}|{url}".encode()).hexdigest()


class GoogleMeshService:
    """Restores the V50 78-locale Google News matrix and grouped publisher fallbacks.

    This is a separate coverage plane: first-party/direct feeds remain the highest-priority path.
    Google is used as a resilient discovery/fallback surface and cannot block the hot collector.
    """

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.registry = SourceRegistry(settings)
        self.store = NewsStore(settings)
        self.http = ResilientHttpClient(
            settings,
            name="google-mesh",
            concurrency=settings.google_mesh_concurrency,
            per_host_limit=max(1, min(8, settings.google_mesh_per_host_limit)),
            min_host_gap_seconds=settings.google_mesh_min_gap_seconds,
            retries=2,
            total_timeout=settings.news_http_total_timeout,
            max_body_bytes=settings.news_max_body_bytes,
        )
        self.states = self._build_states()
        self.queue: asyncio.Queue[_State] = asyncio.Queue(maxsize=max(1000, len(self.states) * 2))
        self._stop = asyncio.Event()

    def _build_sources(self) -> list[NewsSource]:
        base = "https://news.google.com/rss"
        topics = (
            ("WORLD", "WORLD"),
            ("WORLD", "NATION"),
            ("BUSINESS", "BUSINESS"),
            ("TECH", "TECHNOLOGY"),
            ("ENTERTAINMENT", "ENTERTAINMENT"),
            ("SPORTS", "SPORTS"),
            ("TECH", "SCIENCE"),
            ("HEALTH", "HEALTH"),
        )
        out: list[NewsSource] = []
        # Full 78-locale TOP + eight standard topic feeds = 702 independent feeds.
        for row in self.registry.fundamental.get("google_locales", []):
            if not isinstance(row, list) or len(row) < 4:
                continue
            country, hl, ceid, lang = (str(x) for x in row[:4])
            top = f"{base}?hl={quote_plus(hl)}&gl={quote_plus(country)}&ceid={quote_plus(ceid)}"
            out.append(NewsSource(_key(f"Google {country} TOP", top), f"Google {country} TOP", top, "WORLD", country, lang, False, 1, "google_feed", self.settings.google_top_poll_seconds))
            for cat, topic in topics:
                url = f"{base}/headlines/section/topic/{topic}?hl={quote_plus(hl)}&gl={quote_plus(country)}&ceid={quote_plus(ceid)}"
                out.append(NewsSource(_key(f"Google {country} {cat}", url), f"Google {country} {cat}", url, cat, country, lang, False, 2, "google_feed", self.settings.google_topic_poll_seconds))

        # Group publisher fallbacks so thousands of domains do not become thousands of requests.
        if self.settings.google_publisher_fallback_enabled:
            by_cat: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
            seen: set[tuple[str, str]] = set()
            for row in self.registry.discovery_sites():
                domain = _domain(str(row.get("url") or ""))
                if not domain:
                    continue
                cat = str(row.get("category") or "WORLD").upper()
                sig = (cat, domain)
                if sig in seen:
                    continue
                seen.add(sig)
                by_cat[cat].append((domain, str(row.get("region") or "GLOBAL")))
            batch_size = self.settings.google_publisher_batch_domains
            for cat, entries in sorted(by_cat.items()):
                for off in range(0, len(entries), batch_size):
                    chunk = entries[off : off + batch_size]
                    query = " OR ".join(f"site:{domain}" for domain, _ in chunk)
                    if self.settings.google_publisher_when:
                        query = f"({query}) when:{self.settings.google_publisher_when}"
                    url = f"{base}/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
                    region = chunk[0][1] if len({x[1] for x in chunk}) == 1 else "GLOBAL"
                    name = f"Google publisher fallback {cat} {off // batch_size + 1}"
                    out.append(NewsSource(_key(name, url), name, url, cat, region, "en", False, 2, "google_feed", self.settings.google_publisher_poll_seconds))

        # Fundamental query families catch stories from publishers not yet present in the catalog.
        for cat, query in (
            ("ECONOMY", '"central bank" OR inflation OR "interest rate" OR treasury OR bonds'),
            ("SANCTIONS", 'sanctions OR tariff OR "export controls"'),
            ("MARKETS", 'earnings OR merger OR bankruptcy OR downgrade OR "trading halt"'),
            ("GEOPOLITICS", 'ceasefire OR missile OR war OR blockade OR military'),
            ("ENERGY", 'OPEC OR oil OR LNG OR refinery OR copper OR gold'),
            ("CRYPTO", 'bitcoin OR ethereum OR stablecoin OR ETF OR "exchange hack"'),
        ):
            q = f"({query}) when:{self.settings.google_publisher_when}" if self.settings.google_publisher_when else query
            url = f"{base}/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
            out.append(NewsSource(_key(f"Google fundamental {cat}", url), f"Google fundamental {cat}", url, cat, "GLOBAL", "en", False, 1, "google_feed", self.settings.google_topic_poll_seconds))

        unique: dict[str, NewsSource] = {}
        for src in out:
            unique[src.url] = src
        return list(unique.values())

    def _build_states(self) -> dict[str, _State]:
        now = time.monotonic()
        states: dict[str, _State] = {}
        for idx, src in enumerate(self._build_sources()):
            interval = max(self.settings.google_mesh_min_poll_seconds, src.base_poll_seconds)
            interval = min(self.settings.google_mesh_max_poll_seconds, interval)
            # Spread startup instead of stampeding Google and the local CPU at t=0.
            due = now + (idx % max(1, self.settings.google_mesh_startup_spread_seconds * 10)) / 10.0
            states[src.key] = _State(src, interval, due + random.random() * 0.25)
        return states

    def _next_interval(self, st: _State, ok: bool, classification: str, new_items: int, retry_after: float | None) -> float:
        if ok and new_items:
            value = max(self.settings.google_mesh_min_poll_seconds, st.interval * 0.85)
        elif ok:
            value = min(self.settings.google_mesh_max_poll_seconds, st.interval * 1.10)
        elif classification == "rate_limited":
            value = max(st.interval * 2.0, retry_after or 60.0)
        elif classification in {"blocked_or_auth", "too_many_redirects"}:
            value = max(600.0, st.interval * 3.0)
        else:
            value = st.interval * min(4.0, 1.5 + 0.25 * st.failures)
        return min(self.settings.google_mesh_max_poll_seconds * 6.0, max(self.settings.google_mesh_min_poll_seconds, value))

    async def _fetch(self, st: _State) -> None:
        src = st.source
        result = await self.http.get(src.url)
        parsed = 0
        inserted = 0
        error = result.error
        if result.ok:
            try:
                items = await asyncio.to_thread(parse_feed, src, result.body)
                parsed = len(items)
                accepted = [
                    item for item in items
                    if admission_eligible(item)
                ][:150]
                self.metrics.inc("google_mesh_noise_filtered", max(0, len(items) - len(accepted)))
                for item in accepted:
                    item.payload.update({"source_type": "GOOGLE_NEWS", "google_mesh": True})
                inserted = await self.store.insert_raw_many(accepted)
                self.metrics.inc("news_raw_inserted", inserted)
                self.metrics.inc("google_mesh_parsed", parsed)
                self.metrics.inc("google_mesh_duplicates", max(0, len(accepted) - inserted))
                st.failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Google mesh parse failure source=%s", src.name)
                self.metrics.inc("google_mesh_parse_error")
                result.ok = False
                result.classification = "parse_error"
                error = f"{type(exc).__name__}:{exc}"
                st.failures += 1
        else:
            st.failures += 1
            self.metrics.inc(f"google_mesh_{result.classification}")
        st.interval = self._next_interval(st, result.ok, result.classification, inserted, result.retry_after)
        st.next_due = time.monotonic() + st.interval + random.random() * min(2.0, st.interval * 0.05)
        st.in_flight = False
        await self.store.source_result(
            src.key, src.name, src.url, ok=result.ok, status=result.classification,
            http_status=result.status, latency_ms=result.latency_ms, parsed=parsed, new_items=inserted,
            rate_limited=result.classification == "rate_limited", timeout=result.classification == "timeout",
            error=error, adaptive_poll_seconds=st.interval,
        )

    async def _worker(self, idx: int) -> None:
        while not self._stop.is_set():
            try:
                st = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._fetch(st)
            except asyncio.CancelledError:
                raise
            except Exception:
                st.failures += 1
                st.in_flight = False
                st.next_due = time.monotonic() + min(600.0, 2 ** min(9, st.failures))
                self.metrics.inc("google_mesh_worker_error")
                logger.exception("Google mesh worker failure idx=%s source=%s", idx, st.source.name)
            finally:
                self.queue.task_done()

    async def _scheduler(self) -> None:
        while not self._stop.is_set():
            pending = await self.store.pending_count()
            self.metrics.set("raw_pending", pending)
            if pending >= self.settings.news_raw_backpressure_rows:
                self.metrics.inc("google_mesh_backpressure")
                await asyncio.sleep(1.0)
                continue
            now = time.monotonic()
            due = [s for s in self.states.values() if not s.in_flight and s.next_due <= now]
            due.sort(key=lambda x: (x.source.tier, x.next_due))
            for st in due[: max(1, self.settings.google_mesh_concurrency * 2)]:
                st.in_flight = True
                await self.queue.put(st)
            self.metrics.set("google_mesh_queue", self.queue.qsize())
            self.metrics.set("google_mesh_sources", len(self.states))
            await asyncio.sleep(0.20)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.google_mesh_enabled:
            logger.info("Google global mesh disabled")
            await self.http.close()
            await stop_event.wait()
            return
        workers = [asyncio.create_task(self._worker(i), name=f"google-mesh-worker:{i}") for i in range(self.settings.google_mesh_concurrency)]
        scheduler = asyncio.create_task(self._scheduler(), name="google-mesh-scheduler")
        stop_task = asyncio.create_task(stop_event.wait(), name="google-mesh-stop")
        self.metrics.set("google_mesh_sources", len(self.states))
        logger.info("Google global mesh started sources=%s workers=%s", len(self.states), len(workers))
        try:
            done, _ = await asyncio.wait([stop_task, scheduler, *workers], return_when=asyncio.FIRST_COMPLETED)
            if stop_task not in done and not stop_event.is_set():
                failures = [t for t in done if t is not stop_task and not t.cancelled() and t.exception() is not None]
                if failures:
                    raise RuntimeError(f"Google mesh critical task failed: {failures[0].exception()}")
                raise RuntimeError("Google mesh critical task exited unexpectedly")
        finally:
            self._stop.set()
            for t in [scheduler, *workers, stop_task]:
                t.cancel()
            await asyncio.gather(scheduler, *workers, stop_task, return_exceptions=True)
            await self.http.close()
