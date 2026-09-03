from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import email.utils
import logging
import random
import socket
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from ..config import Settings
from ..loggingx import redact_text
from .circuit import CircuitBreaker
from .rate import AsyncRateLimiter

logger = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True)
class HttpResult:
    ok: bool
    url: str
    status: int | None
    body: bytes
    headers: dict[str, str]
    latency_ms: float
    classification: str
    error: str = ""
    retry_after: float | None = None

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")


class HttpFailure(RuntimeError):
    def __init__(self, result: HttpResult):
        self.result = result
        super().__init__(redact_text(f"HTTP failure classification={result.classification} status={result.status} url={result.url} error={result.error}"))


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            return max(0.0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class ResilientHttpClient:
    def __init__(
        self,
        settings: Settings,
        *,
        name: str,
        concurrency: int = 16,
        per_host_limit: int = 4,
        min_host_gap_seconds: float = 0.0,
        retries: int = 3,
        total_timeout: float | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        max_body_bytes: int | None = None,
    ):
        self.settings = settings
        self.name = name
        self.retries = max(0, retries)
        self.max_body_bytes = max_body_bytes or settings.news_max_body_bytes
        self.circuit = CircuitBreaker()
        self._host_limiters: dict[str, AsyncRateLimiter] = defaultdict(lambda: AsyncRateLimiter(min_host_gap_seconds))
        self._timeout = aiohttp.ClientTimeout(
            total=total_timeout or settings.news_http_total_timeout,
            connect=connect_timeout or settings.news_http_connect_timeout,
            sock_read=read_timeout or settings.news_http_read_timeout,
        )
        connector = aiohttp.TCPConnector(
            limit=max(1, concurrency),
            limit_per_host=max(1, per_host_limit),
            ttl_dns_cache=300
        )
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=self._timeout,
            headers={"User-Agent": settings.user_agent, "Accept-Encoding": "gzip, deflate, br"},
            raise_for_status=False,
        )

    async def close(self) -> None:
        await self.session.close()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        data: Any = None,
        retries: int | None = None,
        max_body_bytes: int | None = None,
        allow_status: set[int] | None = None,
    ) -> HttpResult:
        tries = self.retries if retries is None else max(0, retries)
        host = urlsplit(url).netloc.lower()
        allowed, wait_for = self.circuit.allow(host)
        if not allowed:
            return HttpResult(False, url, None, b"", {}, 0.0, "circuit_open", redact_text(f"circuit open for {wait_for:.1f}s"), wait_for)
        body_limit = max_body_bytes or self.max_body_bytes
        last: HttpResult | None = None
        for attempt in range(tries + 1):
            await self._host_limiters[host].wait()
            start = time.perf_counter()
            try:
                async with self.session.request(
                    method.upper(), url, params=params, headers=headers, json=json_body, data=data,
                    allow_redirects=True, max_redirects=8,
                ) as response:
                    status = response.status
                    chunks: list[bytes] = []
                    received = 0
                    too_large = False
                    async for part in response.content.iter_chunked(min(64 * 1024, body_limit + 1)):
                        received += len(part)
                        if received > body_limit:
                            keep = len(part) - (received - body_limit)
                            if keep > 0:
                                chunks.append(part[:keep])
                            too_large = True
                            break
                        chunks.append(part)
                    chunk = b"".join(chunks)
                    latency = (time.perf_counter() - start) * 1000.0
                    hdrs = {k: v for k, v in response.headers.items()}
                    if too_large:
                        result = HttpResult(False, str(response.url), status, chunk, hdrs, latency, "body_too_large", f"body exceeded {body_limit} bytes")
                    elif (
                        response.content_length is not None
                        and not response.headers.get("Content-Encoding")
                        and received != response.content_length
                    ):
                        result = HttpResult(False, str(response.url), status, chunk, hdrs, latency, "truncated_body", f"expected {response.content_length} bytes, received {received}")
                    elif 200 <= status < 300 or (allow_status and status in allow_status):
                        self.circuit.success(host)
                        return HttpResult(True, str(response.url), status, chunk, hdrs, latency, "ok")
                    elif status == 429:
                        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                        result = HttpResult(False, str(response.url), status, chunk, hdrs, latency, "rate_limited", "HTTP 429", retry_after)
                    elif status in {401, 403}:
                        result = HttpResult(False, str(response.url), status, chunk, hdrs, latency, "blocked_or_auth", f"HTTP {status}")
                    elif status in {404, 410}:
                        result = HttpResult(False, str(response.url), status, chunk, hdrs, latency, "not_found", f"HTTP {status}")
                    elif 500 <= status <= 599:
                        result = HttpResult(False, str(response.url), status, chunk, hdrs, latency, "server_error", f"HTTP {status}")
                    else:
                        result = HttpResult(False, str(response.url), status, chunk, hdrs, latency, "http_error", f"HTTP {status}")
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                result = HttpResult(False, url, None, b"", {}, (time.perf_counter() - start) * 1000.0, "timeout", type(exc).__name__)
            except aiohttp.TooManyRedirects as exc:
                result = HttpResult(False, url, None, b"", {}, (time.perf_counter() - start) * 1000.0, "too_many_redirects", redact_text(str(exc)))
            except (aiohttp.ClientConnectorDNSError, socket.gaierror) as exc:
                result = HttpResult(False, url, None, b"", {}, (time.perf_counter() - start) * 1000.0, "dns_error", redact_text(str(exc)))
            except aiohttp.ClientSSLError as exc:
                result = HttpResult(False, url, None, b"", {}, (time.perf_counter() - start) * 1000.0, "tls_error", redact_text(str(exc)))
            except (aiohttp.ClientConnectionError, ConnectionResetError, BrokenPipeError) as exc:
                result = HttpResult(False, url, None, b"", {}, (time.perf_counter() - start) * 1000.0, "transport_error", redact_text(str(exc)))
            except aiohttp.ClientError as exc:
                result = HttpResult(False, url, None, b"", {}, (time.perf_counter() - start) * 1000.0, "client_error", redact_text(str(exc)))
            except Exception as exc:
                logger.exception("unclassified HTTP exception url=%s", redact_text(url))
                result = HttpResult(False, url, None, b"", {}, (time.perf_counter() - start) * 1000.0, "unexpected_error", redact_text(f"{type(exc).__name__}: {exc}"))

            last = result
            retryable = result.classification in {"rate_limited", "server_error", "timeout", "dns_error", "tls_error", "transport_error", "client_error", "truncated_body"}
            if not retryable or attempt >= tries:
                break
            delay = result.retry_after if result.retry_after is not None else min(30.0, 0.5 * (2**attempt)) + random.random() * 0.35
            await asyncio.sleep(max(0.05, delay))

        assert last is not None
        if last.classification in {"not_found", "blocked_or_auth", "too_many_redirects"}:
            self.circuit.failure(host, last.classification)
        elif last.classification not in {"rate_limited"}:
            self.circuit.failure(host, last.classification)
        return last

    async def get(self, url: str, **kwargs: Any) -> HttpResult:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> HttpResult:
        return await self.request("POST", url, **kwargs)
