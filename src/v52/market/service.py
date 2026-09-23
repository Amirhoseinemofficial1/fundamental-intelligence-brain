from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Callable, Awaitable

import aiohttp

from ..config import Settings
from ..metrics import Metrics
from ..models import MarketEvent, StoryCandidate
from ..storage.control import ControlStore
from ..timeutil import iso_now, parse_datetime, to_iso
from .writer import RawWireJournal, SemanticWriter
from .crypto_universe import CryptoUniverseScanner, base_symbol_from_market

logger = logging.getLogger(__name__)


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def mexc_depth_side(rows: Any) -> dict[float, float]:
    """Normalize MEXC contract depth rows: [price, quantity, order_count]."""
    out: dict[float, float] = {}
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            out.pop(price, None)
        else:
            out[price] = qty
    return out


class MarketRuntime:
    def __init__(self, settings: Settings, metrics: Metrics, service: str, exchange: str):
        self.settings = settings
        self.metrics = metrics
        self.service = service
        self.exchange = exchange
        self.raw = RawWireJournal(settings, metrics, service)
        self.semantic = SemanticWriter(settings, metrics, service)
        self.control = ControlStore(settings)
        connector = aiohttp.TCPConnector(limit=16, limit_per_host=8, ttl_dns_cache=300)
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=20, connect=5, sock_read=15),
            headers={"User-Agent": settings.user_agent},
        )
        self.books: dict[tuple[str, str], dict[str, dict[float, float]]] = {}
        self.last_emit: dict[str, float] = {}
        self.trade_buckets: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {
            "count": 0.0, "buy_qty": 0.0, "sell_qty": 0.0, "buy_notional": 0.0, "sell_notional": 0.0, "unknown_notional": 0.0,
        })
        self.last_trade_emit: dict[tuple[str, str], float] = {}
        self.last_seq: dict[str, int] = {}
        self.recovery_pending: set[str] = set()
        self.sequence_dirty: set[str] = set()
        self.dydx_trade_ids: set[str] = set()
        self.dydx_trade_order: deque[str] = deque()
        self.dydx_offsets: dict[tuple[str, str, str], int] = {}
        self.kraken_dirty: dict[str, str | None] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self.large_trade_usd = float(os.getenv("MARKET_LARGE_TRADE_ALERT_USD", "1000000"))
        self.liquidation_alert_usd = float(os.getenv("MARKET_LIQUIDATION_ALERT_USD", "3000000"))
        self.funding_alert_abs = float(os.getenv("MARKET_FUNDING_ALERT_ABS", "0.001"))
        self.crypto_scanner = CryptoUniverseScanner(settings, self.session)
        self._universe_last_story: dict[str, int] = {}

    async def start_writers(self) -> None:
        states = await asyncio.to_thread(self.semantic.store.load_sequence_states)
        for key, row in states.items():
            seq = row.get("last_sequence")
            if seq is not None:
                self.last_seq[key] = int(seq)
            if row.get("recovery_pending"):
                self.recovery_pending.add(key)
        self.metrics.set("market_recovery_pending", len(self.recovery_pending))
        await self.raw.start()
        await self.semantic.start()

    async def _checkpoint_sequences(self) -> None:
        if not self.sequence_dirty:
            return
        keys = list(self.sequence_dirty)
        states = {k: (self.last_seq.get(k), k in self.recovery_pending) for k in keys}
        await asyncio.to_thread(self.semantic.store.checkpoint_sequences, states)
        for key in keys:
            self.sequence_dirty.discard(key)
        self.metrics.inc("market_sequence_checkpoints", len(keys))

    async def _sequence_checkpoint_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                await self._checkpoint_sequences()

    async def wire(self, venue: str, stream: str, symbol: str, raw: str | bytes, metadata: dict[str, Any] | None = None) -> None:
        await self.raw.append(venue, stream, symbol, raw, metadata)

    async def emit(
        self,
        event_type: str,
        venue: str,
        symbol: str,
        payload: dict[str, Any],
        lane: str,
        exchange_ts: Any = None,
        min_interval: float = 0.0,
    ) -> bool:
        key = f"{event_type}|{venue}|{symbol}"
        now = time.monotonic()
        # Persist a representative state series rather than every websocket
        # delta.  Liquidations remain event-exact; continuously changing book,
        # ticker and flow states are bounded by a server profile floor.
        if event_type != "liquidation":
            min_interval=max(min_interval,float(os.getenv("MARKET_SEMANTIC_MIN_INTERVAL_SECONDS","1")))
        if min_interval > 0 and now - self.last_emit.get(key, 0.0) < min_interval:
            self.metrics.inc("market_semantic_coalesced")
            return False
        self.last_emit[key] = now
        ts = to_iso(parse_datetime(exchange_ts)) if exchange_ts is not None else None
        await self.semantic.append(MarketEvent(event_type, venue, symbol, ts, iso_now(), lane, payload))
        return True

    def observe_sequence(self, key: str, sequence: Any, *, previous: Any = None, strict: bool = False, snapshot: bool = False) -> dict[str, Any]:
        seq = _int(sequence)
        prev = _int(previous)
        last = self.last_seq.get(key)
        gap = False
        nonmonotonic = False
        if seq is not None and last is not None:
            if strict and seq > last + 1:
                gap = True
            if prev is not None and prev != last and strict:
                gap = True
            if seq < last:
                nonmonotonic = True
        if seq is not None and (last is None or seq >= last):
            self.last_seq[key] = seq
            self.sequence_dirty.add(key)
        if gap:
            self.recovery_pending.add(key)
            self.sequence_dirty.add(key)
            self.metrics.inc("market_sequence_gap")
        if nonmonotonic:
            self.metrics.inc("market_sequence_nonmonotonic")
        if snapshot and key in self.recovery_pending:
            self.recovery_pending.discard(key)
            self.sequence_dirty.add(key)
            self.metrics.inc("market_recovery_snapshot")
        self.metrics.set("market_recovery_pending", len(self.recovery_pending))
        return {"last": last, "sequence": seq, "gap": gap, "nonmonotonic": nonmonotonic}

    def mark_reconnect(self, venue: str) -> None:
        key = f"provider:{venue}"
        self.recovery_pending.add(key)
        self.metrics.inc("market_ws_reconnect")
        self.metrics.set("market_recovery_pending", len(self.recovery_pending))

    def clear_reconnect(self, venue: str) -> None:
        key = f"provider:{venue}"
        if key in self.recovery_pending:
            self.recovery_pending.discard(key)
            self.metrics.inc("market_reconnect_recovered")
        self.metrics.set("market_recovery_pending", len(self.recovery_pending))

    def _book(self, venue: str, symbol: str) -> dict[str, dict[float, float]]:
        return self.books.setdefault((venue, symbol), {"bids": {}, "asks": {}})

    @staticmethod
    def _set_level(book: dict[float, float], price: Any, qty: Any) -> None:
        try:
            p, q = float(price), float(qty)
        except (TypeError, ValueError):
            return
        if q <= 0:
            book.pop(p, None)
        else:
            book[p] = q

    @classmethod
    def _update_rows(cls, book: dict[float, float], rows: Any, price_idx: int = 0, qty_idx: int = 1) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if isinstance(row, dict):
                cls._set_level(book, row.get("price") or row.get("px") or row.get("price_level"), row.get("size") or row.get("sz") or row.get("qty") or row.get("new_quantity"))
            elif isinstance(row, (list, tuple)) and len(row) > max(price_idx, qty_idx):
                cls._set_level(book, row[price_idx], row[qty_idx])

    async def emit_book(self, venue: str, symbol: str, bids: dict[float, float], asks: dict[float, float], exchange_ts: Any = None, cadence: str = "push") -> None:
        depth = self.settings.liquidity_depth
        bb = sorted(bids.items(), reverse=True)[:depth]
        aa = sorted(asks.items())[:depth]
        if not bb or not aa:
            return
        bid_notional = sum(p * q for p, q in bb)
        ask_notional = sum(p * q for p, q in aa)
        den = bid_notional + ask_notional
        mid = (bb[0][0] + aa[0][0]) / 2.0
        stored_levels=max(0,min(depth,int(os.getenv("MARKET_STORED_BOOK_LEVELS","3"))))
        payload = {
            "best_bid": bb[0][0], "best_ask": aa[0][0], "mid": mid,
            "spread_bps": ((aa[0][0] - bb[0][0]) / mid * 10000.0) if mid else None,
            "bid_notional_depth": bid_notional, "ask_notional_depth": ask_notional,
            "book_imbalance": (bid_notional - ask_notional) / den if den else 0.0,
            "depth_levels": depth,
            "top_bids": bb[:stored_levels],
            "top_asks": aa[:stored_levels],
            "cadence": cadence,
        }
        await self.emit("liquidity_depth", venue, symbol, payload, "LIQUIDITY", exchange_ts, min_interval=float(os.getenv("MARKET_BOOK_EMIT_SECONDS", "0.5")))

    def _side(self, raw: Any, invert: bool = False) -> str:
        s = str(raw or "").upper()
        if s in {"1", "BUY", "B", "BID"}:
            side = "BUY"
        elif s in {"2", "SELL", "S", "A", "ASK"}:
            side = "SELL"
        else:
            side = "UNKNOWN"
        if invert:
            side = "SELL" if side == "BUY" else "BUY" if side == "SELL" else side
        return side

    async def trade_flow(self, venue: str, symbol: str, rows: list[dict[str, Any]], side_field: str, invert: bool = False, exchange_ts: Any = None, cadence: str = "trades") -> None:
        key = (venue, symbol)
        b = self.trade_buckets[key]
        for row in rows:
            px = _float(row.get("price") or row.get("px") or row.get("p"))
            qty = _float(row.get("size") or row.get("sz") or row.get("qty") or row.get("q") or row.get("v"))
            side = self._side(row.get(side_field), invert=invert)
            notional = abs(px * qty)
            b["count"] += 1
            if side == "BUY":
                b["buy_qty"] += qty
                b["buy_notional"] += notional
            elif side == "SELL":
                b["sell_qty"] += qty
                b["sell_notional"] += notional
            else:
                b["unknown_notional"] += notional
            if self.large_trade_usd > 0 and notional >= self.large_trade_usd:
                event_id = row.get("id") or row.get("a") or row.get("trade_id") or row.get("T") or time.time_ns()
                await self._market_alert(
                    f"large:{venue}:{symbol}:{event_id}", 8,
                    {"kind": "market_large_trade", "event_type": "LARGE TRADE", "venue": venue, "symbol": symbol,
                     "headline": f"Large {side} trade {venue} {symbol}",
                     "details": {"side": side, "price": px, "qty": qty, "notional_usd": round(notional, 2)}},
                )
        now = time.monotonic()
        interval = float(os.getenv("MARKET_TRADE_FLOW_EMIT_SECONDS", "0.5"))
        if now - self.last_trade_emit.get(key, 0.0) >= interval and b["count"]:
            total = b["buy_notional"] + b["sell_notional"]
            payload = dict(b)
            payload["aggressor_imbalance"] = (b["buy_notional"] - b["sell_notional"]) / total if total else 0.0
            payload["aggregation_seconds"] = interval
            await self.emit("trade_flow", venue, symbol, payload, "FLOW_VOLUME", exchange_ts, 0.0)
            self.trade_buckets[key] = {"count": 0.0, "buy_qty": 0.0, "sell_qty": 0.0, "buy_notional": 0.0, "sell_notional": 0.0, "unknown_notional": 0.0}
            self.last_trade_emit[key] = now

    async def liquidation(self, venue: str, symbol: str, payload: dict[str, Any], exchange_ts: Any = None) -> None:
        await self.emit("liquidation", venue, symbol, payload, "LIQUIDATION", exchange_ts, 0.0)
        notional = _float(payload.get("notional_usd"))
        if notional <= 0:
            price = _float(payload.get("price") or payload.get("p"))
            qty = _float(payload.get("size") or payload.get("qty") or payload.get("v"))
            notional = abs(price * qty)
        if self.liquidation_alert_usd > 0 and notional >= self.liquidation_alert_usd:
            h = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
            await self._market_alert(
                f"liq:{venue}:{symbol}:{h}", 10,
                {"kind": "market_liquidation", "event_type": "LIQUIDATION", "venue": venue, "symbol": symbol,
                 "headline": f"Large liquidation {venue} {symbol}", "details": {**payload, "notional_usd": round(notional, 2)}},
            )

    async def funding(self, venue: str, symbol: str, payload: dict[str, Any], exchange_ts: Any = None) -> None:
        await self.emit("funding_rate", venue, symbol, payload, "DERIVATIVES", exchange_ts, 0.25)
        rate = _float(payload.get("fundingRate") or payload.get("funding_rate") or payload.get("r") or payload.get("funding"))
        if self.funding_alert_abs > 0 and abs(rate) >= self.funding_alert_abs:
            minute = int(time.time() // 60)
            await self._market_alert(
                f"funding:{venue}:{symbol}:{minute}", 7,
                {"kind": "market_funding", "event_type": "FUNDING EXTREME", "venue": venue, "symbol": symbol,
                 "headline": f"Extreme funding {venue} {symbol}", "details": {"funding_rate": rate, **payload}},
            )

    async def _market_alert(self, client_key: str, priority: int, payload: dict[str, Any]) -> None:
        # Market anomalies become durable Brain inputs, but never bypass the
        # intelligence/live qualification gate into Telegram.
        if not self.settings.market_crypto_anomaly_enabled:
            self.metrics.inc("market_alert_suppressed_intelligence_only")
            return
        symbol=str(payload.get('symbol') or '')
        base=base_symbol_from_market(symbol)
        headline=str(payload.get('headline') or payload.get('event_type') or 'Crypto market anomaly')
        summary=json.dumps({'token_symbol':base,'market_symbol':symbol,'venue':payload.get('venue'),'event_type':payload.get('event_type'),'details':payload.get('details') or {}},ensure_ascii=False,default=str)
        now=iso_now();fp=hashlib.sha256(f"market-brain:{client_key}".encode()).hexdigest()
        raw_id=int(fp[:14],16)
        candidate=StoryCandidate(fp,headline,summary,f"market://{payload.get('venue') or 'crypto'}/{symbol}",'CRYPTO','GLOBAL','en',now,now,max(3,min(10,int(priority))),['market_anomaly','internal_market_plane'],str(payload.get('venue') or 'market'),f"market://{payload.get('venue') or 'crypto'}",raw_id)
        try:
            _story_id,is_new=await self.control.record_story(candidate,candidate.canonical_url,False,None,0,None,False)
            self.metrics.inc('market_anomaly_brain_new' if is_new else 'market_anomaly_brain_seen')
        except Exception:
            self.metrics.inc('market_anomaly_brain_error');logger.exception('market anomaly brain handoff failed symbol=%s',symbol)

    async def _run_stream_with_rotation(self, venue: str, runner: Callable[[], Awaitable[None]], stop_event: asyncio.Event) -> bool:
        """Run one stream session; return True when it was intentionally rotated."""
        dynamic={'BYBIT','OKX','BINANCE_FUTURES','MEXC_FUTURES','DYDX','COINBASE','KRAKEN_FUTURES','HYPERLIQUID'}
        rotate=float(self.settings.market_crypto_stream_rotate_seconds) if self.settings.market_crypto_universe_enabled and venue in dynamic else 0.0
        if rotate<=0:
            await runner();return False
        stream=asyncio.create_task(runner(),name=f'{self.service}:rotating-stream')
        timer=asyncio.create_task(asyncio.sleep(rotate),name=f'{self.service}:rotation-timer')
        stopper=asyncio.create_task(stop_event.wait(),name=f'{self.service}:rotation-stop')
        try:
            done,_=await asyncio.wait((stream,timer,stopper),return_when=asyncio.FIRST_COMPLETED)
            if stream in done:
                timer.cancel();stopper.cancel();await asyncio.gather(timer,stopper,return_exceptions=True);await stream;return False
            if stopper in done:
                stream.cancel();timer.cancel();await asyncio.gather(stream,timer,return_exceptions=True);return False
            stream.cancel();stopper.cancel();await asyncio.gather(stream,stopper,return_exceptions=True)
            self.metrics.inc('crypto_stream_planned_rotation');logger.info('planned crypto stream rotation venue=%s seconds=%s',venue,rotate)
            return True
        finally:
            for task in (stream,timer,stopper):
                if not task.done():task.cancel()

    async def reconnect_loop(self, venue: str, runner: Callable[[], Awaitable[None]], stop_event: asyncio.Event) -> None:
        backoff = 1.0
        while not stop_event.is_set():
            try:
                rotated=await self._run_stream_with_rotation(venue,runner,stop_event)
                if stop_event.is_set():
                    return
                self.mark_reconnect(venue)
                self.metrics.inc("market_ws_planned_rotation" if rotated else "market_ws_clean_disconnect")
                logger.warning("market stream ended without exception venue=%s; reconnecting", venue)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0 + random.random())
                except asyncio.TimeoutError:
                    logger.debug("market reconnect pause elapsed venue=%s", venue)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.mark_reconnect(venue)
                self.metrics.inc(f"market_{venue.lower()}_error")
                logger.warning("market reconnect venue=%s error=%s", venue, exc, exc_info=True)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff + random.random())
                except asyncio.TimeoutError:
                    logger.debug("market reconnect backoff elapsed venue=%s", venue)
                backoff = min(30.0, backoff * 2.0)

    async def run_exchange(self, stop_event: asyncio.Event) -> None:
        if not self.settings.market_enabled:
            logger.info("market disabled service=%s", self.service)
            await self.session.close()
            await stop_event.wait()
            return
        await self.start_writers()
        checkpoint_task = asyncio.create_task(self._sequence_checkpoint_loop(stop_event), name=f"{self.service}:sequence-checkpoint")
        self._tasks.append(checkpoint_task)
        mapping: dict[str, tuple[str, Callable[[asyncio.Event], Awaitable[None]]]] = {
            "bybit": ("BYBIT", self.bybit),
            "okx": ("OKX", self.okx),
            "deribit": ("DERIBIT", self.deribit),
            "binance": ("BINANCE_FUTURES", self.binance),
            "mexc": ("MEXC_FUTURES", self.mexc),
            "dydx": ("DYDX", self.dydx),
            "coinbase": ("COINBASE", self.coinbase),
            "kraken": ("KRAKEN_FUTURES", self.kraken),
            "hyperliquid": ("HYPERLIQUID", self.hyperliquid),
            "core": ("MARKET_CORE", self.market_core),
        }
        if self.exchange not in mapping:
            raise ValueError(f"unknown market exchange {self.exchange}")
        venue, fn = mapping[self.exchange]
        network_task = asyncio.create_task(self.reconnect_loop(venue, lambda: fn(stop_event), stop_event), name=f"{self.service}:network")
        self._tasks.append(network_task)
        stop_task = asyncio.create_task(stop_event.wait(), name=f"{self.service}:stop-wait")
        try:
            watched = [stop_task, *self._tasks]
            if self.raw.task is not None:
                watched.append(self.raw.task)
            if self.semantic.task is not None:
                watched.append(self.semantic.task)
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            if stop_task not in done:
                failed = next(iter(done))
                exc = None if failed.cancelled() else failed.exception()
                self.metrics.inc("market_internal_task_fatal")
                raise RuntimeError(f"market internal task terminated unexpectedly task={failed.get_name()} error={exc}") from exc
        finally:
            stop_task.cancel()
            # Producer-first shutdown: no network task may call wire()/emit() after writer sentinels.
            for t in self._tasks:
                t.cancel()
            results = await asyncio.gather(*self._tasks, stop_task, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    logger.error("market task ended with error service=%s error=%s", self.service, result)
            # Persist the last observed sequence and unresolved recovery state before writer shutdown.
            await self._checkpoint_sequences()
            # Flush dirty Kraken book before closing semantic writer.
            if self.exchange == "kraken":
                for sym, ts in list(self.kraken_dirty.items()):
                    b = self._book("KRAKEN_FUTURES", sym)
                    await self.emit_book("KRAKEN_FUTURES", sym, b["bids"], b["asks"], ts, "final dirty book")
            await self.semantic.stop()
            await self.raw.stop()
            await self.session.close()
            boundary = {k for k in self.recovery_pending if k.startswith("provider:")}
            if boundary:
                self.metrics.set("market_shutdown_reconnect_boundary", len(boundary))
                self.recovery_pending.difference_update(boundary)
            self.metrics.set("market_recovery_pending", len(self.recovery_pending))
            self.metrics.set("market_shutdown_complete", 1)

    async def _crypto_stream_symbols(self, venue: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
        if not self.settings.market_crypto_universe_enabled:
            return fallback
        rows = await self.crypto_scanner.stream_symbols(venue, fallback, self.settings.market_crypto_stream_limit)
        self.metrics.set(f"crypto_stream_symbols_{venue}", len(rows))
        return rows

    async def bybit(self, stop_event: asyncio.Event) -> None:
        url = os.getenv("BYBIT_WS", "wss://stream.bybit.com/v5/public/linear")
        symbols=await self._crypto_stream_symbols('bybit',self.settings.market_symbols)
        async with self.session.ws_connect(url, heartbeat=20, receive_timeout=45, max_msg_size=12_000_000) as ws:
            args: list[str] = []
            for sym in symbols:
                args.extend([f"tickers.{sym}", f"allLiquidation.{sym}", f"orderbook.{self.settings.bybit_orderbook_depth}.{sym}"])
            # Keep subscription frames well below Bybit's aggregate args-length limit.
            for i in range(0,len(args),100):
                await ws.send_json({"op":"subscribe","args":args[i:i+100]})
                if i: await asyncio.sleep(0.05)
            self.clear_reconnect("BYBIT")
            self.metrics.inc("market_ws_connected")
            async for msg in ws:
                if stop_event.is_set():
                    return
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                await self.wire("BYBIT", "wire", "GLOBAL", msg.data)
                obj = json.loads(msg.data)
                topic = str(obj.get("topic") or "")
                if topic.startswith("tickers."):
                    d = obj.get("data") or {}
                    d = d[0] if isinstance(d, list) and d else d
                    if not isinstance(d, dict):
                        continue
                    sym = str(d.get("symbol") or topic.split(".", 1)[1])
                    self.clear_reconnect("BYBIT")
                    payload = {k: d.get(k) for k in ("lastPrice", "markPrice", "indexPrice", "openInterest", "openInterestValue", "fundingRate", "nextFundingTime", "volume24h", "turnover24h", "price24hPcnt") if k in d}
                    payload["exchange_ts_ms"] = obj.get("ts")
                    await self.emit("derivatives_state", "BYBIT", sym, payload, "DERIVATIVES", obj.get("ts"), 0.25)
                    if "fundingRate" in d:
                        await self.funding("BYBIT", sym, {"fundingRate": d.get("fundingRate"), "nextFundingTime": d.get("nextFundingTime")}, obj.get("ts"))
                elif topic.startswith("allLiquidation."):
                    rows = obj.get("data") or []
                    rows = [rows] if isinstance(rows, dict) else rows
                    for d in rows:
                        if not isinstance(d, dict):
                            continue
                        sym = str(d.get("s") or topic.split(".", 1)[1])
                        px, qty = _float(d.get("p")), _float(d.get("v"))
                        await self.liquidation("BYBIT", sym, {"side": d.get("S"), "size": d.get("v"), "price": d.get("p"), "notional_usd": abs(px * qty), "exchange_ts_ms": d.get("T")}, d.get("T"))
                elif topic.startswith("orderbook."):
                    d = obj.get("data") or {}
                    if not isinstance(d, dict):
                        continue
                    sym = topic.split(".")[-1]
                    typ = str(obj.get("type") or "")
                    book = self._book("BYBIT", sym)
                    if typ == "snapshot":
                        book["bids"].clear(); book["asks"].clear(); self.clear_reconnect("BYBIT")
                    self._update_rows(book["bids"], d.get("b") or [])
                    self._update_rows(book["asks"], d.get("a") or [])
                    await self.emit_book("BYBIT", sym, book["bids"], book["asks"], obj.get("ts"), "orderbook push")

    async def okx(self, stop_event: asyncio.Event) -> None:
        url = os.getenv("OKX_PUBLIC_WS", "wss://ws.okx.com:8443/ws/v5/public")
        instruments=await self._crypto_stream_symbols('okx',self.settings.okx_instruments)
        async with self.session.ws_connect(url, heartbeat=20, receive_timeout=45, max_msg_size=12_000_000) as ws:
            args = []
            for inst in instruments:
                args.extend([
                    {"channel": "open-interest", "instId": inst},
                    {"channel": "funding-rate", "instId": inst},
                    {"channel": "mark-price", "instId": inst},
                ])
            args.append({"channel": "liquidation-orders", "instType": "SWAP"})
            # OKX caps total subscription payload size; bounded batches are safer
            # for a dynamic universe and also reduce reconnect burst pressure.
            for i in range(0,len(args),100):
                await ws.send_json({"op":"subscribe","args":args[i:i+100]})
                if i: await asyncio.sleep(0.05)
            self.clear_reconnect("OKX")
            async for msg in ws:
                if stop_event.is_set(): return
                if msg.type != aiohttp.WSMsgType.TEXT: continue
                await self.wire("OKX", "wire", "GLOBAL", msg.data)
                obj = json.loads(msg.data)
                if obj.get("event") == "error":
                    self.metrics.inc("market_subscription_error")
                    logger.warning("OKX subscription error %s", obj.get("msg"))
                    continue
                arg = obj.get("arg") or {}
                ch = str(arg.get("channel") or "")
                rows = obj.get("data") or []
                for d in rows if isinstance(rows, list) else [rows]:
                    if not isinstance(d, dict): continue
                    inst = str(d.get("instId") or arg.get("instId") or arg.get("instFamily") or "SWAP")
                    ts = d.get("ts") or d.get("uTime")
                    self.clear_reconnect("OKX")
                    if ch == "open-interest":
                        await self.emit("open_interest", "OKX", inst, d, "DERIVATIVES", ts, 0.25)
                    elif ch == "funding-rate":
                        await self.funding("OKX", inst, d, ts)
                    elif ch == "mark-price":
                        await self.emit("mark_price", "OKX", inst, d, "DERIVATIVES", ts, 0.25)
                    elif ch == "liquidation-orders":
                        # OKX payload format can nest details; preserve raw normalized dict and estimate notional when fields exist.
                        px = _float(d.get("bkPx") or d.get("px")); qty = _float(d.get("sz") or d.get("qty"))
                        d2 = dict(d); d2["notional_usd"] = abs(px * qty)
                        await self.liquidation("OKX", inst, d2, ts)

    async def deribit(self, stop_event: asyncio.Event) -> None:
        url = os.getenv("DERIBIT_WS", "wss://www.deribit.com/ws/api/v2")
        async with self.session.ws_connect(url, heartbeat=20, receive_timeout=45, max_msg_size=12_000_000) as ws:
            interval = os.getenv("DERIBIT_TICK_INTERVAL", "100ms")
            channels = [f"ticker.{x}.{interval}" for x in self.settings.deribit_instruments]
            await ws.send_json({"jsonrpc": "2.0", "method": "public/subscribe", "id": 42, "params": {"channels": channels}})
            self.clear_reconnect("DERIBIT")
            async for msg in ws:
                if stop_event.is_set(): return
                if msg.type != aiohttp.WSMsgType.TEXT: continue
                await self.wire("DERIBIT", "wire", "GLOBAL", msg.data)
                obj = json.loads(msg.data); params = obj.get("params") or {}; ch = str(params.get("channel") or "")
                if not ch.startswith("ticker."): continue
                d = params.get("data") or {}
                if not isinstance(d, dict): continue
                inst = str(d.get("instrument_name") or ch.split(".")[1])
                self.clear_reconnect("DERIBIT")
                kind = "options_state" if any(k in d for k in ("mark_iv", "greeks", "bid_iv", "ask_iv")) else "derivatives_state"
                lane = "OPTIONS" if kind == "options_state" else "DERIVATIVES"
                await self.emit(kind, "DERIBIT", inst, d, lane, d.get("timestamp"), 0.10)
                if "current_funding" in d:
                    await self.funding("DERIBIT", inst, {"funding_rate": d.get("current_funding"), "funding_8h": d.get("funding_8h")}, d.get("timestamp"))

    async def binance(self, stop_event: asyncio.Event) -> None:
        public_url = os.getenv("BINANCE_FUTURES_PUBLIC_WS", "wss://fstream.binance.com/public/stream")
        market_url = os.getenv("BINANCE_FUTURES_MARKET_WS", "wss://fstream.binance.com/market/stream")
        symbols=await self._crypto_stream_symbols('binance',self.settings.binance_symbols)
        async def public_loop() -> None:
            async with self.session.ws_connect(public_url, heartbeat=20, receive_timeout=60, max_msg_size=12_000_000) as ws:
                params = []
                speed = os.getenv("BINANCE_DEPTH_SPEED", "500ms")
                if speed not in {"100ms", "500ms"}: speed = "500ms"
                depth = 20 if self.settings.liquidity_depth not in {5, 10, 20} else self.settings.liquidity_depth
                for sym in symbols:
                    lo = sym.lower(); params.extend([f"{lo}@bookTicker", f"{lo}@depth{depth}@{speed}"])
                for i in range(0,len(params),100):
                    await ws.send_json({"method":"SUBSCRIBE","params":params[i:i+100],"id":uuid.uuid4().hex})
                    if i: await asyncio.sleep(0.05)
                self.clear_reconnect("BINANCE_FUTURES")
                async for msg in ws:
                    if stop_event.is_set(): return
                    if msg.type != aiohttp.WSMsgType.TEXT: continue
                    await self.wire("BINANCE_FUTURES", "public", "GLOBAL", msg.data)
                    obj = json.loads(msg.data); d = obj.get("data") if isinstance(obj.get("data"), dict) else obj
                    if not isinstance(d, dict): continue
                    event = str(d.get("e") or ""); sym = str(d.get("s") or d.get("ps") or "")
                    if not sym: continue
                    ts = d.get("E") or d.get("T")
                    if event == "bookTicker":
                        await self.emit("best_bid_ask", "BINANCE_FUTURES", sym, {"best_bid": d.get("b"), "bid_qty": d.get("B"), "best_ask": d.get("a"), "ask_qty": d.get("A"), "update_id": d.get("u")}, "LIQUIDITY", ts, 0.10)
                    elif event == "depthUpdate":
                        bids = {float(x[0]): float(x[1]) for x in (d.get("b") or []) if isinstance(x, (list, tuple)) and len(x) >= 2}
                        asks = {float(x[0]): float(x[1]) for x in (d.get("a") or []) if isinstance(x, (list, tuple)) and len(x) >= 2}
                        await self.emit_book("BINANCE_FUTURES", sym, bids, asks, ts, f"top-{depth} {speed}")
        async def market_loop() -> None:
            async with self.session.ws_connect(market_url, heartbeat=20, receive_timeout=60, max_msg_size=12_000_000) as ws:
                params = ["!forceOrder@arr"]
                for sym in symbols:
                    lo = sym.lower(); params.append(f"{lo}@markPrice@1s")
                    if self.settings.binance_aggtrade_enabled: params.append(f"{lo}@aggTrade")
                for i in range(0,len(params),100):
                    await ws.send_json({"method":"SUBSCRIBE","params":params[i:i+100],"id":uuid.uuid4().hex})
                    if i: await asyncio.sleep(0.05)
                self.clear_reconnect("BINANCE_FUTURES")
                async for msg in ws:
                    if stop_event.is_set(): return
                    if msg.type != aiohttp.WSMsgType.TEXT: continue
                    await self.wire("BINANCE_FUTURES", "market", "GLOBAL", msg.data)
                    obj = json.loads(msg.data); d = obj.get("data") if isinstance(obj.get("data"), dict) else obj
                    rows = d if isinstance(d, list) else [d]
                    for item in rows:
                        if not isinstance(item, dict): continue
                        event = str(item.get("e") or ""); sym = str(item.get("s") or item.get("ps") or "GLOBAL")
                        ts = item.get("E") or item.get("T")
                        if event == "aggTrade":
                            obs = self.observe_sequence(f"BINANCE_FUTURES:aggTrade:{sym}", item.get("a"), strict=True)
                            seq_key = f"BINANCE_FUTURES:aggTrade:{sym}"
                            if obs["gap"] and obs["last"] is not None and obs["sequence"] is not None:
                                if await self._binance_backfill(sym, int(obs["last"]) + 1, int(obs["sequence"]) - 1):
                                    self.recovery_pending.discard(seq_key)
                                    self.sequence_dirty.add(seq_key)
                                    self.metrics.inc("market_sequence_recovered")
                            elif seq_key in self.recovery_pending and obs["last"] is not None and obs["sequence"] == int(obs["last"]) + 1:
                                self.recovery_pending.discard(seq_key)
                                self.sequence_dirty.add(seq_key)
                                self.metrics.inc("market_sequence_contiguous_recovery")
                            self.metrics.set("market_recovery_pending", len(self.recovery_pending))
                            row = dict(item); row["aggressor_side"] = "SELL" if bool(item.get("m")) else "BUY"
                            await self.trade_flow("BINANCE_FUTURES", sym, [row], "aggressor_side", False, ts, "aggTrade")
                        elif event == "markPriceUpdate":
                            await self.emit("derivatives_state", "BINANCE_FUTURES", str(item.get("s") or "UNKNOWN"), item, "DERIVATIVES", ts, 1.0)
                            await self.funding("BINANCE_FUTURES", str(item.get("s") or "UNKNOWN"), {"fundingRate": item.get("r"), "nextFundingTime": item.get("T")}, ts)
                        elif event == "forceOrder":
                            order = item.get("o") if isinstance(item.get("o"), dict) else {}
                            sym2 = str(order.get("s") or item.get("ps") or "UNKNOWN")
                            px = _float(order.get("ap") or order.get("p")); qty = _float(order.get("l") or order.get("z") or order.get("q"))
                            await self.liquidation("BINANCE_FUTURES", sym2, {"order": order, "side": order.get("S"), "price": px, "qty": qty, "notional_usd": abs(px * qty), "event_time": item.get("E")}, ts)
        tasks = [asyncio.create_task(public_loop(), name="binance-public"), asyncio.create_task(market_loop(), name="binance-market")]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks: t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _binance_backfill(self, sym: str, start_id: int, end_id: int) -> bool:
        if end_id < start_id: return True
        max_rows = int(os.getenv("BINANCE_AGGTRADE_BACKFILL_MAX", "10000"))
        if end_id - start_id + 1 > max_rows:
            self.metrics.inc("market_binance_gap_too_large"); return False
        base = os.getenv("BINANCE_FUTURES_REST", "https://fapi.binance.com").rstrip("/")
        cur = start_id
        while cur <= end_id:
            limit = min(1000, end_id - cur + 1)
            async with self.session.get(f"{base}/fapi/v1/aggTrades", params={"symbol": sym, "fromId": cur, "limit": limit}, timeout=aiohttp.ClientTimeout(total=8, connect=3, sock_read=6)) as r:
                body = await r.text(errors="replace")
                await self.wire("BINANCE_FUTURES", "aggTrade_backfill", sym, body, {"from_id": cur, "limit": limit, "status": r.status})
                if r.status != 200: return False
                rows = json.loads(body)
            if not isinstance(rows, list) or not rows: return False
            usable = []
            for item in rows:
                if not isinstance(item, dict): continue
                aid = _int(item.get("a"))
                if aid is None or aid < cur: continue
                if aid > end_id: break
                row = dict(item); row["aggressor_side"] = "SELL" if bool(item.get("m")) else "BUY"; usable.append(row)
            ids = [_int(x.get("a")) for x in usable]
            if not usable or ids != list(range(cur, cur + len(ids))): return False
            await self.trade_flow("BINANCE_FUTURES", sym, usable, "aggressor_side", False, usable[-1].get("T"), "REST gap recovery")
            cur = int(ids[-1]) + 1
            self.metrics.inc("market_binance_gap_backfill_rows", len(usable))
        return cur == end_id + 1

    async def mexc(self, stop_event: asyncio.Event) -> None:
        url = os.getenv("MEXC_FUTURES_WS", "wss://contract.mexc.com/edge")
        async with self.session.ws_connect(url, heartbeat=20, receive_timeout=60, max_msg_size=12_000_000) as ws:
            symbols=await self._crypto_stream_symbols('mexc',self.settings.mexc_symbols)
            for idx,sym in enumerate(symbols,1):
                for method, param in (("sub.ticker", {"symbol": sym}), ("sub.depth.full", {"symbol": sym, "limit": 20}), ("sub.deal", {"symbol": sym}), ("sub.funding.rate", {"symbol": sym})):
                    await ws.send_json({"method": method, "param": param})
                if idx%20==0: await asyncio.sleep(0.15)
            async def pinger() -> None:
                while not stop_event.is_set():
                    await asyncio.sleep(15); await ws.send_json({"method": "ping"})
            pt = asyncio.create_task(pinger(), name="mexc-ping")
            self.clear_reconnect("MEXC_FUTURES")
            try:
                async for msg in ws:
                    if stop_event.is_set(): return
                    if msg.type != aiohttp.WSMsgType.TEXT: continue
                    await self.wire("MEXC_FUTURES", "wire", "GLOBAL", msg.data)
                    obj = json.loads(msg.data); ch = str(obj.get("channel") or ""); d = obj.get("data")
                    sym = str(obj.get("symbol") or (d.get("symbol") if isinstance(d, dict) else "") or "UNKNOWN")
                    ts = obj.get("ts") or (d.get("timestamp") if isinstance(d, dict) else None)
                    if ch == "push.ticker" and isinstance(d, dict):
                        self.clear_reconnect("MEXC_FUTURES")
                        await self.emit("derivatives_state", "MEXC_FUTURES", sym, d, "DERIVATIVES", ts, 0.25)
                    elif ch == "push.depth" and isinstance(d, dict):
                        self.clear_reconnect("MEXC_FUTURES")
                        bids = mexc_depth_side(d.get("bids"))
                        asks = mexc_depth_side(d.get("asks"))
                        await self.emit_book("MEXC_FUTURES", sym, bids, asks, ts, "full depth push")
                    elif ch == "push.deal":
                        rows = d if isinstance(d, list) else [d] if isinstance(d, dict) else []
                        await self.trade_flow("MEXC_FUTURES", sym, [x for x in rows if isinstance(x, dict)], "T", False, ts, "transaction push")
                    elif ch == "push.funding.rate" and isinstance(d, dict):
                        await self.funding("MEXC_FUTURES", sym, d, ts)
            finally:
                pt.cancel(); await asyncio.gather(pt, return_exceptions=True)

    async def dydx(self, stop_event: asyncio.Event) -> None:
        url = os.getenv("DYDX_WS", "wss://indexer.dydx.trade/v4/ws")
        async with self.session.ws_connect(url, heartbeat=20, receive_timeout=50, max_msg_size=12_000_000) as ws:
            await ws.send_json({"type": "subscribe", "channel": "v4_markets", "batched": False})
            symbols=await self._crypto_stream_symbols('dydx',self.settings.dydx_markets)
            for idx,sym in enumerate(symbols,1):
                if self.settings.dydx_hf_enabled:
                    await ws.send_json({"type": "subscribe", "channel": "v4_trades", "id": sym, "batched": False})
                    await ws.send_json({"type": "subscribe", "channel": "v4_orderbook", "id": sym, "batched": False})
                    if idx%20==0: await asyncio.sleep(0.15)
            self.clear_reconnect("DYDX")
            async for msg in ws:
                if stop_event.is_set(): return
                if msg.type != aiohttp.WSMsgType.TEXT: continue
                await self.wire("DYDX", "wire", "GLOBAL", msg.data)
                obj = json.loads(msg.data); ch = str(obj.get("channel") or ""); sym = str(obj.get("id") or "GLOBAL"); contents = obj.get("contents")
                if ch == "v4_markets" and isinstance(contents, (dict, list)):
                    self.clear_reconnect("DYDX")
                    await self.emit("derivatives_universe", "DYDX", "GLOBAL", {"contents": contents, "message_id": obj.get("message_id")}, "DERIVATIVES", None, 0.25)
                elif ch == "v4_trades" and isinstance(contents, dict):
                    rows = contents.get("trades") or contents.get("fills") or []; fresh = []
                    for x in rows if isinstance(rows, list) else []:
                        if not isinstance(x, dict): continue
                        tid = str(x.get("id") or "")
                        if tid and tid in self.dydx_trade_ids:
                            self.metrics.inc("market_dydx_duplicate_trade_skipped"); continue
                        if tid:
                            self.dydx_trade_ids.add(tid); self.dydx_trade_order.append(tid)
                            while len(self.dydx_trade_order) > 100_000:
                                self.dydx_trade_ids.discard(self.dydx_trade_order.popleft())
                        fresh.append(x)
                    if fresh: await self.trade_flow("DYDX", sym, fresh, "side", False, None, "Indexer trades")
                elif ch == "v4_orderbook" and isinstance(contents, dict):
                    book = self._book("DYDX", sym)
                    if obj.get("type") == "subscribed":
                        book["bids"].clear(); book["asks"].clear(); self.clear_reconnect("DYDX")
                        for k in [k for k in self.dydx_offsets if k[0] == sym]: self.dydx_offsets.pop(k, None)
                    for side_name in ("bids", "asks"):
                        for row in contents.get(side_name) or []:
                            price = size = off = None
                            if isinstance(row, dict): price, size, off = row.get("price"), row.get("size"), row.get("offset")
                            elif isinstance(row, (list, tuple)) and len(row) >= 2: price, size = row[0], row[1]
                            if price is None: continue
                            if off is not None:
                                oi = _int(off)
                                if oi is not None:
                                    okey = (sym, side_name, str(price)); prev = self.dydx_offsets.get(okey)
                                    if prev is not None and oi < prev:
                                        self.metrics.inc("market_dydx_stale_level_offset_skipped"); continue
                                    self.dydx_offsets[okey] = oi
                            self._set_level(book[side_name], price, size)
                    await self.emit_book("DYDX", sym, book["bids"], book["asks"], None, "Indexer orderbook")

    async def coinbase(self, stop_event: asyncio.Event) -> None:
        url = os.getenv("COINBASE_WS", "wss://advanced-trade-ws.coinbase.com")
        products=await self._crypto_stream_symbols('coinbase',self.settings.coinbase_products)
        async with self.session.ws_connect(url, heartbeat=20, receive_timeout=60, max_msg_size=12_000_000) as ws:
            await ws.send_json({"type":"subscribe","channel":"heartbeats"})
            for ch in ("ticker","level2","market_trades"):
                for i in range(0,len(products),100):
                    await ws.send_json({"type":"subscribe","channel":ch,"product_ids":list(products[i:i+100])})
                    if i: await asyncio.sleep(0.05)
            self.clear_reconnect("COINBASE")
            async for msg in ws:
                if stop_event.is_set(): return
                if msg.type != aiohttp.WSMsgType.TEXT: continue
                await self.wire("COINBASE", "wire", "GLOBAL", msg.data)
                obj = json.loads(msg.data); ch = str(obj.get("channel") or ""); timestamp = obj.get("timestamp")
                events = obj.get("events") or []
                for event in events:
                    if not isinstance(event, dict): continue
                    if ch == "ticker":
                        self.clear_reconnect("COINBASE")
                        for d in event.get("tickers") or []:
                            if isinstance(d, dict): await self.emit("spot_ticker", "COINBASE", str(d.get("product_id") or "UNKNOWN"), d, "MARKET_STATE", timestamp, 0.15)
                    elif ch in {"l2_data", "level2"}:
                        sym = str(event.get("product_id") or "UNKNOWN"); book = self._book("COINBASE", sym)
                        if event.get("type") == "snapshot": book["bids"].clear(); book["asks"].clear(); self.clear_reconnect("COINBASE")
                        for u in event.get("updates") or []:
                            if not isinstance(u, dict): continue
                            side = "bids" if str(u.get("side") or "").lower().startswith("b") else "asks"
                            self._set_level(book[side], u.get("price_level"), u.get("new_quantity"))
                        await self.emit_book("COINBASE", sym, book["bids"], book["asks"], timestamp, "level2 updates")
                    elif ch == "market_trades":
                        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
                        for d in event.get("trades") or []:
                            if isinstance(d, dict): groups[str(d.get("product_id") or "UNKNOWN")].append(d)
                        for sym, rows in groups.items():
                            await self.trade_flow("COINBASE", sym, rows, "side", True, timestamp, "250ms batch")

    async def kraken(self, stop_event: asyncio.Event) -> None:
        url = os.getenv("KRAKEN_FUTURES_WS", "wss://futures.kraken.com/ws/v1")
        products=await self._crypto_stream_symbols('kraken',self.settings.kraken_products)
        async with self.session.ws_connect(url, heartbeat=20, receive_timeout=60, max_msg_size=12_000_000) as ws:
            for feed in ("ticker","book"):
                for i in range(0,len(products),100):
                    await ws.send_json({"event":"subscribe","feed":feed,"product_ids":list(products[i:i+100])})
                    if i: await asyncio.sleep(0.05)
            self.clear_reconnect("KRAKEN_FUTURES")
            async def flush_dirty() -> None:
                while not stop_event.is_set():
                    await asyncio.sleep(float(os.getenv("KRAKEN_BOOK_EMIT_SECONDS", "0.5")))
                    dirty = list(self.kraken_dirty.items()); self.kraken_dirty.clear()
                    for sym, ts in dirty:
                        b = self._book("KRAKEN_FUTURES", sym)
                        await self.emit_book("KRAKEN_FUTURES", sym, b["bids"], b["asks"], ts, "coalesced deltas")
            ft = asyncio.create_task(flush_dirty(), name="kraken-book-flusher")
            try:
                async for msg in ws:
                    if stop_event.is_set(): return
                    if msg.type != aiohttp.WSMsgType.TEXT: continue
                    await self.wire("KRAKEN_FUTURES", "wire", "GLOBAL", msg.data)
                    d = json.loads(msg.data); feed = str(d.get("feed") or ""); sym = str(d.get("product_id") or "")
                    if not sym: continue
                    ts = d.get("timestamp")
                    if feed.startswith("ticker"):
                        self.clear_reconnect("KRAKEN_FUTURES")
                        await self.emit("derivatives_state", "KRAKEN_FUTURES", sym, d, "DERIVATIVES", ts, 0.25)
                    elif feed == "book_snapshot":
                        book = self._book("KRAKEN_FUTURES", sym); book["bids"].clear(); book["asks"].clear(); self.clear_reconnect("KRAKEN_FUTURES")
                        for row in d.get("bids") or []:
                            if isinstance(row, dict): self._set_level(book["bids"], row.get("price"), row.get("qty"))
                        for row in d.get("asks") or []:
                            if isinstance(row, dict): self._set_level(book["asks"], row.get("price"), row.get("qty"))
                        await self.emit_book("KRAKEN_FUTURES", sym, book["bids"], book["asks"], ts, "snapshot")
                    elif feed == "book":
                        book = self._book("KRAKEN_FUTURES", sym); side = "bids" if str(d.get("side") or "").lower().startswith("b") else "asks"
                        self._set_level(book[side], d.get("price"), d.get("qty")); self.kraken_dirty[sym] = to_iso(parse_datetime(ts))
            finally:
                ft.cancel(); await asyncio.gather(ft, return_exceptions=True)

    async def hyperliquid(self, stop_event: asyncio.Event) -> None:
        url = os.getenv("HYPERLIQUID_WS", "wss://api.hyperliquid.xyz/ws")
        coins=await self._crypto_stream_symbols('hyperliquid',self.settings.hyperliquid_coins)
        async with self.session.ws_connect(url, heartbeat=20, receive_timeout=45, max_msg_size=12_000_000) as ws:
            for idx,coin in enumerate(coins,1):
                for sub in ({"type": "activeAssetCtx", "coin": coin}, {"type": "l2Book", "coin": coin}, {"type": "trades", "coin": coin}):
                    await ws.send_json({"method": "subscribe", "subscription": sub})
                if idx%20==0: await asyncio.sleep(0.15)
            self.clear_reconnect("HYPERLIQUID")
            async for msg in ws:
                if stop_event.is_set(): return
                if msg.type != aiohttp.WSMsgType.TEXT: continue
                await self.wire("HYPERLIQUID", "wire", "GLOBAL", msg.data)
                obj = json.loads(msg.data); ch = str(obj.get("channel") or ""); d = obj.get("data")
                if ch == "activeAssetCtx" and isinstance(d, dict):
                    coin = str(d.get("coin") or "UNKNOWN"); ctx = d.get("ctx") if isinstance(d.get("ctx"), dict) else d; self.clear_reconnect("HYPERLIQUID")
                    await self.emit("derivatives_state", "HYPERLIQUID", coin, dict(ctx), "DERIVATIVES", None, 0.2)
                    if isinstance(ctx, dict) and "funding" in ctx: await self.funding("HYPERLIQUID", coin, {"funding": ctx.get("funding")})
                elif ch == "l2Book" and isinstance(d, dict):
                    coin = str(d.get("coin") or "UNKNOWN"); levels = d.get("levels") or [[], []]; book = self._book("HYPERLIQUID", coin); book["bids"].clear(); book["asks"].clear(); self.clear_reconnect("HYPERLIQUID")
                    if isinstance(levels, list) and len(levels) >= 2:
                        for row in levels[0] or []:
                            if isinstance(row, dict): self._set_level(book["bids"], row.get("px"), row.get("sz"))
                        for row in levels[1] or []:
                            if isinstance(row, dict): self._set_level(book["asks"], row.get("px"), row.get("sz"))
                    await self.emit_book("HYPERLIQUID", coin, book["bids"], book["asks"], d.get("time"), "L2 snapshot")
                elif ch == "trades":
                    rows = d if isinstance(d, list) else [d] if isinstance(d, dict) else []
                    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
                    for row in rows:
                        if isinstance(row, dict): groups[str(row.get("coin") or "UNKNOWN")].append(row)
                    for coin, grp in groups.items(): await self.trade_flow("HYPERLIQUID", coin, grp, "side", False, grp[-1].get("time") if grp else None, "trade stream")

    async def market_core(self, stop_event: asyncio.Event) -> None:
        # Low-rate public global state runs isolated from exchange WebSocket processes.
        async def poll(name: str, url: str, interval: float, handler: Callable[[Any], Awaitable[None]]) -> None:
            while not stop_event.is_set():
                try:
                    async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=12, connect=4, sock_read=8)) as r:
                        body = await r.text(errors="replace")
                        await self.wire(name, "rest", "GLOBAL", body, {"status": r.status, "url": str(r.url)})
                        if r.status == 200:
                            await handler(json.loads(body)); self.metrics.inc("market_core_ok")
                        elif r.status == 429: self.metrics.inc("market_core_429")
                        else: self.metrics.inc("market_core_http_error")
                except asyncio.CancelledError: raise
                except Exception:
                    self.metrics.inc("market_core_error"); logger.exception("market core poll failed name=%s", name)
                try: await asyncio.wait_for(stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    logger.debug("market core poll interval elapsed name=%s", name)
        async def global_handler(obj: Any) -> None:
            d = obj.get("data") or {} if isinstance(obj, dict) else {}
            await self.emit("global_crypto", "COINGECKO", "GLOBAL", d, "MARKET_CAP", None, 30.0)
        async def fear_handler(obj: Any) -> None:
            row = ((obj.get("data") or [{}])[0]) if isinstance(obj, dict) else {}
            await self.emit("fear_greed", "ALTERNATIVE_ME", "GLOBAL", row if isinstance(row, dict) else {}, "SENTIMENT", row.get("timestamp") if isinstance(row, dict) else None, 60.0)
        async def defi_handler(obj: Any) -> None:
            rows = obj if isinstance(obj, list) else []
            total = sum(_float(x.get("tvl")) for x in rows if isinstance(x, dict))
            await self.emit("defi_tvl", "DEFILLAMA", "GLOBAL", {"protocols": len(rows), "aggregate_tvl": total}, "DEFI", None, 60.0)
        async def crypto_universe_loop() -> None:
            while not stop_event.is_set():
                if self.settings.market_crypto_universe_enabled:
                    try:
                        result=await self.crypto_scanner.scan_all(self.settings.market_crypto_universe_venues)
                        screen=self.crypto_scanner.screen_all_tokens(result,self.settings.market_crypto_stream_limit)
                        path=self.crypto_scanner.persist_snapshot(result,screen)
                        stats=await asyncio.to_thread(self.crypto_scanner.register_assets,result)
                        total=sum(len(rows) for rows in result.values())
                        hot=sum(1 for row in screen if row.get('tier')=='HOT');warm=max(0,len(screen)-hot)
                        self.metrics.set('crypto_universe_markets',total);self.metrics.set('crypto_universe_assets',stats.get('assets',0));self.metrics.set('crypto_screened_tokens',len(screen));self.metrics.set('crypto_hot_tokens',hot);self.metrics.set('crypto_warm_tokens',warm)
                        await self.emit('crypto_universe_snapshot','CRYPTO_UNIVERSE','GLOBAL',{'markets':total,'assets':stats.get('assets',0),'screened_tokens':len(screen),'hot_tokens':hot,'warm_tokens':warm,'venues':{k:len(v) for k,v in result.items()},'snapshot':str(path)},'MARKET_STATE',None,30.0)
                        if self.settings.market_crypto_anomaly_enabled:
                            anomalies=self.crypto_scanner.anomalies(result,self.settings.market_crypto_anomaly_change_pct,self.settings.market_crypto_anomaly_min_quote_volume_usd,self.settings.market_crypto_anomaly_max_per_cycle)
                            bucket=int(time.time()//max(3600.0,self.settings.market_crypto_universe_refresh_seconds))
                            for row in anomalies:
                                verb='surged' if row.change_pct_24h>0 else 'fell'
                                await self._market_alert(f'universe:{row.base}:{verb}:{bucket}',6,{'kind':'crypto_market_anomaly','event_type':'MARKET MOMENTUM ANOMALY','venue':row.venue,'symbol':row.symbol,'headline':f'Crypto market anomaly ${row.base} {verb} {abs(row.change_pct_24h):.2f}% in 24h','details':{'change_pct_24h':row.change_pct_24h,'quote_volume_24h':row.quote_volume_24h,'price':row.price}})
                    except asyncio.CancelledError:raise
                    except Exception:
                        self.metrics.inc('crypto_universe_cycle_error');logger.exception('crypto universe cycle failed')
                try:await asyncio.wait_for(stop_event.wait(),timeout=self.settings.market_crypto_universe_refresh_seconds)
                except asyncio.TimeoutError:continue
        tasks = [
            asyncio.create_task(crypto_universe_loop(),name='crypto-universe'),
            asyncio.create_task(poll("COINGECKO", os.getenv("COINGECKO_GLOBAL_URL", "https://api.coingecko.com/api/v3/global"), 60.0, global_handler)),
            asyncio.create_task(poll("ALTERNATIVE_ME", os.getenv("FEAR_GREED_URL", "https://api.alternative.me/fng/?limit=1&format=json"), 300.0, fear_handler)),
            asyncio.create_task(poll("DEFILLAMA", os.getenv("DEFILLAMA_PROTOCOLS_URL", "https://api.llama.fi/protocols"), 300.0, defi_handler)),
        ]
        try: await asyncio.gather(*tasks)
        finally:
            for t in tasks: t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
