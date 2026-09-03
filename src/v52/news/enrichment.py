from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import uuid

from bs4 import BeautifulSoup
try:
    from pypdf import PdfReader
except ImportError:  # package remains functional for HTML-only sources; preflight reports enhancement state
    PdfReader = None  # type: ignore[assignment]
try:
    import trafilatura
except ImportError:
    trafilatura = None  # type: ignore[assignment]

from ..config import Settings
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.control import ControlStore

logger = logging.getLogger(__name__)


class EnrichmentService:
    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.store = ControlStore(settings)
        self.owner = f"enrichment:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.http = ResilientHttpClient(settings, name="enrichment", concurrency=settings.news_enrichment_concurrency, per_host_limit=2, retries=2, max_body_bytes=5_000_000)

    async def _one(self, job: dict) -> None:
        job_id = int(job["id"])
        story_id = int(job["story_id"])
        if time.time() - float(job.get("created_at") or time.time()) > self.settings.news_enrichment_max_age_seconds:
            await self.store.retry_job("enrichment_jobs", job_id, "stale_enrichment_job", 0.0, dead=True)
            self.metrics.inc("enrichment_stale_skipped")
            return
        url = str(job["url"])
        result = await self.http.get(url)
        if not result.ok:
            attempts = int(job.get("attempts") or 1)
            dead = attempts >= 8 or result.classification in {"not_found", "blocked_or_auth"}
            delay = result.retry_after or min(3600.0, 15.0 * (2 ** min(8, attempts)))
            await self.store.retry_job("enrichment_jobs", job_id, f"{result.classification}:{result.error}", delay, dead)
            self.metrics.inc("enrichment_http_failure")
            return
        content_type = str(result.headers.get("Content-Type") or "").lower()
        is_pdf = "application/pdf" in content_type or url.lower().split("?", 1)[0].endswith(".pdf")

        def extract_article(body: bytes) -> str:
            if is_pdf:
                if PdfReader is None:
                    raise RuntimeError("PDF enrichment requires pypdf; install requirements.txt")
                reader = PdfReader(io.BytesIO(body))
                parts: list[str] = []
                for page in reader.pages[:250]:
                    value = (page.extract_text() or "").strip()
                    if value:
                        parts.append(value)
                return "\n".join(parts)
            html = body.decode("utf-8", errors="replace")
            # V50 relied on trafilatura for publisher-specific article bodies; keep it first,
            # then fall back to a deterministic lxml/BeautifulSoup extraction.
            extracted = None
            if trafilatura is not None:
                extracted = trafilatura.extract(
                    html, include_comments=False, include_tables=True, favor_precision=True,
                    output_format="txt", url=url,
                )
            if extracted and len(extracted.strip()) >= 80:
                return extracted.strip()
            soup = BeautifulSoup(html, "lxml")
            for node in soup.select("script,style,noscript,nav,header,footer,aside,form,svg"):
                node.decompose()
            root = soup.find("article") or soup.find("main") or soup.body or soup
            parts = []
            for node in root.find_all(["h1", "h2", "p", "li"]):
                value = " ".join(node.get_text(" ", strip=True).split())
                if len(value) >= 30:
                    parts.append(value)
            return "\n".join(parts)
        text = (await asyncio.to_thread(extract_article, result.body)).strip()
        if not text:
            attempts = int(job.get("attempts") or 1)
            await self.store.retry_job("enrichment_jobs", job_id, "empty_extract", 300.0, attempts >= 4)
            self.metrics.inc("enrichment_empty")
            return
        await self.store.complete_enrichment(job_id, story_id, text[:100_000])
        self.metrics.inc("enrichment_completed")

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.news_enrichment_enabled:
            logger.info("enrichment disabled")
            await self.http.close()
            await stop_event.wait()
            return
        sem = asyncio.Semaphore(self.settings.news_enrichment_concurrency)
        async def wrapped(job: dict) -> None:
            async with sem:
                try:
                    await self._one(job)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("enrichment job crashed id=%s", job.get("id"))
                    self.metrics.inc("enrichment_error")
                    await self.store.retry_job("enrichment_jobs", int(job["id"]), f"{type(exc).__name__}:{exc}", 60.0)
        tasks: set[asyncio.Task] = set()
        try:
            while not stop_event.is_set():
                tasks = {t for t in tasks if not t.done()}
                capacity = max(0, self.settings.news_enrichment_concurrency - len(tasks))
                if capacity:
                    jobs = await self.store.lease_enrichment(self.owner, capacity, 120.0)
                    for job in jobs:
                        task = asyncio.create_task(wrapped(job))
                        tasks.add(task)
                if not tasks:
                    await asyncio.sleep(0.5)
                else:
                    await asyncio.sleep(0.1)
        finally:
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15.0)
                except asyncio.TimeoutError:
                    for t in tasks:
                        t.cancel()
            await self.http.close()
