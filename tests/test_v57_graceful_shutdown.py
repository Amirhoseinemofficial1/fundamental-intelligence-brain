from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from v52.news.discovery import DiscoveryService
from v52.news.sitemap import SitemapService


class _Metrics:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.values: dict[str, float] = {}

    def inc(self, name: str, value: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + value

    def set(self, name: str, value) -> None:
        self.values[name] = value


class _Http:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def _exercise_sitemap_shutdown() -> tuple[float, _Metrics, _Http]:
    svc = SitemapService.__new__(SitemapService)
    svc.settings = SimpleNamespace(
        sitemap_enabled=True,
        sitemap_concurrency=2,
        sitemap_sites_per_cycle=2,
        sitemap_cycle_seconds=60.0,
    )
    svc.metrics = _Metrics()
    svc.http = _Http()
    svc.sites = [object(), object()]
    svc.cursor = 0

    started = asyncio.Event()

    async def slow_probe(_site) -> None:
        started.set()
        await asyncio.sleep(30.0)

    svc._probe_site = slow_probe  # type: ignore[method-assign]
    stop = asyncio.Event()
    task = asyncio.create_task(svc.run(stop))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    t0 = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    return time.monotonic() - t0, svc.metrics, svc.http


async def _exercise_discovery_shutdown() -> tuple[float, _Metrics, _Http]:
    svc = DiscoveryService.__new__(DiscoveryService)
    svc.settings = SimpleNamespace(
        discovery_enabled=True,
        discovery_max_sites_per_cycle=2,
        news_discovery_concurrency=2,
        discovery_cycle_seconds=60.0,
    )
    svc.metrics = _Metrics()
    svc.http = _Http()
    svc.registry = SimpleNamespace(discovery_sites=lambda: [{"url": "https://a.invalid"}, {"url": "https://b.invalid"}])

    started = asyncio.Event()

    async def slow_probe(_row) -> None:
        started.set()
        await asyncio.sleep(30.0)

    svc._probe = slow_probe  # type: ignore[method-assign]
    stop = asyncio.Event()
    task = asyncio.create_task(svc.run(stop))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    t0 = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    return time.monotonic() - t0, svc.metrics, svc.http


def test_sitemap_inflight_batch_is_cancelled_on_shutdown() -> None:
    elapsed, metrics, http = asyncio.run(_exercise_sitemap_shutdown())
    assert elapsed < 0.8
    assert metrics.counters.get("sitemap_shutdown_inflight_cancel", 0) >= 1
    assert http.closed is True


def test_discovery_inflight_batch_is_cancelled_on_shutdown() -> None:
    elapsed, metrics, http = asyncio.run(_exercise_discovery_shutdown())
    assert elapsed < 0.8
    assert metrics.counters.get("discovery_shutdown_inflight_cancel", 0) >= 1
    assert http.closed is True


def test_preflight_scopes_pip_check_to_project_dependency_owner(monkeypatch) -> None:
    from v52.diagnostics import preflight

    class _Result:
        returncode = 1
        stdout = "moviepy 2.2.1 has requirement pillow<12.0,>=9.2.0, but you have pillow 12.3.0.\n"
        stderr = ""

    monkeypatch.setattr(preflight.subprocess, "run", lambda *args, **kwargs: _Result())
    monkeypatch.setattr(preflight, "_project_dependency_closure", lambda: {"aiohttp", "pillow"})
    result, errors = preflight._check_package_consistency()
    assert errors == []
    assert result["ok"] is True
    assert result["ignored_unrelated_conflicts"]


def test_preflight_still_fails_project_owned_pip_conflict(monkeypatch) -> None:
    from v52.diagnostics import preflight

    class _Result:
        returncode = 1
        stdout = "aiohttp 3.13.3 has requirement yarl<2,>=1.17.0, but you have yarl 2.1.0.\n"
        stderr = ""

    monkeypatch.setattr(preflight.subprocess, "run", lambda *args, **kwargs: _Result())
    monkeypatch.setattr(preflight, "_project_dependency_closure", lambda: {"aiohttp", "yarl"})
    result, errors = preflight._check_package_consistency()
    assert result["ok"] is False
    assert len(errors) == 1
    assert "aiohttp" in errors[0]
