from __future__ import annotations

import asyncio
import logging
import random
import time

from ..config import Settings
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.news import NewsStore
from .parse import discover_feed_links
from .registry import SourceRegistry

logger = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.registry = SourceRegistry(settings)
        self.store = NewsStore(settings)
        self.http = ResilientHttpClient(settings, name="discovery", concurrency=settings.news_discovery_concurrency, per_host_limit=1, retries=1, total_timeout=10.0, max_body_bytes=1_500_000)

    async def _probe(self, row: dict) -> None:
        url = str(row.get("url") or "")
        if not url:
            return
        result = await self.http.get(url)
        self.metrics.inc("discovery_attempt")
        if not result.ok:
            self.metrics.inc(f"discovery_{result.classification}")
            return
        links = await asyncio.to_thread(discover_feed_links, result.url, result.body)
        self.metrics.inc("discovery_page_ok")
        for link in links:
            if await self.store.add_discovered_feed(link, result.url):
                self.metrics.inc("discovery_feed_new")

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.discovery_enabled:
            logger.info("discovery plane disabled")
            await self.http.close()
            await stop_event.wait()
            return
        sites = self.registry.discovery_sites()
        cursor = 0
        try:
            while not stop_event.is_set():
                if not sites:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=60.0)
                    except asyncio.TimeoutError:
                        continue
                    break
                batch = []
                for _ in range(min(self.settings.discovery_max_sites_per_cycle, len(sites))):
                    batch.append(sites[cursor % len(sites)])
                    cursor += 1
                sem = asyncio.Semaphore(self.settings.news_discovery_concurrency)
                async def one(x):
                    async with sem:
                        try:
                            await self._probe(x)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            self.metrics.inc("discovery_unexpected")
                            logger.exception("discovery probe failed url=%s", x.get("url"))
                batch_task = asyncio.gather(*(one(x) for x in batch))
                stop_task = asyncio.create_task(stop_event.wait())
                try:
                    done, _ = await asyncio.wait({batch_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                    if stop_task in done and stop_event.is_set() and not batch_task.done():
                        batch_task.cancel()
                        await asyncio.gather(batch_task, return_exceptions=True)
                        self.metrics.inc("discovery_shutdown_inflight_cancel")
                        break
                    await batch_task
                finally:
                    if not stop_task.done():
                        stop_task.cancel()
                    await asyncio.gather(stop_task, return_exceptions=True)
                self.metrics.set("discovery_cursor", cursor)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.settings.discovery_cycle_seconds)
                except asyncio.TimeoutError:
                    continue
        finally:
            await self.http.close()
