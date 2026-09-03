from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from typing import Any

import aiohttp

from ..config import Settings
from ..loggingx import redact_text
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.structured import StructuredStore

logger = logging.getLogger(__name__)


def _json_list(raw: str, label: str) -> list[dict[str, Any]]:
    raw = (raw or "").strip()
    if not raw:
        return []
    obj = json.loads(raw)
    if not isinstance(obj, list):
        raise ValueError(f"{label} must be a JSON list")
    return [dict(x) for x in obj if isinstance(x, dict)]


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("{ENV:") and value.endswith("}"):
            body = value[5:-1]
            name, _, default = body.partition(":")
            return os.getenv(name, default)
        return value
    if isinstance(value, dict):
        return {str(k): _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class ProviderFlowService:
    """Durable V52 smart-money/on-chain/provider flow plane.

    Concrete adapters exist for Nansen and CryptoQuant. CoinGlass carries the
    high-value V50 routes plus optional custom routes. Glassnode/EIA and other
    authorized providers are runtime-configurable through JSON query lists so
    endpoint revisions do not require edits to the process supervisor.
    """

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.store = StructuredStore(settings)
        self.http = ResilientHttpClient(
            settings,
            name="provider-flows",
            concurrency=settings.provider_flow_concurrency,
            per_host_limit=3,
            min_host_gap_seconds=settings.provider_flow_min_gap_seconds,
            retries=2,
            total_timeout=settings.provider_flow_timeout_seconds,
            max_body_bytes=12_000_000,
        )

    async def _status(self, name: str, result: Any) -> None:
        if result.ok:
            await self.store.status(name, "ok", result.status)
        else:
            await self.store.status(name, result.classification, result.status, result.error, result.classification == "rate_limited")

    async def _poll_rest(self, cfg: dict[str, Any], stop_event: asyncio.Event) -> None:
        name = str(cfg.get("name") or cfg.get("provider") or "provider")
        provider = str(cfg.get("provider") or name).lower().replace(" ", "_")
        url = str(cfg.get("url") or "").strip()
        if not url:
            await self.store.status(provider, "disabled_config", error=f"{name}: missing url")
            return
        poll = max(1.0, float(cfg.get("poll_seconds") or self.settings.provider_flow_poll_seconds))
        method = str(cfg.get("method") or "GET").upper()
        headers = _expand_env(dict(cfg.get("headers") or {}))
        params = _expand_env(dict(cfg.get("params") or {}))
        body = _expand_env(cfg.get("json"))
        record_type = str(cfg.get("record_type") or "PROVIDER_FLOW")
        lane = str(cfg.get("lane") or "SMART_MONEY")
        while not stop_event.is_set():
            try:
                result = await self.http.request(method, url, headers=headers or None, params=params or None, json_body=body)
                await self._status(provider, result)
                if result.ok:
                    try:
                        payload = json.loads(result.body)
                    except Exception:
                        payload = {"text": result.text()[:1_000_000]}
                    inserted = await self.store.insert_snapshot(provider, lane, record_type, payload)
                    self.metrics.inc("provider_flow_ok")
                    self.metrics.inc("provider_flow_changed" if inserted else "provider_flow_unchanged")
                    wait = poll
                else:
                    self.metrics.inc(f"provider_flow_{result.classification}")
                    wait = max(poll, result.retry_after or 0.0)
                    if result.classification in {"blocked_or_auth", "not_found"}:
                        wait = max(wait, 1800.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("provider flow failed provider=%s", name)
                await self.store.status(provider, "unexpected_error", error=redact_text(f"{type(exc).__name__}: {exc}"))
                self.metrics.inc("provider_flow_unexpected")
                wait = min(1800.0, max(10.0, poll * 2))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait + random.random() * min(1.0, wait * 0.05))
            except asyncio.TimeoutError:
                continue

    async def _stream(self, cfg: dict[str, Any], stop_event: asyncio.Event) -> None:
        name = str(cfg.get("name") or "authorized_stream")
        provider = str(cfg.get("provider") or name).lower().replace(" ", "_")
        url = str(cfg.get("url") or "").strip()
        if not url.startswith(("ws://", "wss://")):
            await self.store.status(provider, "disabled_config", error=f"{name}: stream url must be ws/wss")
            return
        headers = _expand_env(dict(cfg.get("headers") or {}))
        subscriptions = _expand_env(cfg.get("subscriptions") or cfg.get("subscribe") or [])
        if isinstance(subscriptions, dict):
            subscriptions = [subscriptions]
        record_type = str(cfg.get("record_type") or "PROVIDER_STREAM")
        lane = str(cfg.get("lane") or "SMART_MONEY")
        backoff = 1.0
        while not stop_event.is_set():
            try:
                async with self.http.session.ws_connect(url, headers=headers or None, heartbeat=20, receive_timeout=75, max_msg_size=8_000_000) as ws:
                    for payload in subscriptions if isinstance(subscriptions, list) else []:
                        if isinstance(payload, str):
                            await ws.send_str(payload)
                        else:
                            await ws.send_json(payload)
                    await self.store.status(provider, "ok", 101)
                    self.metrics.inc("provider_stream_connected")
                    backoff = 1.0
                    async for msg in ws:
                        if stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            text = str(msg.data).strip()
                            if not text:
                                continue
                            try:
                                payload = json.loads(text)
                            except Exception:
                                payload = {"text": text[:1_000_000]}
                            inserted = await self.store.insert_snapshot(provider, lane, record_type, payload)
                            self.metrics.inc("provider_stream_changed" if inserted else "provider_stream_unchanged")
                        elif msg.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE}:
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.inc("provider_stream_reconnect")
                await self.store.status(provider, "stream_error", error=redact_text(f"{type(exc).__name__}: {exc}"))
                logger.warning("provider stream reconnect provider=%s error=%s", provider, redact_text(str(exc)))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff + random.random())
            except asyncio.TimeoutError:
                backoff = min(60.0, backoff * 2.0)

    def _configs(self) -> list[dict[str, Any]]:
        s = self.settings
        out: list[dict[str, Any]] = []
        if s.nansen_api_key:
            out.append({
                "name": "Nansen Smart Money Netflow", "provider": "nansen", "method": "POST",
                "url": f"{s.nansen_base}/smart-money/netflow",
                "headers": {"apiKey": s.nansen_api_key, "Content-Type": "application/json"},
                "json": {"chains": list(s.nansen_smart_money_chains), "pagination": {"page": 1, "per_page": 100}, "order_by": [{"field": "net_flow_1h_usd", "direction": "DESC"}]},
                "poll_seconds": s.nansen_poll_seconds, "record_type": "SMART_MONEY_NETFLOW", "lane": "SMART_MONEY",
            })
            for row in _json_list(os.getenv("NANSEN_TOKEN_QUERIES_JSON", ""), "NANSEN_TOKEN_QUERIES_JSON"):
                endpoint = str(row.pop("endpoint", "tgm/flow-intelligence")).lstrip("/")
                row.setdefault("name", f"Nansen {endpoint}")
                row.setdefault("provider", "nansen")
                row.setdefault("method", "POST")
                row.setdefault("url", f"{s.nansen_base}/{endpoint}")
                row.setdefault("headers", {"apiKey": s.nansen_api_key, "Content-Type": "application/json"})
                row.setdefault("poll_seconds", s.nansen_poll_seconds)
                row.setdefault("record_type", "NANSEN_TOKEN_FLOW")
                row.setdefault("lane", "WHALE_FLOW")
                out.append(row)

        if s.cryptoquant_token:
            for exchange in s.cryptoquant_exchanges:
                params = {"exchange": exchange, "window": s.cryptoquant_window, "limit": 2}
                for endpoint, rtype, lane in (
                    ("reserve", "EXCHANGE_RESERVE", "EXCHANGE_FLOW"),
                    ("netflow", "EXCHANGE_NETFLOW", "EXCHANGE_FLOW"),
                    ("inflow", "EXCHANGE_INFLOW", "EXCHANGE_FLOW"),
                    ("outflow", "EXCHANGE_OUTFLOW", "EXCHANGE_FLOW"),
                ):
                    out.append({
                        "name": f"CryptoQuant {exchange} {endpoint}", "provider": "cryptoquant",
                        "url": f"{s.cryptoquant_base}/btc/exchange-flows/{endpoint}",
                        "headers": {"Authorization": f"Bearer {s.cryptoquant_token}"},
                        "params": params, "poll_seconds": s.cryptoquant_poll_seconds,
                        "record_type": rtype, "lane": lane,
                    })
            for row in _json_list(os.getenv("CRYPTOQUANT_EXTRA_QUERIES_JSON", ""), "CRYPTOQUANT_EXTRA_QUERIES_JSON"):
                path = str(row.pop("path", "")).lstrip("/")
                row.setdefault("name", f"CryptoQuant {path or 'extra'}")
                row.setdefault("provider", "cryptoquant")
                row.setdefault("url", f"{s.cryptoquant_base}/{path}")
                row.setdefault("headers", {"Authorization": f"Bearer {s.cryptoquant_token}"})
                row.setdefault("poll_seconds", s.cryptoquant_poll_seconds)
                out.append(row)

        if s.coinglass_api_key:
            h = {"CG-API-KEY": s.coinglass_api_key}
            for symbol in s.coinglass_symbols:
                out.extend([
                    {"name": f"CoinGlass Whale {symbol}", "provider": "coinglass", "url": f"{s.coinglass_base}/api/chain/v2/whale-transfer", "headers": h, "params": {"symbol": symbol}, "poll_seconds": s.coinglass_whale_poll_seconds, "record_type": "WHALE_TRANSFER", "lane": "WHALE_FLOW"},
                    {"name": f"CoinGlass OI {symbol}", "provider": "coinglass", "url": f"{s.coinglass_base}/api/futures/open-interest/exchange-list", "headers": h, "params": {"symbol": symbol}, "poll_seconds": 5.0, "record_type": "OPEN_INTEREST_AGGREGATE", "lane": "DERIVATIVES"},
                    {"name": f"CoinGlass Spot NetFlow {symbol}", "provider": "coinglass", "url": f"{s.coinglass_base}/api/spot/coin/netflow", "headers": h, "params": {"symbol": symbol, "exchange_list": "Binance,Bybit,OKX,Bitget,Gate"}, "poll_seconds": 5.0, "record_type": "SPOT_BUY_SELL_NETFLOW", "lane": "FLOW_VOLUME"},
                    {"name": f"CoinGlass Balance {symbol}", "provider": "coinglass", "url": f"{s.coinglass_base}/api/exchange/balance/list", "headers": h, "params": {"symbol": symbol}, "poll_seconds": s.coinglass_balance_poll_seconds, "record_type": "EXCHANGE_BALANCE", "lane": "EXCHANGE_FLOW"},
                ])
            out.extend([
                {"name": "CoinGlass Spot NetFlow Universe", "provider": "coinglass", "url": f"{s.coinglass_base}/api/spot/netflow-list", "headers": h, "poll_seconds": 5.0, "record_type": "SPOT_NETFLOW_UNIVERSE", "lane": "FLOW_VOLUME"},
                {"name": "CoinGlass Funding Across Exchanges", "provider": "coinglass", "url": f"{s.coinglass_base}/api/futures/funding-rate/exchange-list", "headers": h, "poll_seconds": 10.0, "record_type": "FUNDING_UNIVERSE", "lane": "DERIVATIVES"},
                {"name": "CoinGlass Stablecoin Market Cap", "provider": "coinglass", "url": f"{s.coinglass_base}/api/index/stableCoin-marketCap-history", "headers": h, "poll_seconds": 300.0, "record_type": "STABLECOIN_MARKET_CAP", "lane": "STABLECOIN"},
            ])
            for row in _json_list(os.getenv("COINGLASS_EXTRA_QUERIES_JSON", ""), "COINGLASS_EXTRA_QUERIES_JSON"):
                path = str(row.pop("path", ""))
                row.setdefault("name", "CoinGlass Extra")
                row.setdefault("provider", "coinglass")
                row.setdefault("url", f"{s.coinglass_base}{path}")
                row.setdefault("headers", h)
                out.append(row)

        # Glassnode deliberately uses explicit query definitions because metric paths and plan
        # entitlements vary. V50 used the same model; V52 keeps it durable and observable.
        for row in _json_list(os.getenv("GLASSNODE_QUERIES_JSON", ""), "GLASSNODE_QUERIES_JSON"):
            if s.glassnode_api_key:
                params = dict(row.get("params") or {})
                params.setdefault("api_key", s.glassnode_api_key)
                row["params"] = params
            row.setdefault("provider", "glassnode")
            row.setdefault("name", "Glassnode Query")
            row.setdefault("record_type", "ONCHAIN_METRIC")
            row.setdefault("lane", "ONCHAIN")
            out.append(row)

        for env_name in ("REALTIME_FUNDAMENTAL_REST_JSON", "AUTHORIZED_STRUCTURED_REST_JSON"):
            for row in _json_list(os.getenv(env_name, ""), env_name):
                row.setdefault("provider", str(row.get("name") or "authorized_rest").lower().replace(" ", "_"))
                out.append(row)
        return out

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.provider_flow_enabled:
            logger.info("provider-flow plane disabled")
            await self.http.close()
            await stop_event.wait()
            return
        try:
            configs = self._configs()
        except Exception as exc:
            await self.http.close()
            raise RuntimeError(f"invalid provider-flow JSON configuration: {exc}") from exc
        stream_configs: list[dict[str, Any]] = []
        for env_name in ("REALTIME_FUNDAMENTAL_STREAMS_JSON", "AUTHORIZED_STRUCTURED_STREAMS_JSON"):
            stream_configs.extend(_json_list(os.getenv(env_name, ""), env_name))
        self.metrics.set("provider_flow_configured", len(configs))
        self.metrics.set("provider_stream_configured", len(stream_configs))
        if not configs and not stream_configs:
            logger.info("provider-flow plane active with no credentials/configured queries")
            await stop_event.wait()
            await self.http.close()
            return
        tasks = [asyncio.create_task(self._poll_rest(cfg, stop_event), name=f"provider-flow:{i}:{cfg.get('provider')}") for i, cfg in enumerate(configs)]
        tasks.extend(asyncio.create_task(self._stream(cfg, stop_event), name=f"provider-stream:{i}:{cfg.get('provider')}") for i, cfg in enumerate(stream_configs))
        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    self.metrics.inc("provider_flow_task_shutdown_error")
                    logger.error("provider-flow task shutdown error=%s", redact_text(str(result)))
            await self.http.close()
