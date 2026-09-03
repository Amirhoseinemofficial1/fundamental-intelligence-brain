from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from lxml import etree

from ..config import Settings
from ..loggingx import redact_text
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.structured import StructuredStore

logger = logging.getLogger(__name__)
_REGISTRIES = ("V34_18_LAYER_IMPLEMENTATION.json", "V35_30_LAYER_IMPLEMENTATION.json", "V36_30_FINAL_GAP_CLOSURE_IMPLEMENTATION.json")


def _load_json_list(raw: str, name: str) -> list[dict[str, Any]]:
    if not (raw or "").strip():
        return []
    obj = json.loads(raw)
    if not isinstance(obj, list):
        raise ValueError(f"{name} must be a JSON list")
    return [dict(x) for x in obj if isinstance(x, dict)]


def _resolve(value: Any) -> Any:
    if isinstance(value, str):
        # Explicit {ENV:NAME} references keep secrets out of connector JSON.
        def repl(m: re.Match[str]) -> str:
            return os.getenv(m.group(1), m.group(2) or "")
        return re.sub(r"\{ENV:([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}", repl, value)
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _resolve(v) for k, v in value.items()}
    return value


class InstitutionalConnectorService:
    """V52-native replacement for V34/V35/V36 closure connector runners.

    Connector names are discovered from the shipped registries instead of a hand-maintained
    subset. REST, WebSocket and configured node/RPC sources run independently and write through
    the durable StructuredStore. Commercial/private endpoints are never guessed: the operator
    supplies current documented URLs and credentials through the registry-declared env variables.
    """

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.store = StructuredStore(settings)
        self.http = ResilientHttpClient(
            settings, name="institutional-connectors", concurrency=settings.institutional_concurrency,
            per_host_limit=3, min_host_gap_seconds=settings.institutional_min_gap_seconds,
            retries=2, total_timeout=settings.institutional_timeout_seconds, max_body_bytes=16_000_000,
        )
        self.connector_envs, self.public_pages = self._registry_contracts()

    def _registry_contracts(self) -> tuple[list[str], list[dict[str, Any]]]:
        envs: set[str] = {"V34_AUTHORIZED_CONNECTORS_JSON", "V35_AUTHORIZED_CONNECTORS_JSON", "V35_PUBLIC_CONNECTORS_JSON", "V36_AUTHORIZED_CONNECTORS_JSON"}
        pages: list[dict[str, Any]] = []
        for name in _REGISTRIES:
            path = self.settings.reference_dir / name
            obj = json.loads(path.read_text(encoding="utf-8"))
            for layer in obj.get("layers", []):
                family = str(layer.get("id") or "INSTITUTIONAL")
                for src in layer.get("sources", []):
                    if not isinstance(src, dict):
                        continue
                    ce = str(src.get("connector_env") or "").strip()
                    if ce.endswith("_JSON"):
                        envs.add(ce)
                    url = str(src.get("url") or "").strip()
                    status = str(src.get("implementation_status") or "")
                    if url.startswith("https://") and status in {"native_public", "native_public_or_key", "public_native", "public"}:
                        pages.append({"family": family, "name": str(src.get("name") or urlparse(url).netloc), "url": url, "official": bool(src.get("official", False))})
        # Node connector contracts were implemented by V34 but are not all registry connector_env entries.
        envs.update({"EVM_NODE_CONNECTORS_JSON", "SOLANA_NODE_CONNECTORS_JSON", "BITCOIN_NODE_CONNECTORS_JSON"})
        return sorted(envs), pages

    def _specs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for env_name in self.connector_envs:
            for row in _load_json_list(os.getenv(env_name, ""), env_name):
                row.setdefault("connector_env", env_name)
                out.append(row)
        return out

    @staticmethod
    def _auth(spec: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
        headers = {str(k): str(v) for k, v in _resolve(dict(spec.get("headers") or {})).items()}
        params = dict(_resolve(spec.get("params") or {}))
        token_env = str(spec.get("token_env") or "")
        token = os.getenv(token_env, "").strip() if token_env else ""
        if token:
            auth_type = str(spec.get("auth_type") or "bearer").lower()
            if auth_type == "query":
                params[str(spec.get("auth_name") or "api_key")] = token
            elif auth_type == "basic":
                headers["Authorization"] = "Basic " + base64.b64encode((token + ":").encode()).decode()
            else:
                name = str(spec.get("auth_header") or "Authorization")
                prefix = str(spec.get("auth_prefix") or "Bearer").strip()
                headers[name] = f"{prefix} {token}".strip()
        return headers, params

    async def _rest(self, spec: dict[str, Any], stop_event: asyncio.Event) -> None:
        name = str(spec.get("name") or spec.get("connector_env") or "institutional")
        provider = "institutional:" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        url = str(_resolve(spec.get("url") or "")).strip()
        if not url.startswith(("http://", "https://")):
            await self.store.status(provider, "disabled_config", error="missing/invalid HTTP URL")
            return
        poll = max(0.5, float(spec.get("poll_seconds") or 30.0))
        method = str(spec.get("method") or "GET").upper()
        headers, params = self._auth(spec)
        json_body = _resolve(spec.get("body") if "body" in spec else spec.get("json"))
        lane = str(spec.get("lane") or spec.get("family") or "INSTITUTIONAL")
        record_type = str(spec.get("record_type") or spec.get("event_type") or "INSTITUTIONAL_PROVIDER_UPDATE")
        while not stop_event.is_set():
            try:
                result = await self.http.request(method, url, headers=headers or None, params=params or None, json_body=json_body)
                if result.ok:
                    try:
                        payload = json.loads(result.body)
                    except Exception:
                        payload = {"text": result.text()[:1_000_000], "content_type": result.headers.get("Content-Type", "")}
                    inserted = await self.store.insert_snapshot(provider, lane, record_type, payload)
                    await self.store.status(provider, "ok", result.status)
                    self.metrics.inc("institutional_rest_changed" if inserted else "institutional_rest_unchanged")
                    wait = poll
                else:
                    await self.store.status(provider, result.classification, result.status, result.error, result.classification == "rate_limited")
                    self.metrics.inc(f"institutional_{result.classification}")
                    wait = max(poll, result.retry_after or 0.0)
                    if result.classification in {"blocked_or_auth", "not_found"}:
                        wait = max(wait, 900.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("institutional REST failed name=%s", name)
                await self.store.status(provider, "unexpected_error", error=redact_text(f"{type(exc).__name__}: {exc}"))
                self.metrics.inc("institutional_unexpected")
                wait = min(1800.0, max(10.0, poll * 2))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait + random.random())
            except asyncio.TimeoutError:
                continue

    async def _ws(self, spec: dict[str, Any], stop_event: asyncio.Event) -> None:
        name = str(spec.get("name") or spec.get("connector_env") or "institutional_ws")
        provider = "institutional:" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        url = str(_resolve(spec.get("url") or "")).strip()
        headers, _ = self._auth(spec)
        subscriptions = _resolve(spec.get("subscribe") or spec.get("subscriptions") or [])
        if isinstance(subscriptions, dict):
            subscriptions = [subscriptions]
        lane = str(spec.get("lane") or spec.get("family") or "INSTITUTIONAL")
        record_type = str(spec.get("record_type") or spec.get("event_type") or "INSTITUTIONAL_STREAM_EVENT")
        backoff = 1.0
        while not stop_event.is_set():
            try:
                async with self.http.session.ws_connect(url, headers=headers or None, heartbeat=20, receive_timeout=75, max_msg_size=16_000_000) as ws:
                    for sub in subscriptions if isinstance(subscriptions, list) else []:
                        if isinstance(sub, str):
                            await ws.send_str(sub)
                        else:
                            await ws.send_json(sub)
                    await self.store.status(provider, "ok", 101)
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
                            self.metrics.inc("institutional_ws_changed" if inserted else "institutional_ws_unchanged")
                        elif msg.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.inc("institutional_ws_reconnect")
                await self.store.status(provider, "stream_error", error=redact_text(f"{type(exc).__name__}: {exc}"))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff + random.random())
            except asyncio.TimeoutError:
                backoff = min(60.0, backoff * 2.0)

    async def _bitcoin_rpc(self, spec: dict[str, Any], stop_event: asyncio.Event) -> None:
        name = str(spec.get("name") or "Bitcoin Core")
        provider = "node:bitcoin:" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        url = str(spec.get("url") or "")
        poll = max(0.5, float(spec.get("poll_seconds") or 2.0))
        user = str(spec.get("user") or os.getenv(str(spec.get("user_env") or ""), ""))
        password = str(spec.get("password") or os.getenv(str(spec.get("password_env") or ""), ""))
        headers: dict[str, str] = {}
        if user:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        while not stop_event.is_set():
            result = await self.http.post(url, headers=headers or None, json_body={"jsonrpc": "2.0", "id": "v52", "method": "getrawmempool", "params": [False]})
            if result.ok:
                try:
                    obj = json.loads(result.body)
                except Exception:
                    obj = {"raw": result.text()[:100_000]}
                rows = obj.get("result") if isinstance(obj, dict) else None
                payload = {"mempool_tx_count": len(rows) if isinstance(rows, list) else None, "sample": rows[:100] if isinstance(rows, list) else []}
                await self.store.insert_snapshot(provider, "OWN_ONCHAIN_GROUND_TRUTH", "BITCOIN_MEMPOOL_STATE", payload)
                await self.store.status(provider, "ok", result.status)
            else:
                await self.store.status(provider, result.classification, result.status, result.error, result.classification == "rate_limited")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll)
            except asyncio.TimeoutError:
                continue

    async def _public_page_watch(self, stop_event: asyncio.Event) -> None:
        if not self.settings.institutional_page_watch_enabled:
            return
        pages = self.public_pages
        cursor = 0
        while not stop_event.is_set() and pages:
            batch = []
            for _ in range(min(self.settings.institutional_page_watch_batch, len(pages))):
                batch.append(pages[cursor % len(pages)])
                cursor += 1
            for row in batch:
                if stop_event.is_set():
                    break
                result = await self.http.get(str(row["url"]), max_body_bytes=3_000_000)
                provider = "page:" + hashlib.sha1(str(row["url"]).encode()).hexdigest()[:20]
                if result.ok:
                    sig = {"sha256": hashlib.sha256(result.body).hexdigest(), "bytes": len(result.body), "url": result.url, "family": row["family"], "source": row["name"], "official": row["official"]}
                    inserted = await self.store.insert_snapshot(provider, str(row["family"]), "PUBLIC_SOURCE_PAGE_REVISION", sig)
                    self.metrics.inc("institutional_page_changed" if inserted else "institutional_page_unchanged")
                    await self.store.status(provider, "ok", result.status)
                else:
                    await self.store.status(provider, result.classification, result.status, result.error, result.classification == "rate_limited")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.settings.institutional_page_watch_seconds)
            except asyncio.TimeoutError:
                continue

    async def _sec_current_forms(self, stop_event: asyncio.Event) -> None:
        if not self.settings.sec_current_forms_enabled:
            return
        forms = ("N-PX", "DEF 14A", "S-1", "F-1", "S-3", "F-3", "424B5", "SC TO-I", "SC TO-T", "8-K", "6-K", "20-F", "10-K")
        url = "https://www.sec.gov/cgi-bin/browse-edgar"
        while not stop_event.is_set():
            for form in forms:
                result = await self.http.get(url, params={"action": "getcurrent", "type": form, "owner": "include", "count": "100", "output": "atom"}, headers={"User-Agent": self.settings.user_agent, "Accept-Encoding": "gzip, deflate"}, max_body_bytes=8_000_000)
                provider = "sec_current:" + form.replace(" ", "_")
                if not result.ok:
                    await self.store.status(provider, result.classification, result.status, result.error, result.classification == "rate_limited")
                    continue
                try:
                    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
                    root = etree.fromstring(result.body, parser=parser)
                    ns = {"a": "http://www.w3.org/2005/Atom"}
                    rows=[]
                    for e in root.findall("a:entry", ns):
                        eid=(e.findtext("a:id", "", ns) or "").strip(); title=(e.findtext("a:title", "", ns) or "").strip(); updated=(e.findtext("a:updated", "", ns) or "").strip(); link=e.find("a:link", ns); href=link.get("href", "") if link is not None else ""
                        if eid: rows.append({"form":form,"entry_id":eid,"title":title,"updated":updated,"url":href})
                    inserted = await self.store.insert_snapshots(provider, "GLOBAL_ISSUER_FILINGS", "SEC_CURRENT_FILING", rows)
                    self.metrics.inc("sec_current_forms_changed", inserted)
                    await self.store.status(provider, "ok", result.status)
                except Exception as exc:
                    await self.store.status(provider, "parse_error", result.status, redact_text(str(exc)))
                    self.metrics.inc("sec_current_forms_parse_error")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.settings.sec_current_forms_poll_seconds)
            except asyncio.TimeoutError:
                continue

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.institutional_enabled:
            logger.info("institutional connector plane disabled")
            await self.http.close(); await stop_event.wait(); return
        try:
            specs = self._specs()
        except Exception as exc:
            await self.http.close()
            raise RuntimeError(f"invalid institutional connector JSON: {exc}") from exc
        self.metrics.set("institutional_connector_env_contracts", len(self.connector_envs))
        self.metrics.set("institutional_connector_specs", len(specs))
        self.metrics.set("institutional_public_pages", len(self.public_pages))
        tasks: list[asyncio.Task[Any]] = []
        for i, spec in enumerate(specs):
            env_name = str(spec.get("connector_env") or "")
            url = str(spec.get("url") or "")
            if env_name == "BITCOIN_NODE_CONNECTORS_JSON":
                tasks.append(asyncio.create_task(self._bitcoin_rpc(spec, stop_event), name=f"institutional:bitcoin:{i}"))
            elif url.startswith(("ws://", "wss://")):
                # Provide V34 defaults for node subscriptions when the operator omitted subscribe.
                if env_name == "EVM_NODE_CONNECTORS_JSON" and not spec.get("subscribe"):
                    spec["subscribe"] = {"jsonrpc":"2.0","id":1,"method":"eth_subscribe","params":["newHeads"]}
                if env_name == "SOLANA_NODE_CONNECTORS_JSON" and not spec.get("subscribe"):
                    spec["subscribe"] = {"jsonrpc":"2.0","id":1,"method":"logsSubscribe","params":["all",{"commitment":"processed"}]}
                tasks.append(asyncio.create_task(self._ws(spec, stop_event), name=f"institutional:ws:{i}"))
            else:
                tasks.append(asyncio.create_task(self._rest(spec, stop_event), name=f"institutional:rest:{i}"))
        tasks.append(asyncio.create_task(self._public_page_watch(stop_event), name="institutional:page-watch"))
        tasks.append(asyncio.create_task(self._sec_current_forms(stop_event), name="institutional:sec-current"))
        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    self.metrics.inc("institutional_task_shutdown_error")
                    logger.error("institutional task shutdown error=%s", redact_text(str(result)))
            await self.http.close()
