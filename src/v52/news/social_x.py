from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

import aiohttp

from ..config import Settings
from ..loggingx import redact_text
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.news import NewsStore
from .external_common import make_raw_news
from .registry import SourceRegistry

logger = logging.getLogger(__name__)

_STREAM = "https://api.x.com/2/tweets/search/stream"
_RULES = "https://api.x.com/2/tweets/search/stream/rules"
_RECENT = "https://api.x.com/2/tweets/search/recent"

_TOPIC_RULES = (
    '("central bank" OR "rate cut" OR "rate hike" OR inflation OR CPI OR PPI) -is:retweet',
    '(sanctions OR tariff OR "export controls" OR embargo) -is:retweet',
    '(earnings OR merger OR acquisition OR bankruptcy OR downgrade OR "trading halt") -is:retweet',
    '(OPEC OR oil OR LNG OR refinery OR copper OR gold) -is:retweet',
    '(bitcoin OR ethereum OR stablecoin OR ETF OR liquidation OR "exchange hack") -is:retweet',
    '(ceasefire OR missile OR airstrike OR blockade OR military) -is:retweet',
)


class XSocialService:
    """Official X API v2 filtered stream + recent-search fallback.

    Only this process owns the filtered-stream connection, respecting the one-connection-per-App
    constraint documented by X. Rules are tagged and managed without touching unrelated App rules.
    """

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.registry = SourceRegistry(settings)
        self.store = NewsStore(settings)
        self.http = ResilientHttpClient(
            settings, name="x-social", concurrency=4, per_host_limit=2,
            min_host_gap_seconds=settings.x_min_request_gap_seconds, retries=2,
            total_timeout=20.0, max_body_bytes=8_000_000,
        )
        self.token = settings.x_bearer_token
        self.headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _insert_tweet(self, tw: dict[str, Any], users: dict[str, dict[str, Any]], source_type: str) -> None:
        post_id = str(tw.get("id") or "")
        text = str(tw.get("text") or "").strip()
        if not post_id or not text:
            return
        user = users.get(str(tw.get("author_id") or ""), {})
        username = str(user.get("username") or "").strip()
        url = f"https://x.com/{username}/status/{post_id}" if username else f"https://x.com/i/web/status/{post_id}"
        item = make_raw_news(
            source_key="x",
            source_name=f"@{username}" if username else "X",
            source_url=_STREAM if source_type == "X_FILTERED_STREAM" else _RECENT,
            item_url=url,
            title=text[:1000],
            summary=text,
            category="SOCIAL_FUNDAMENTAL",
            region="GLOBAL",
            language=str(tw.get("lang") or ""),
            published_at=tw.get("created_at"),
            external_id=post_id,
            priority=8 if source_type == "X_FILTERED_STREAM" else 7,
            payload={
                "source_type": source_type,
                "author_id": str(tw.get("author_id") or ""),
                "username": username,
                "public_metrics": tw.get("public_metrics") or {},
            },
        )
        if item:
            inserted = await self.store.insert_raw(item)
            self.metrics.inc("news_raw_inserted", int(inserted))
            self.metrics.inc("x_items_inserted" if inserted else "x_duplicates")

    async def _list_rules(self) -> list[dict[str, Any]]:
        result = await self.http.get(_RULES, headers=self.headers, max_body_bytes=4_000_000)
        if not result.ok:
            self.metrics.inc(f"x_rules_{result.classification}")
            await self.store.source_result("x:rules", "X rules", _RULES, ok=False, status=result.classification,
                                           http_status=result.status, latency_ms=result.latency_ms,
                                           rate_limited=result.classification == "rate_limited", error=result.error)
            return []
        try:
            obj = json.loads(result.body)
            return [x for x in (obj.get("data") or []) if isinstance(x, dict)]
        except Exception as exc:
            self.metrics.inc("x_rules_parse_error")
            logger.warning("X rules response parse failed error=%s", exc)
            return []

    def _desired_rules(self) -> list[dict[str, str]]:
        rules: list[dict[str, str]] = []
        handles = self.registry.x_handles()
        size = max(1, self.settings.x_handle_group_size)
        for i in range(0, len(handles), size):
            group = handles[i : i + size]
            value = "(" + " OR ".join(f"from:{h}" for h in group) + ") -is:retweet"
            if len(value) <= 1024:
                rules.append({"value": value, "tag": f"v52-fund-handles-{i // size}"})
        for i, value in enumerate(_TOPIC_RULES):
            rules.append({"value": value, "tag": f"v52-fund-topic-{i}"})
        return rules[: self.settings.x_rule_max]

    async def _sync_rules(self) -> None:
        if not self.settings.x_sync_rules:
            return
        current = await self._list_rules()
        ours = [x for x in current if str(x.get("tag") or "").startswith("v52-fund-")]
        ids = [str(x.get("id")) for x in ours if x.get("id")]
        for i in range(0, len(ids), 100):
            result = await self.http.post(_RULES, headers={**self.headers, "Content-Type": "application/json"}, json_body={"delete": {"ids": ids[i:i+100]}})
            if result.ok:
                self.metrics.inc("x_rules_deleted", len(ids[i:i+100]))
            else:
                self.metrics.inc("x_rule_delete_failed")
                logger.warning("X rule delete failed status=%s class=%s", result.status, result.classification)
        installed = 0
        for rule in self._desired_rules():
            result = await self.http.post(_RULES, headers={**self.headers, "Content-Type": "application/json"}, json_body={"add": [rule]})
            if result.ok:
                installed += 1; self.metrics.inc("x_rules_installed")
            else:
                self.metrics.inc("x_rule_rejected")
                logger.warning("X rule rejected tag=%s status=%s class=%s body=%s", rule["tag"], result.status, result.classification, redact_text(result.text()[:300]))
                if result.classification == "rate_limited" and result.retry_after:
                    await asyncio.sleep(result.retry_after)
        self.metrics.set("x_rules_requested", len(self._desired_rules()))
        self.metrics.set("x_rules_installed_last_sync", installed)

    async def _stream_loop(self, stop_event: asyncio.Event) -> None:
        await self._sync_rules()
        backoff = 1.0
        params = {
            "tweet.fields": "created_at,lang,author_id,public_metrics,entities,referenced_tweets",
            "expansions": "author_id",
            "user.fields": "username,name,public_metrics,verified",
        }
        while not stop_event.is_set():
            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=35)
                async with self.http.session.get(_STREAM, headers=self.headers, params=params, timeout=timeout) as resp:
                    if resp.status >= 400:
                        body = await resp.text(errors="ignore")
                        self.metrics.inc("x_stream_http_error")
                        await self.store.source_result("x:stream", "X filtered stream", _STREAM, ok=False, status=f"http_{resp.status}", http_status=resp.status, latency_ms=0.0, rate_limited=resp.status == 429, error=redact_text(body[:500]))
                        await asyncio.sleep(60.0 if resp.status in {401, 402, 403, 429} else backoff)
                        backoff = min(60.0, backoff * 2.0)
                        continue
                    backoff = 1.0
                    await self.store.source_result("x:stream", "X filtered stream", _STREAM, ok=True, status="connected", http_status=resp.status, latency_ms=0.0)
                    self.metrics.inc("x_stream_connected")
                    async for raw in resp.content:
                        if stop_event.is_set():
                            return
                        line = raw.strip()
                        if not line:  # documented keep-alive
                            self.metrics.inc("x_stream_keepalive")
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            self.metrics.inc("x_stream_parse_error")
                            continue
                        tw = obj.get("data") or {}
                        users = {str(u.get("id")): u for u in ((obj.get("includes") or {}).get("users") or []) if isinstance(u, dict)}
                        if isinstance(tw, dict):
                            await self._insert_tweet(tw, users, "X_FILTERED_STREAM")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.inc("x_stream_reconnect")
                logger.warning("X filtered stream reconnect error=%s", redact_text(f"{type(exc).__name__}:{exc}"))
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff + random.random())
                except asyncio.TimeoutError:
                    backoff = min(60.0, backoff * 2.0)
                    continue

    async def _recent_loop(self, stop_event: asyncio.Event) -> None:
        queries = list(_TOPIC_RULES)
        index = 0
        while not stop_event.is_set():
            query = queries[index % len(queries)]
            index += 1
            result = await self.http.get(_RECENT, headers=self.headers, params={
                "query": query,
                "max_results": "100",
                "tweet.fields": "created_at,lang,author_id,public_metrics",
                "expansions": "author_id",
                "user.fields": "username,name,public_metrics,verified",
            })
            wait = self.settings.x_recent_search_poll_seconds
            if result.ok:
                try:
                    obj = json.loads(result.body)
                    users = {str(u.get("id")): u for u in ((obj.get("includes") or {}).get("users") or []) if isinstance(u, dict)}
                    rows = [x for x in (obj.get("data") or []) if isinstance(x, dict)]
                    for tw in rows:
                        await self._insert_tweet(tw, users, "X_RECENT_SEARCH")
                    self.metrics.inc("x_recent_batches")
                    self.metrics.inc("x_recent_rows", len(rows))
                except Exception as exc:
                    self.metrics.inc("x_recent_parse_error")
                    logger.warning("X recent search parse failed error=%s", exc)
            else:
                self.metrics.inc(f"x_recent_{result.classification}")
                wait = max(wait, result.retry_after or 0.0)
                if result.status in {401, 402, 403}:
                    wait = max(wait, 900.0)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait + random.random())
            except asyncio.TimeoutError:
                continue

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.x_enabled or not self.token:
            logger.info("X social plane disabled or X_BEARER_TOKEN missing")
            self.metrics.set("x_state", "disabled_missing_token" if not self.token else "disabled")
            await self.http.close(); await stop_event.wait(); return
        tasks = [asyncio.create_task(self._stream_loop(stop_event), name="x-filtered-stream")]
        if self.settings.x_recent_search_enabled:
            tasks.append(asyncio.create_task(self._recent_loop(stop_event), name="x-recent-search"))
        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.http.close()
