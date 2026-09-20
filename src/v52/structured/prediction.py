from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any

import aiohttp

from ..config import Settings
from ..loggingx import redact_text
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.structured import StructuredStore

logger = logging.getLogger(__name__)

_UNRELATED_MARKET = re.compile(
    r"\b(?:nfl|nba|nhl|mlb|ncaa|premier league|champions league|"
    r"super bowl|world cup|tennis|golf|boxing|ufc|grammy|oscar|"
    r"movie|album|celebrity|football|basketball|baseball|soccer)\b", re.I,
)
_FUNDAMENTAL_MARKET = re.compile(
    r"\b(?:federal reserve|fed (?:rate|chair|cut|hike)|interest rate|"
    r"inflation|cpi|pce|nonfarm|payroll|unemployment|recession|"
    r"gdp|treasury|debt ceiling|tariff|sanction|opec|oil|brent|"
    r"wti|natural gas|bitcoin|ethereum|crypto|spot etf|sec approval|"
    r"central bank|ecb|boe|boj|election|ceasefire|war|"
    r"iran|russia|ukraine|china|taiwan)\b", re.I,
)


def prediction_market_relevant(title: str) -> bool:
    """Conservative admission for financial and geopolitical catalysts."""
    return bool(_FUNDAMENTAL_MARKET.search(title)) and not bool(_UNRELATED_MARKET.search(title))


def polymarket_token_ids(row: dict[str, Any]) -> list[str]:
    value = row.get("clobTokenIds") or row.get("clob_token_ids") or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = [x.strip() for x in value.split(",") if x.strip()]
    return [str(x) for x in value if str(x).strip()] if isinstance(value, list) else []


class PredictionMarketService:
    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.store = StructuredStore(settings)
        self.http = ResilientHttpClient(settings, name="prediction", concurrency=4, per_host_limit=2, retries=2, total_timeout=20.0, max_body_bytes=24_000_000)
        self.tokens: set[str] = set()
        self.token_meta: dict[str, dict[str, Any]] = {}
        self.last_ws_emit: dict[str, float] = {}

    async def _polymarket_rest(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            result = await self.http.get(self.settings.polymarket_gamma_markets_url, params={"closed": "false", "limit": str(self.settings.polymarket_max_markets), "order": "volume24hr", "ascending": "false"})
            if result.ok:
                try:
                    obj = json.loads(result.body)
                    rows = obj if isinstance(obj, list) else (obj.get("markets") if isinstance(obj, dict) else [])
                    rows = rows if isinstance(rows, list) else []
                    snapshots: list[dict[str, Any]] = []
                    for row in rows[: self.settings.polymarket_max_markets]:
                        if not isinstance(row, dict):
                            continue
                        if not prediction_market_relevant(str(row.get("question") or row.get("title") or "")):
                            self.metrics.inc("polymarket_irrelevant_market_filtered")
                            continue
                        market_id = str(row.get("id") or row.get("conditionId") or row.get("slug") or "")
                        tokens = polymarket_token_ids(row)
                        meta = {k: row.get(k) for k in ("id", "question", "slug", "conditionId", "volume24hr", "liquidity", "endDate", "active", "closed") if k in row}
                        for token in tokens:
                            self.tokens.add(token)
                            self.token_meta[token] = {**meta, "market_id": market_id}
                        snapshots.append({**meta, "clobTokenIds": tokens, "outcomes": row.get("outcomes"), "outcomePrices": row.get("outcomePrices")})
                    inserted = await self.store.insert_snapshots("polymarket", "PREDICTION_EVENT_MARKETS", "PREDICTION_MARKET_STATE", snapshots)
                    await self.store.status("polymarket", "ok", result.status)
                    self.metrics.inc("polymarket_rest_batches")
                    self.metrics.inc("polymarket_market_changed", inserted)
                    self.metrics.set("polymarket_tokens", len(self.tokens))
                    wait = self.settings.polymarket_poll_seconds
                except Exception as exc:
                    logger.exception("polymarket parse failure")
                    await self.store.status("polymarket", "parse_error", result.status, redact_text(str(exc)))
                    self.metrics.inc("polymarket_parse_error")
                    wait = max(30.0, self.settings.polymarket_poll_seconds)
            else:
                await self.store.status("polymarket", result.classification, result.status, result.error, result.classification == "rate_limited")
                self.metrics.inc(f"polymarket_{result.classification}")
                wait = max(self.settings.polymarket_poll_seconds, result.retry_after or 0.0)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue

    async def _emit_ws_obj(self, obj: Any) -> None:
        rows = obj if isinstance(obj, list) else [obj]
        payloads: list[dict[str, Any]] = []
        now = time.monotonic()
        for row in rows:
            if not isinstance(row, dict):
                continue
            typ = str(row.get("event_type") or row.get("type") or "event")
            token = str(row.get("asset_id") or row.get("assetId") or row.get("token_id") or "")
            market = str(row.get("market") or row.get("condition_id") or row.get("conditionId") or "")
            key = token or market or typ
            emit_key = f"{typ}:{key}"
            last = self.last_ws_emit.get(emit_key, 0.0)
            if typ in {"book", "price_change", "best_bid_ask"} and (now - last) * 1000.0 < self.settings.polymarket_ws_emit_ms:
                continue
            self.last_ws_emit[emit_key] = now
            bounded = {k: v for k, v in row.items() if k not in {"bids", "asks"}}
            if typ == "book":
                def levels(value: Any, reverse: bool) -> list[dict[str, float]]:
                    out: list[dict[str, float]] = []
                    for x in value if isinstance(value, list) else []:
                        if not isinstance(x, dict):
                            continue
                        try:
                            out.append({"price": float(x.get("price")), "size": float(x.get("size"))})
                        except (TypeError, ValueError):
                            continue
                    out.sort(key=lambda z: z["price"], reverse=reverse)
                    return out[: self.settings.liquidity_depth]
                bounded["bids"] = levels(row.get("bids"), True)
                bounded["asks"] = levels(row.get("asks"), False)
            if token and token in self.token_meta:
                bounded["market_meta"] = self.token_meta[token]
            payloads.append({"event_type": typ, "market": market, "asset_id": token, "data": bounded})
        if payloads:
            inserted = await self.store.insert_snapshots("polymarket_ws", "PREDICTION_EVENT_MARKETS", "PREDICTION_MARKET_REALTIME", payloads)
            self.metrics.inc("polymarket_ws_events", inserted)

    async def _polymarket_ws(self, stop_event: asyncio.Event) -> None:
        backoff = 1.0
        while not stop_event.is_set():
            if not self.tokens:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                continue
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=90)
                async with self.http.session.ws_connect(self.settings.polymarket_market_ws, heartbeat=None, receive_timeout=90, timeout=timeout, max_msg_size=8_000_000) as ws:
                    subscribed: set[str] = set()
                    async def subscribe_new(initial: bool = False) -> None:
                        tokens = list(self.tokens)
                        if self.settings.polymarket_ws_max_tokens > 0:
                            tokens = tokens[: self.settings.polymarket_ws_max_tokens]
                        new = [t for t in tokens if t not in subscribed]
                        for i in range(0, len(new), self.settings.polymarket_ws_subscribe_batch):
                            batch = new[i:i + self.settings.polymarket_ws_subscribe_batch]
                            if not batch:
                                continue
                            await ws.send_json({"assets_ids": batch, "type": "market"} if initial and not subscribed else {"assets_ids": batch, "operation": "subscribe"})
                            subscribed.update(batch)
                            self.metrics.inc("polymarket_ws_tokens_subscribed", len(batch))
                    await subscribe_new(True)
                    backoff = 1.0
                    last_subscribe = time.monotonic()
                    async for msg in ws:
                        if stop_event.is_set():
                            break
                        if time.monotonic() - last_subscribe >= 2.0:
                            await subscribe_new(False)
                            last_subscribe = time.monotonic()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            text = str(msg.data).strip()
                            if not text or text.upper() == "PONG":
                                continue
                            try:
                                await self._emit_ws_obj(json.loads(text))
                            except Exception:
                                self.metrics.inc("polymarket_ws_parse_error")
                                logger.exception("polymarket websocket payload parse failed")
                        elif msg.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.inc("polymarket_ws_reconnect")
                logger.warning("polymarket websocket reconnect error=%s", redact_text(str(exc)))
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff + random.random())
                except asyncio.TimeoutError:
                    continue
                backoff = min(30.0, backoff * 2.0)

    async def _kalshi(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            cursor = ""
            collected = 0
            had_error = False
            while collected < self.settings.kalshi_max_markets and not stop_event.is_set():
                params = {"status": "open", "limit": str(min(self.settings.kalshi_page_limit, self.settings.kalshi_max_markets - collected))}
                if cursor:
                    params["cursor"] = cursor
                result = await self.http.get(self.settings.kalshi_markets_url, params=params)
                if not result.ok:
                    await self.store.status("kalshi", result.classification, result.status, result.error, result.classification == "rate_limited")
                    self.metrics.inc(f"kalshi_{result.classification}")
                    had_error = True
                    break
                try:
                    obj = json.loads(result.body)
                    rows = obj.get("markets", []) if isinstance(obj, dict) else []
                    rows = rows if isinstance(rows, list) else []
                    bounded = []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        if not prediction_market_relevant(" ".join(str(row.get(key) or "") for key in ("title", "subtitle"))):
                            self.metrics.inc("kalshi_irrelevant_market_filtered")
                            continue
                        bounded.append({k: row.get(k) for k in ("ticker", "event_ticker", "title", "subtitle", "status", "yes_bid_dollars", "yes_ask_dollars", "last_price_dollars", "volume_fp", "volume_24h_fp", "open_interest_fp", "liquidity_dollars", "updated_time", "open_time", "close_time", "expiration_time") if k in row})
                    inserted = await self.store.insert_snapshots("kalshi", "PREDICTION_EVENT_MARKETS", "PREDICTION_MARKET_STATE", bounded)
                    self.metrics.inc("kalshi_market_changed", inserted)
                    collected += len(rows)
                    cursor = str(obj.get("cursor") or "") if isinstance(obj, dict) else ""
                    if not rows or not cursor:
                        break
                except Exception as exc:
                    await self.store.status("kalshi", "parse_error", result.status, redact_text(str(exc)))
                    self.metrics.inc("kalshi_parse_error")
                    had_error = True
                    break
            if not had_error:
                await self.store.status("kalshi", "ok", 200)
                self.metrics.inc("kalshi_batches")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.settings.kalshi_poll_seconds)
            except asyncio.TimeoutError:
                continue

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.prediction_enabled:
            logger.info("prediction market plane disabled")
            await self.http.close()
            await stop_event.wait()
            return
        tasks: list[asyncio.Task[Any]] = []
        if self.settings.polymarket_enabled:
            tasks.append(asyncio.create_task(self._polymarket_rest(stop_event), name="prediction:polymarket-rest"))
            if self.settings.polymarket_ws_enabled:
                tasks.append(asyncio.create_task(self._polymarket_ws(stop_event), name="prediction:polymarket-ws"))
        if self.settings.kalshi_enabled:
            tasks.append(asyncio.create_task(self._kalshi(stop_event), name="prediction:kalshi"))
        if not tasks:
            await stop_event.wait()
            await self.http.close()
            return
        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    self.metrics.inc("prediction_task_shutdown_error")
                    logger.error("prediction task shutdown error=%s", redact_text(str(result)))
            await self.http.close()
