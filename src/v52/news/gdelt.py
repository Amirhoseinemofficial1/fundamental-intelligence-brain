from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

from ..config import Settings
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.news import NewsStore
from .external_common import make_raw_news
from .relevance import admission_eligible, fundamental_relevant

logger = logging.getLogger(__name__)

_QUERIES: tuple[tuple[str, str], ...] = (
    ("central bank OR interest rate OR inflation OR treasury OR bond", "ECONOMY"),
    ("sanctions OR tariff OR export controls OR trade restrictions", "SANCTIONS"),
    ("bank OR markets OR earnings OR merger OR credit rating", "MARKETS"),
    ("war OR ceasefire OR missile OR geopolitical OR military", "GEOPOLITICS"),
    ("oil OR OPEC OR LNG OR refinery OR gold OR copper", "ENERGY"),
    ("bitcoin OR crypto OR stablecoin OR ETF OR exchange", "CRYPTO"),
)

_HARD_NOISE = {
    "football", "soccer", "basketball", "tennis", "baseball", "cricket", "golf", "horoscope",
    "celebrity", "movie", "film", "tv show", "recipe", "fashion", "coupon", "promo code",
    "net worth", "dating", "wedding", "game review", "video game", "lottery", "obituary",
    "playstation", "xbox", "concert", "streaming series", "box office", "quiz", "giveaway",
}
_HIGH_IMPACT = {
    "central bank", "interest rate", "rate cut", "rate hike", "export control", "debt ceiling",
    "government shutdown", "credit rating", "bond auction", "earnings guidance", "profit warning",
    "merger agreement", "bankruptcy filing", "share buyback", "initial public offering",
    "supply chain", "data breach", "military strike", "ceasefire agreement", "exchange halted",
    "withdrawals suspended", "stablecoin depeg", "etf approval", "sanctions", "tariff", "inflation",
    "cpi", "ppi", "payroll", "unemployment", "gdp", "recession", "opec", "lng", "refinery",
    "bitcoin", "ethereum", "stablecoin", "liquidation", "hack", "exploit", "depeg", "filing",
}


def _gdelt_relevant(title: str, category: str) -> bool:
    text = title.lower()
    noise = sum(1 for x in _HARD_NOISE if x in text)
    impact = sum(1 for x in _HIGH_IMPACT if x in text)
    if noise and not impact:
        return False
    return fundamental_relevant(category, title, "")


class GdeltService:
    """Dedicated GDELT DOC API backstop.

    It writes directly to the durable raw-news database. GDELT is intentionally isolated from
    first-party hot feeds so a slow/limited aggregator cannot steal worker slots from Reuters,
    regulators, exchanges, central banks, or direct publisher feeds.
    """

    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.store = NewsStore(settings)
        self.http = ResilientHttpClient(
            settings,
            name="gdelt",
            concurrency=max(1, min(8, settings.gdelt_concurrency)),
            per_host_limit=2,
            min_host_gap_seconds=settings.gdelt_min_gap_seconds,
            retries=2,
            total_timeout=settings.gdelt_timeout_seconds,
            max_body_bytes=8_000_000,
        )

    async def _one(self, query: str, category: str, stop_event: asyncio.Event) -> None:
        await asyncio.sleep(random.random() * 1.5)
        while not stop_event.is_set():
            result = await self.http.get(
                self.endpoint,
                params={
                    "query": query,
                    "mode": "ArtList",
                    "maxrecords": str(self.settings.gdelt_max_records),
                    "format": "json",
                    "sort": "DateDesc",
                    "timespan": self.settings.gdelt_timespan,
                },
            )
            wait = self.settings.gdelt_poll_seconds
            if result.ok:
                try:
                    obj: Any = json.loads(result.body)
                    rows = obj.get("articles") if isinstance(obj, dict) else []
                    accepted = []
                    for row in (rows or [])[: self.settings.gdelt_max_records]:
                        if not isinstance(row, dict):
                            continue
                        title = str(row.get("title") or "")
                        if not _gdelt_relevant(title, category):
                            self.metrics.inc("gdelt_nonfundamental_filtered")
                            continue
                        url = str(row.get("url") or "")
                        item = make_raw_news(
                            source_key=f"gdelt:{category.lower()}",
                            source_name=str(row.get("domain") or "GDELT"),
                            source_url=self.endpoint,
                            item_url=url,
                            title=title,
                            category=category,
                            region=str(row.get("sourcecountry") or "GLOBAL"),
                            language=str(row.get("language") or ""),
                            published_at=row.get("seendate") or row.get("date"),
                            external_id=url,
                            priority=5,
                            payload={"source_type": "GDELT", "aggregator": "GDELT", "query": query},
                        )
                        if item and admission_eligible(item):
                            accepted.append(item)
                    inserted = await self.store.insert_raw_many(accepted)
                    self.metrics.inc("news_raw_inserted", inserted)
                    self.metrics.inc("gdelt_items_seen", len(accepted))
                    self.metrics.inc("gdelt_duplicates", max(0, len(accepted) - inserted))
                    self.metrics.inc("gdelt_batches")
                    await self.store.source_result(
                        f"gdelt:{category.lower()}", f"GDELT {category}", self.endpoint,
                        ok=True, status="ok", http_status=result.status, latency_ms=result.latency_ms,
                        parsed=len(rows or []), new_items=inserted,
                        next_due=0.0, adaptive_poll_seconds=self.settings.gdelt_poll_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("GDELT parse failed category=%s", category)
                    self.metrics.inc("gdelt_parse_error")
                    await self.store.source_result(
                        f"gdelt:{category.lower()}", f"GDELT {category}", self.endpoint,
                        ok=False, status="parse_error", http_status=result.status, latency_ms=result.latency_ms,
                        error=f"{type(exc).__name__}:{exc}", adaptive_poll_seconds=self.settings.gdelt_poll_seconds,
                    )
            else:
                self.metrics.inc(f"gdelt_{result.classification}")
                wait = max(wait, result.retry_after or 0.0)
                await self.store.source_result(
                    f"gdelt:{category.lower()}", f"GDELT {category}", self.endpoint,
                    ok=False, status=result.classification, http_status=result.status, latency_ms=result.latency_ms,
                    rate_limited=result.classification == "rate_limited", timeout=result.classification == "timeout",
                    error=result.error, adaptive_poll_seconds=wait,
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait + random.random() * 1.5)
            except asyncio.TimeoutError:
                continue

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.gdelt_enabled:
            logger.info("GDELT plane disabled")
            await self.http.close()
            await stop_event.wait()
            return
        tasks = [asyncio.create_task(self._one(q, cat, stop_event), name=f"gdelt:{cat}") for q, cat in _QUERIES]
        self.metrics.set("gdelt_queries", len(tasks))
        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    self.metrics.inc("gdelt_task_shutdown_error")
                    logger.error("GDELT task terminated with error=%s", result)
            await self.http.close()
