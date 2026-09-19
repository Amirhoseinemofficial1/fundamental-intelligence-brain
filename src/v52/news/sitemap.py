from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from lxml import etree

from ..config import Settings
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.news import NewsStore
from .external_common import make_raw_news
from .parse import canonical_url
from .relevance import fundamental_relevant
from .registry import SourceRegistry

logger = logging.getLogger(__name__)

_ARTICLE_HINT = re.compile(r"/(news|article|press|release|announcement|media|story|stories|updates?)/", re.I)


@dataclass(slots=True)
class Site:
    name: str
    url: str
    category: str
    region: str
    tier: int


def _xml(body: bytes) -> Any | None:
    try:
        parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
        return etree.fromstring(body, parser=parser)
    except (etree.XMLSyntaxError, TypeError, ValueError):
        return None


def _text(node: Any, local: str) -> str:
    rows = node.xpath(f"./*[local-name()='{local}']")
    if not rows:
        return ""
    return "".join(rows[0].itertext()).strip()


def parse_sitemap(site: Site, sitemap_url: str, body: bytes, parser_profile: dict[str, Any] | None) -> tuple[list[str], list[Any]]:
    root = _xml(body)
    if root is None:
        return [], []
    nested: list[str] = []
    items = []
    root_name = etree.QName(root).localname.lower() if isinstance(root.tag, str) else ""
    if root_name == "sitemapindex":
        for node in root.xpath("//*[local-name()='sitemap']")[:5000]:
            loc = canonical_url(_text(node, "loc"))
            if loc.startswith(("http://", "https://")):
                nested.append(loc)
        return nested, []

    for node in root.xpath("//*[local-name()='url']")[:10000]:
        loc = canonical_url(_text(node, "loc"))
        if not loc.startswith(("http://", "https://")):
            continue
        title_rows = node.xpath(".//*[local-name()='title']")
        title = "".join(title_rows[0].itertext()).strip() if title_rows else ""
        pub_rows = node.xpath(".//*[local-name()='publication_date']")
        published = "".join(pub_rows[0].itertext()).strip() if pub_rows else _text(node, "lastmod")
        # News sitemaps carry an explicit title. For generic sitemaps, only accept article-like
        # URLs and derive a conservative slug title; these rows are still filtered downstream.
        if not title and _ARTICLE_HINT.search(urlsplit(loc).path):
            slug = urlsplit(loc).path.rstrip("/").split("/")[-1]
            title = re.sub(r"[-_]+", " ", slug).strip()
        if not title:
            continue
        if not fundamental_relevant(site.category, title):
            continue
        item = make_raw_news(
            source_key=f"sitemap:{site.name}:{urlsplit(sitemap_url).netloc}",
            source_name=f"{site.name} sitemap",
            source_url=sitemap_url,
            item_url=loc,
            title=title,
            category=site.category,
            region=site.region,
            published_at=published or None,
            external_id=loc,
            priority=max(1, 9 - site.tier * 2),
            payload={
                "source_type": "NEWS_SITEMAP" if title_rows else "SITEMAP",
                "parser_profile": (parser_profile or {}).get("parser_id"),
                "parser_stack": (parser_profile or {}).get("parser_stack", []),
            },
        )
        if item:
            items.append(item)
    return nested, items


class SitemapService:
    """Cold-plane sitemap/news-sitemap crawler with bounded fanout and durable output."""

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.registry = SourceRegistry(settings)
        self.store = NewsStore(settings)
        self.http = ResilientHttpClient(
            settings,
            name="sitemap",
            concurrency=settings.sitemap_concurrency,
            per_host_limit=1,
            min_host_gap_seconds=settings.sitemap_host_gap_seconds,
            retries=1,
            total_timeout=settings.sitemap_timeout_seconds,
            max_body_bytes=settings.sitemap_max_body_bytes,
        )
        self.sites = self._sites()
        self.cursor = 0

    def _sites(self) -> list[Site]:
        out: list[Site] = []
        seen: set[str] = set()
        for row in self.registry.discovery_sites():
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            p = urlsplit(url)
            if not p.scheme or not p.netloc:
                continue
            origin = f"{p.scheme}://{p.netloc}/"
            if origin in seen:
                continue
            seen.add(origin)
            official = bool(row.get("official", False))
            tier = self.registry.tier_by_domain.get(p.netloc.lower().removeprefix("www."), 1 if official else 2)
            out.append(Site(str(row.get("name") or p.netloc), origin, str(row.get("category") or "GLOBAL_NEWS"), str(row.get("region") or "GLOBAL"), tier))
        return out

    async def _candidate_urls(self, site: Site) -> list[str]:
        p = urlsplit(site.url)
        origin = f"{p.scheme}://{p.netloc}"
        urls: list[str] = []
        robots = await self.http.get(origin + "/robots.txt", max_body_bytes=1_000_000)
        if robots.ok:
            for line in robots.text().splitlines():
                if line.lower().startswith("sitemap:"):
                    u = canonical_url(line.split(":", 1)[1].strip())
                    if u.startswith(("http://", "https://")):
                        urls.append(u)
        # Common standards remain fallback even if robots is blocked or incomplete.
        urls.extend((origin + "/sitemap.xml", origin + "/news-sitemap.xml", origin + "/sitemap_news.xml"))
        unique: list[str] = []
        seen: set[str] = set()
        for u in urls:
            u = canonical_url(u)
            if u not in seen:
                seen.add(u); unique.append(u)
        return unique[: self.settings.sitemap_candidates_per_site]

    async def _fetch_sitemap(self, site: Site, url: str, depth: int = 0) -> int:
        result = await self.http.get(url)
        self.metrics.inc("sitemap_http_attempt")
        if not result.ok:
            self.metrics.inc(f"sitemap_{result.classification}")
            return 0
        profile = self.registry.parser_profile(site.url)
        nested, items = await asyncio.to_thread(parse_sitemap, site, result.url, result.body, profile)
        inserted = await self.store.insert_raw_many(items[: self.settings.sitemap_max_items_per_file])
        self.metrics.inc("news_raw_inserted", inserted)
        self.metrics.inc("sitemap_items_seen", len(items))
        self.metrics.inc("sitemap_duplicates", max(0, min(len(items), self.settings.sitemap_max_items_per_file) - inserted))
        await self.store.source_result(
            f"sitemap:{canonical_url(url)}", f"{site.name} sitemap", url,
            ok=True, status="ok", http_status=result.status, latency_ms=result.latency_ms,
            parsed=len(items), new_items=inserted, adaptive_poll_seconds=self.settings.sitemap_cycle_seconds,
        )
        if depth < self.settings.sitemap_nested_depth:
            # Favor news sitemaps first and bound nested fanout.
            nested.sort(key=lambda x: ("news" not in x.lower(), x))
            for child in nested[: self.settings.sitemap_nested_max]:
                if self.store and await self.store.pending_count() >= self.settings.news_raw_backpressure_rows:
                    self.metrics.inc("sitemap_backpressure")
                    break
                inserted += await self._fetch_sitemap(site, child, depth + 1)
        return inserted

    async def _probe_site(self, site: Site) -> None:
        try:
            candidates = await self._candidate_urls(site)
            total = 0
            for url in candidates:
                total += await self._fetch_sitemap(site, url)
            self.metrics.inc("sitemap_sites_completed")
            if total:
                self.metrics.inc("sitemap_sites_with_news")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.metrics.inc("sitemap_site_error")
            logger.exception("sitemap site probe failed site=%s", site.url)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.sitemap_enabled:
            logger.info("sitemap plane disabled")
            await self.http.close(); await stop_event.wait(); return
        sem = asyncio.Semaphore(self.settings.sitemap_concurrency)
        try:
            while not stop_event.is_set():
                if not self.sites:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=60.0)
                    except asyncio.TimeoutError:
                        continue
                    break
                batch: list[Site] = []
                for _ in range(min(self.settings.sitemap_sites_per_cycle, len(self.sites))):
                    batch.append(self.sites[self.cursor % len(self.sites)])
                    self.cursor += 1
                async def one(site: Site) -> None:
                    async with sem:
                        await self._probe_site(site)
                batch_task = asyncio.gather(*(one(site) for site in batch))
                stop_task = asyncio.create_task(stop_event.wait())
                try:
                    done, _ = await asyncio.wait({batch_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                    if stop_task in done and stop_event.is_set() and not batch_task.done():
                        batch_task.cancel()
                        await asyncio.gather(batch_task, return_exceptions=True)
                        self.metrics.inc("sitemap_shutdown_inflight_cancel")
                        break
                    await batch_task
                finally:
                    if not stop_task.done():
                        stop_task.cancel()
                    await asyncio.gather(stop_task, return_exceptions=True)
                self.metrics.set("sitemap_cursor", self.cursor)
                self.metrics.set("sitemap_sites_total", len(self.sites))
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.settings.sitemap_cycle_seconds + random.random() * 10.0)
                except asyncio.TimeoutError:
                    continue
        finally:
            await self.http.close()
