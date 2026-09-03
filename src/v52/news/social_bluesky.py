from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from urllib.parse import urlencode

import aiohttp

from ..config import Settings
from ..loggingx import redact_text
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.news import NewsStore
from .external_common import make_raw_news
from .relevance import fundamental_relevant

logger = logging.getLogger(__name__)


class BlueskyService:
    """AT Protocol Jetstream v2 live tail with persisted cursor and v1 compatibility parsing."""

    cursor_key = "bluesky:jetstream:v2"

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.store = NewsStore(settings)
        self.http = ResilientHttpClient(settings, name="bluesky", concurrency=2, per_host_limit=1, retries=1)

    def _url(self, cursor: str) -> str:
        base = self.settings.bluesky_jetstream_url.rstrip("?")
        params = [("collections", "app.bsky.feed.post"), ("kinds", "commit")]
        if cursor:
            params.append(("cursor", cursor))
        sep = "&" if "?" in base else "?"
        return base + sep + urlencode(params)

    async def _handle_v2(self, obj: dict) -> tuple[str, bool]:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        if not isinstance(payload, dict):
            return "", False
        typ = str(payload.get("$type") or obj.get("$type") or "")
        if "commit" not in typ and payload.get("kind") not in {"commit", None}:
            return str(payload.get("seq") or ""), False
        operation = str(payload.get("operation") or "")
        collection = str(payload.get("collection") or "")
        record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
        if operation and operation not in {"create", "update"}:
            return str(payload.get("seq") or ""), False
        if collection and collection != "app.bsky.feed.post":
            return str(payload.get("seq") or ""), False
        text = str(record.get("text") or "").strip()
        if not text:
            return str(payload.get("seq") or ""), False
        if not fundamental_relevant("SOCIAL_FUNDAMENTAL", text, text):
            self.metrics.inc("bluesky_noise_filtered")
            return str(payload.get("seq") or ""), False
        did = str(payload.get("did") or "")
        rkey = str(payload.get("rkey") or "")
        seq = str(payload.get("seq") or "")
        uri = f"at://{did}/app.bsky.feed.post/{rkey}" if did and rkey else str(payload.get("uri") or "")
        url = f"https://bsky.app/profile/{did}/post/{rkey}" if did and rkey else "https://bsky.app/"
        item = make_raw_news(
            source_key="bluesky",
            source_name="Bluesky Jetstream",
            source_url=self.settings.bluesky_jetstream_url,
            item_url=url,
            title=text[:1000],
            summary=text,
            category="SOCIAL_FUNDAMENTAL",
            region="GLOBAL",
            published_at=record.get("createdAt") or payload.get("time_us"),
            external_id=uri or seq or str(payload.get("cid") or ""),
            priority=6,
            payload={"source_type": "BLUESKY_JETSTREAM", "did": did, "rkey": rkey, "seq": seq, "at_uri": uri},
        )
        if not item:
            return seq, False
        inserted = await self.store.insert_raw(item)
        self.metrics.inc("news_raw_inserted", int(inserted))
        self.metrics.inc("bluesky_fundamental" if inserted else "bluesky_duplicate")
        return seq, inserted

    async def _handle_v1(self, obj: dict) -> tuple[str, bool]:
        if obj.get("kind") != "commit":
            return str(obj.get("time_us") or ""), False
        commit = obj.get("commit") if isinstance(obj.get("commit"), dict) else {}
        record = commit.get("record") if isinstance(commit.get("record"), dict) else {}
        if str(commit.get("operation") or "") not in {"create", "update"} or str(commit.get("collection") or "") != "app.bsky.feed.post":
            return str(obj.get("time_us") or ""), False
        converted = {
            "$type": "message",
            "payload": {
                "$type": "network.bsky.jetstream.subscribeEvents#commit",
                "operation": commit.get("operation"), "collection": commit.get("collection"),
                "record": record, "did": obj.get("did"), "rkey": commit.get("rkey"),
                "cid": commit.get("cid"), "seq": obj.get("time_us"), "time_us": obj.get("time_us"),
            },
        }
        return await self._handle_v2(converted)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.bluesky_enabled:
            logger.info("Bluesky Jetstream disabled")
            await self.http.close(); await stop_event.wait(); return
        cursor = await self.store.get_cursor(self.cursor_key)
        backoff = 1.0
        last_cursor_write = time.monotonic()
        last_seq = cursor
        try:
            while not stop_event.is_set():
                url = self._url(cursor)
                started = time.perf_counter()
                try:
                    timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=75)
                    async with self.http.session.ws_connect(
                        url, protocols=("xrpc.v1.json",), heartbeat=20, receive_timeout=75,
                        max_msg_size=self.settings.bluesky_max_message_bytes, timeout=timeout,
                    ) as ws:
                        self.metrics.inc("bluesky_connected")
                        await self.store.source_result(self.cursor_key, "Bluesky Jetstream", self.settings.bluesky_jetstream_url,
                                                       ok=True, status="connected", http_status=101,
                                                       latency_ms=(time.perf_counter() - started) * 1000.0)
                        backoff = 1.0
                        async for msg in ws:
                            if stop_event.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    obj = json.loads(msg.data)
                                except Exception:
                                    self.metrics.inc("bluesky_parse_error"); continue
                                if isinstance(obj, dict) and "payload" in obj:
                                    seq, _ = await self._handle_v2(obj)
                                elif isinstance(obj, dict):
                                    seq, _ = await self._handle_v1(obj)
                                else:
                                    seq = ""
                                if seq:
                                    last_seq = seq
                                    now = time.monotonic()
                                    if now - last_cursor_write >= self.settings.bluesky_cursor_flush_seconds:
                                        await self.store.set_cursor(self.cursor_key, last_seq)
                                        cursor = last_seq
                                        last_cursor_write = now
                                        self.metrics.inc("bluesky_cursor_persisted")
                            elif msg.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                                break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.metrics.inc("bluesky_reconnect")
                    await self.store.source_result(self.cursor_key, "Bluesky Jetstream", self.settings.bluesky_jetstream_url,
                                                   ok=False, status="stream_error", http_status=None,
                                                   latency_ms=(time.perf_counter() - started) * 1000.0,
                                                   error=redact_text(f"{type(exc).__name__}:{exc}"))
                    logger.warning("Bluesky reconnect error=%s", redact_text(f"{type(exc).__name__}:{exc}"))
                if last_seq:
                    await self.store.set_cursor(self.cursor_key, last_seq)
                    cursor = last_seq
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff + random.random())
                except asyncio.TimeoutError:
                    backoff = min(30.0, backoff * 2.0)
                    continue
        finally:
            if last_seq:
                await self.store.set_cursor(self.cursor_key, last_seq)
            await self.http.close()
