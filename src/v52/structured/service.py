from __future__ import annotations

import asyncio
import base64
import copy
import datetime as dt
import json
import logging
import os
import random
import re
import time
from typing import Any

from ..config import Settings
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.structured import StructuredStore
from ..timeutil import utc_now
from ..news.registry import SourceRegistry

logger = logging.getLogger(__name__)
_ENV_PATTERN = re.compile(r"\{ENV:([^}:]+)(?::([^}]*))?\}")


def _slug_env(slug: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", slug.upper()).strip("_") + "_QUERIES_JSON"


def _expand(value: Any, key: str) -> Any:
    now = utc_now()
    if isinstance(value, str):
        out = value.replace("{KEY}", key)
        out = out.replace("{NOW_ISO}", now.isoformat().replace("+00:00", "Z"))
        out = out.replace("{NOW_MINUS_2H_ISO}", (now - dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z"))
        out = out.replace("{YEAR}", str(now.year))
        def repl(m: re.Match[str]) -> str:
            return os.getenv(m.group(1), m.group(2) or "")
        return _ENV_PATTERN.sub(repl, out)
    if isinstance(value, list):
        return [_expand(x, key) for x in value]
    if isinstance(value, dict):
        return {k: _expand(v, key) for k, v in value.items()}
    return value


class StructuredProviderRunner:
    def __init__(self, settings: Settings, metrics: Metrics, provider: dict[str, Any], http: ResilientHttpClient, store: StructuredStore):
        self.settings = settings
        self.metrics = metrics
        self.provider = provider
        self.http = http
        self.store = store
        self.stop = asyncio.Event()
        self.oauth_cache: dict[str, tuple[str, float]] = {}

    def _credential(self) -> tuple[str, bool]:
        req = self.provider.get("request") or {}
        auth = req.get("auth") or {}
        typ = str(auth.get("type") or "none").lower()
        allow_without = bool(req.get("allow_without_key")) or typ in {"none", "header_optional", "oauth_bearer"}
        key_env = str(self.provider.get("key_env") or "")
        key = os.getenv(key_env, "").strip() if key_env else ""
        if typ == "oauth_bearer":
            oauth = dict(self.provider.get("oauth") or {})
            oauth.update(req.get("oauth") or {})
            grant = str(oauth.get("grant_type") or "client_credentials")
            if grant == "client_credentials":
                cid = os.getenv(str(oauth.get("client_id_env") or key_env), "").strip() or str(oauth.get("client_id") or "")
                sec = os.getenv(str(oauth.get("client_secret_env") or ""), "").strip() or str(oauth.get("client_secret") or "")
                return key, bool(cid and sec)
            if grant == "password":
                user = os.getenv(str(oauth.get("username_env") or key_env), "").strip()
                pw = os.getenv(str(oauth.get("password_env") or ""), "").strip()
                return key, bool(user and pw)
            return key, False
        return key, allow_without or bool(key)

    async def _oauth_token(self, req: dict[str, Any]) -> str:
        oauth = dict(self.provider.get("oauth") or {})
        oauth.update(req.get("oauth") or {})
        token_url = str(_expand(oauth.get("token_url") or "", ""))
        if not token_url:
            raise RuntimeError("OAUTH_TOKEN_URL_MISSING")
        cache_key = f"{self.provider.get('slug','oauth')}|{token_url}"
        now = time.monotonic()
        cached = self.oauth_cache.get(cache_key)
        if cached and cached[1] > now + 15.0:
            return cached[0]
        grant = str(oauth.get("grant_type") or "client_credentials")
        form: dict[str, str] = {"grant_type": grant}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        key_env = str(self.provider.get("key_env") or "")
        if grant == "client_credentials":
            cid = os.getenv(str(oauth.get("client_id_env") or key_env), "").strip() or str(oauth.get("client_id") or "")
            sec = os.getenv(str(oauth.get("client_secret_env") or ""), "").strip() or str(oauth.get("client_secret") or "")
            if not cid or not sec:
                raise RuntimeError("OAUTH_CLIENT_CREDENTIALS_MISSING")
            if str(oauth.get("client_auth") or "form").lower() == "basic":
                token = base64.b64encode(f"{cid}:{sec}".encode()).decode()
                headers["Authorization"] = f"Basic {token}"
            else:
                form.update({"client_id": cid, "client_secret": sec})
        elif grant == "password":
            user = os.getenv(str(oauth.get("username_env") or key_env), "").strip()
            pw = os.getenv(str(oauth.get("password_env") or ""), "").strip()
            if not user or not pw:
                raise RuntimeError("OAUTH_USER_CREDENTIALS_MISSING")
            form.update({"username": user, "password": pw, "client_id": str(oauth.get("client_id") or "acled")})
        else:
            raise RuntimeError(f"OAUTH_UNSUPPORTED_GRANT:{grant}")
        if oauth.get("scope"):
            form["scope"] = str(oauth["scope"])
        result = await self.http.request("POST", token_url, headers=headers, data=form, max_body_bytes=1_000_000)
        if not result.ok:
            raise RuntimeError(f"OAUTH_{result.classification}:{result.status}:{result.error}")
        try:
            obj = json.loads(result.body)
        except Exception as exc:
            raise RuntimeError(f"OAUTH_INVALID_JSON:{exc}") from exc
        token = str(obj.get("access_token") or "")
        if not token:
            raise RuntimeError("OAUTH_ACCESS_TOKEN_MISSING")
        expires = max(60, int(obj.get("expires_in") or 300))
        self.oauth_cache[cache_key] = (token, now + max(30, expires - 30))
        return token

    async def _request_parts(self, key: str) -> tuple[str, str, dict[str, Any], dict[str, str], Any, Any]:
        req = copy.deepcopy(self.provider["request"])
        method = str(req.get("method") or "GET").upper()
        url = str(_expand(req.get("url") or "", key))
        params = _expand(req.get("params") or {}, key)
        headers: dict[str, str] = {str(k): str(v) for k, v in (_expand(req.get("headers") or {}, key) or {}).items()}
        json_body = _expand(req.get("json"), key) if "json" in req else None
        data = _expand(req.get("data"), key) if "data" in req else None
        auth = req.get("auth") or {}
        typ = str(auth.get("type") or "none").lower()
        name = str(auth.get("name") or "")
        if typ == "oauth_bearer":
            headers["Authorization"] = f"Bearer {await self._oauth_token(req)}"
        elif key:
            if typ == "query" and name:
                params[name] = key
            elif typ in {"header", "header_optional"} and name:
                headers[name] = key
            elif typ == "bearer":
                headers["Authorization"] = f"Bearer {key}"
            elif typ == "basic":
                token = base64.b64encode(f"{key}:".encode()).decode()
                headers["Authorization"] = f"Basic {token}"
        return method, url, params, headers, json_body, data

    async def run(self, stop_event: asyncio.Event) -> None:
        slug = str(self.provider.get("slug") or self.provider.get("name") or "provider")
        req = self.provider.get("request")
        if not isinstance(req, dict):
            await self.store.status(slug, "registry_only", error="provider has no generic request definition; retained in reference registry")
            self.metrics.inc("structured_registry_only")
            return
        key, enabled = self._credential()
        if not enabled:
            await self.store.status(slug, "disabled_missing_key", error=f"missing {self.provider.get('key_env') or 'credential'}")
            self.metrics.inc("structured_missing_key")
            return
        poll = max(10.0, float(req.get("poll_seconds") or 300.0))
        while not stop_event.is_set():
            try:
                method, url, params, headers, json_body, data = await self._request_parts(key)
                result = await self.http.request(method, url, params=params or None, headers=headers or None, json_body=json_body, data=data)
                if result.ok:
                    try:
                        payload = json.loads(result.body)
                    except Exception:
                        payload = {"text": result.text()[:1_000_000]}
                    inserted = await self.store.insert_snapshot(slug, str(req.get("lane") or "STRUCTURED"), str(req.get("record_type") or "STRUCTURED_UPDATE"), payload)
                    await self.store.status(slug, "ok", result.status)
                    self.metrics.inc("structured_ok")
                    self.metrics.inc("structured_changed" if inserted else "structured_unchanged")
                    wait = poll
                else:
                    await self.store.status(slug, result.classification, result.status, result.error, result.classification == "rate_limited")
                    self.metrics.inc(f"structured_{result.classification}")
                    wait = max(poll, result.retry_after or 0.0)
                    if result.classification in {"blocked_or_auth", "not_found"}:
                        wait = max(wait, 3600.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("structured provider crashed provider=%s", slug)
                await self.store.status(slug, "unexpected_error", error=f"{type(exc).__name__}:{exc}")
                self.metrics.inc("structured_unexpected")
                wait = min(3600.0, poll * 2.0)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait + random.random() * min(2.0, wait * 0.05))
            except asyncio.TimeoutError:
                continue


class StructuredService:
    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.registry = SourceRegistry(settings)
        self.store = StructuredStore(settings)
        self.http = ResilientHttpClient(settings, name="structured", concurrency=settings.structured_concurrency, per_host_limit=2, retries=2, total_timeout=settings.structured_default_timeout, max_body_bytes=8_000_000)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.structured_enabled:
            logger.info("structured plane disabled")
            await self.http.close()
            await stop_event.wait()
            return
        providers = self.registry.structured_providers()
        # Custom generic adapters are fully runtime-configurable without code changes.
        custom = os.getenv("CUSTOM_STRUCTURED_REQUESTS_JSON", "").strip()
        if custom:
            try:
                rows = json.loads(custom)
                if not isinstance(rows, list):
                    raise ValueError("CUSTOM_STRUCTURED_REQUESTS_JSON must be a JSON list")
                for i, row in enumerate(rows):
                    if isinstance(row, dict) and isinstance(row.get("request"), dict):
                        p = dict(row)
                        p.setdefault("slug", f"custom_{i}")
                        p.setdefault("name", p["slug"])
                        providers.append(p)
            except Exception:
                logger.exception("invalid CUSTOM_STRUCTURED_REQUESTS_JSON")
                self.metrics.inc("structured_custom_config_error")
        # Restore the V36 free-provider contract: every registry provider has a canonical
        # <SLUG>_QUERIES_JSON hook. These are concrete runtime adapters, not registry-only labels.
        expanded: list[dict[str, Any]] = []
        for p in providers:
            expanded.append(p)
            slug = str(p.get("slug") or "").strip()
            if not slug:
                continue
            qenv = _slug_env(slug)
            raw = os.getenv(qenv, "").strip()
            if not raw:
                continue
            try:
                rows = json.loads(raw)
                if isinstance(rows, dict):
                    rows = [rows]
                if not isinstance(rows, list):
                    raise ValueError(f"{qenv} must be a JSON list/object")
                for i, row in enumerate(rows):
                    if not isinstance(row, dict) or not row.get("url"):
                        continue
                    req = dict(row)
                    for k in ("record_type", "lane", "poll_seconds", "auth", "oauth", "allow_without_key"):
                        req.setdefault(k, (p.get("request") or {}).get(k))
                    cp = dict(p)
                    cp["slug"] = f"{slug}_configured_{i}"
                    cp["name"] = f"{p.get('name') or slug} configured {i+1}"
                    cp["request"] = req
                    expanded.append(cp)
            except Exception as exc:
                logger.error("invalid provider query config env=%s error=%s", qenv, exc)
                self.metrics.inc("structured_provider_query_config_error")
        providers = expanded
        self.metrics.set("structured_provider_registry", len(providers))
        runners = [StructuredProviderRunner(self.settings, self.metrics, p, self.http, self.store) for p in providers]

        async def supervise(runner: StructuredProviderRunner, provider: dict[str, Any], index: int) -> None:
            slug = str(provider.get("slug") or provider.get("name") or f"provider_{index}")
            backoff = 1.0
            while not stop_event.is_set():
                try:
                    await runner.run(stop_event)
                    # Registry-only and missing-key providers intentionally stop after publishing status.
                    request = provider.get("request")
                    _key, enabled = runner._credential()
                    if not isinstance(request, dict) or not enabled or stop_event.is_set():
                        return
                    self.metrics.inc("structured_runner_early_return")
                    logger.error("structured provider returned unexpectedly provider=%s", slug)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.metrics.inc("structured_runner_restart")
                    logger.exception("structured provider supervisor caught failure provider=%s error=%s", slug, exc)
                    await self.store.status(slug, "runner_crashed", error=f"{type(exc).__name__}:{exc}")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff + random.random())
                except asyncio.TimeoutError:
                    backoff = min(60.0, backoff * 2.0)
                    continue
                return

        tasks = [
            asyncio.create_task(supervise(r, p, i), name=f"structured-supervisor:{p.get('slug', i)}")
            for i, (r, p) in enumerate(zip(runners, providers))
        ]
        try:
            await stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    self.metrics.inc("structured_supervisor_shutdown_error")
                    logger.error("structured supervisor task shutdown error=%s", result)
            await self.http.close()
