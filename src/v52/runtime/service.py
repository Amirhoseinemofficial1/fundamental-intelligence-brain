from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..constants import MARKET_SERVICES
from ..loggingx import configure_logging
from ..metrics import Metrics
from .health import proc_start_ticks, proc_stats

logger = logging.getLogger(__name__)


async def status_loop(settings: Settings, metrics: Metrics, service: str, stop_event: asyncio.Event, parent_pid: int | None, parent_start: int | None) -> None:
    last_loop = time.monotonic()
    while not stop_event.is_set():
        now = time.monotonic()
        lag = max(0.0, now - last_loop - settings.service_status_seconds)
        metrics.set("event_loop_status_lag_ms", lag * 1000.0)
        metrics.set("proc", proc_stats())
        metrics.set("service_state", "running")
        metrics.write_status({"parent_pid": parent_pid, "parent_start_ticks": parent_start})
        # PID start-tick identity is a Linux /proc contract. On platforms without
        # /proc both values are unavailable, so liveness remains supervisor-owned.
        if parent_pid is not None and parent_start is not None:
            actual = proc_start_ticks(parent_pid)
            if actual is None or (parent_start is not None and actual != parent_start):
                logger.error("supervisor identity lost expected_pid=%s expected_start=%s actual_start=%s", parent_pid, parent_start, actual)
                metrics.inc("supervisor_identity_lost")
                stop_event.set()
                return
        last_loop = time.monotonic()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.service_status_seconds)
        except asyncio.TimeoutError:
            continue


def build_runner(service: str, settings: Settings, metrics: Metrics) -> Any:
    # Import only the implementation needed by this process. With 20+ isolated
    # workers, eager cross-plane imports multiply RSS and startup cost.
    if service in {"news_hot", "news_coverage"}:
        from ..news.collector import AdaptiveNewsCollector
        return AdaptiveNewsCollector(settings, metrics, "hot" if service == "news_hot" else "coverage")
    if service == "gdelt":
        from ..news.gdelt import GdeltService
        return GdeltService(settings, metrics)
    if service == "google_mesh":
        from ..news.google_mesh import GoogleMeshService
        return GoogleMeshService(settings, metrics)
    if service == "x_social":
        from ..news.social_x import XSocialService
        return XSocialService(settings, metrics)
    if service == "bluesky":
        from ..news.social_bluesky import BlueskyService
        return BlueskyService(settings, metrics)
    if service == "social_pollers":
        from ..news.social_pollers import SocialPollerService
        return SocialPollerService(settings, metrics)
    if service == "sitemap":
        from ..news.sitemap import SitemapService
        return SitemapService(settings, metrics)
    if service == "news_processor":
        from ..news.processor import NewsProcessor
        return NewsProcessor(settings, metrics)
    if service == "intelligence":
        from ..intelligence.service import IntelligenceService
        return IntelligenceService(settings, metrics)
    if service == "market_response":
        from ..intelligence.market_response import MarketResponseService
        return MarketResponseService(settings, metrics)
    if service == "active_signal_monitor":
        from ..intelligence.active_signal_monitor import ActiveSignalMonitorService
        return ActiveSignalMonitorService(settings, metrics)
    if service == "research":
        from ..intelligence.research import ResearchService
        return ResearchService(settings, metrics)
    if service == "news_enrichment":
        from ..news.enrichment import EnrichmentService
        return EnrichmentService(settings, metrics)
    if service == "translation":
        from ..translation.service import TranslationService
        return TranslationService(settings, metrics)
    if service == "alerts":
        from ..alerts.service import AlertService
        return AlertService(settings, metrics)
    if service == "structured":
        from ..structured.service import StructuredService
        return StructuredService(settings, metrics)
    if service == "institutional_connectors":
        from ..structured.institutional import InstitutionalConnectorService
        return InstitutionalConnectorService(settings, metrics)
    if service == "provider_flows":
        from ..structured.provider_flows import ProviderFlowService
        return ProviderFlowService(settings, metrics)
    if service == "premium_actions":
        from ..structured.premium_actions import PremiumActionsService
        return PremiumActionsService(settings, metrics)
    if service == "prediction":
        from ..structured.prediction import PredictionMarketService
        return PredictionMarketService(settings, metrics)
    if service in {"official_native", "altdata_native", "filings_native"}:
        from ..structured.native import NativePublicService
        lane = {"official_native": "official", "altdata_native": "altdata", "filings_native": "filings"}[service]
        return NativePublicService(settings, metrics, lane)
    if service == "discovery":
        from ..news.discovery import DiscoveryService
        return DiscoveryService(settings, metrics)
    if service == "maintenance":
        from ..intelligence.maintenance import MaintenanceService
        return MaintenanceService(settings, metrics)
    if service in MARKET_SERVICES:
        from ..market.service import MarketRuntime
        exchange = service.removeprefix("market_")
        return MarketRuntime(settings, metrics, service, exchange)
    raise ValueError(f"unknown service {service}")


async def run_service(service: str, run_seconds: float = 0.0) -> int:
    settings = Settings.from_env()
    settings.ensure_directories()
    run_id = os.getenv("V52_RUN_ID", "standalone")
    log_dir = settings.log_dir / run_id if run_id != "standalone" else settings.log_dir
    configure_logging(service, log_dir, settings.log_level)
    metrics = Metrics(service, settings.state_dir)
    metrics.set("run_id", run_id)
    stop_event = asyncio.Event()
    parent_pid = int(os.getenv("V52_SUPERVISOR_PID", "0") or 0) or None
    parent_start = int(os.getenv("V52_SUPERVISOR_START_TICKS", "0") or 0) or None

    def request_stop(sig: signal.Signals) -> None:
        logger.info("stop requested signal=%s", sig.name)
        metrics.set("stop_signal", sig.name)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop, sig)
        except NotImplementedError:
            metrics.inc("signal_handler_unsupported")
            logger.warning("event-loop signal handlers unsupported on this platform signal=%s", sig.name)

    runner = build_runner(service, settings, metrics)

    status_task = asyncio.create_task(status_loop(settings, metrics, service, stop_event, parent_pid, parent_start), name=f"status:{service}")
    timer_task: asyncio.Task[Any] | None = None
    if run_seconds > 0:
        async def timer() -> None:
            await asyncio.sleep(run_seconds)
            metrics.set("stop_reason", "run_seconds")
            stop_event.set()
        timer_task = asyncio.create_task(timer(), name=f"timer:{service}")

    exit_code = 0
    error = ""
    try:
        if service in MARKET_SERVICES:
            await runner.run_exchange(stop_event)
        else:
            await runner.run(stop_event)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        exit_code = 1
        error = f"{type(exc).__name__}: {exc}"
        metrics.inc("service_fatal")
        logger.exception("service fatal service=%s", service)
    finally:
        stop_event.set()
        if timer_task:
            timer_task.cancel()
        status_task.cancel()
        await asyncio.gather(*(x for x in (timer_task, status_task) if x is not None), return_exceptions=True)
        metrics.set("service_state", "stopped" if exit_code == 0 else "failed")
        metrics.set("exit_code", exit_code)
        if error:
            metrics.set("fatal_error", error)
        metrics.set("proc_final", proc_stats())
        payload = metrics.snapshot()
        payload["exit_code"] = exit_code
        payload["fatal_error"] = error
        service_state = settings.state_dir / service
        service_state.mkdir(parents=True, exist_ok=True)
        final = service_state / "final.json"
        tmp = final.with_suffix(".tmp")
        payload["run_id"] = run_id
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, final)
        if run_id != "standalone":
            run_final = settings.run_dir / run_id / "services" / service / "final.json"
            run_final.parent.mkdir(parents=True, exist_ok=True)
            run_tmp = run_final.with_suffix(".tmp")
            run_tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(run_tmp, run_final)
        metrics.write_status({"final": True, "run_id": run_id})
    return exit_code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("service")
    ap.add_argument("--run-seconds", type=float, default=0.0)
    args = ap.parse_args()
    return asyncio.run(run_service(args.service, args.run_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
