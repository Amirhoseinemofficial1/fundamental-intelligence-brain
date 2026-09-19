from __future__ import annotations

import asyncio
import gzip
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..metrics import Metrics
from .store import BrainStore
from ..storage.control import ControlStore
from .telegram_producers import TelegramIntelligenceProducers


class MaintenanceService:
    """Bounded storage lifecycle with recoverable compression and disk guards."""

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.brain = BrainStore(settings)
        self.telegram = TelegramIntelligenceProducers(ControlStore(settings))
        self.compress_after = max(60.0, float(os.getenv("MARKET_RAW_COMPRESS_AFTER_SECONDS", "600")))
        self.raw_retention_days = max(1, int(os.getenv("MARKET_RAW_RETENTION_DAYS", "14")))
        self.db_retention_days = max(1, int(os.getenv("OPERATIONAL_DB_RETENTION_DAYS", "30")))
        self.news_done_retention_seconds = max(300, int(os.getenv("NEWS_DONE_RETENTION_SECONDS", "1800")))
        self.news_pending_retention_seconds = max(300, int(os.getenv("NEWS_PENDING_RETENTION_SECONDS", "900")))
        self.control_retention_seconds = max(3600, int(os.getenv("CONTROL_STORY_RETENTION_SECONDS", "21600")))
        self.structured_retention_seconds = max(3600, int(os.getenv("STRUCTURED_RETENTION_SECONDS", "21600")))
        self.brain_audit_retention_seconds = max(86400, int(os.getenv("BRAIN_AUDIT_RETENTION_SECONDS", "172800")))
        self.reference_price_retention_days = max(
            3, int(os.getenv("REFERENCE_PRICE_RETENTION_DAYS", "7"))
        )
        self.market_semantic_retention_days = max(.25, float(os.getenv("MARKET_SEMANTIC_RETENTION_DAYS", "3")))
        self.runtime_artifact_retention_days = max(1, int(os.getenv("RUNTIME_ARTIFACT_RETENTION_DAYS", "3")))
        self.archive_retention_days = max(self.raw_retention_days, int(os.getenv("MARKET_ARCHIVE_RETENTION_DAYS", "30")))
        self.high_watermark = min(.95, max(.5, float(os.getenv("DISK_HIGH_WATERMARK_RATIO", ".85"))))
        self.critical_watermark = min(.99, max(self.high_watermark, float(os.getenv("DISK_CRITICAL_WATERMARK_RATIO", ".93"))))

    @staticmethod
    def _compress_file(path: Path) -> int:
        target = path.with_suffix(path.suffix + ".gz")
        temporary = target.with_suffix(target.suffix + ".tmp")
        if target.exists():
            return 0
        with path.open("rb") as source, temporary.open("wb") as raw_output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, compresslevel=6, mtime=0) as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        with temporary.open("rb") as check:
            check.read(1)
        os.replace(temporary, target)
        # The original is removed only after the completed compressed file is atomically visible.
        size = path.stat().st_size
        path.unlink()
        return size

    def _compress_segments_sync(self) -> dict[str, int]:
        now = time.time()
        compressed = 0
        source_bytes = 0
        for directory in self.settings.data_dir.glob("market/market_*/raw"):
            paths = sorted(directory.glob("wire-*.jsonl"))
            # Highest segment may be the active writer and is never touched.
            for path in paths[:-1]:
                if now - path.stat().st_mtime < self.compress_after:
                    continue
                source_bytes += self._compress_file(path)
                compressed += 1
        return {"compressed_segments": compressed, "source_bytes": source_bytes}

    @staticmethod
    def _delete_batch(con: sqlite3.Connection, table: str, where: str, params: tuple[Any, ...], limit: int = 50_000) -> int:
        cur = con.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT ?)", (*params, limit))
        return max(0, int(cur.rowcount))

    def _prune_databases_sync(self) -> dict[str, int]:
        cutoff = time.time() - self.db_retention_days * 86400
        now = time.time()
        counts: dict[str, int] = {}
        news = self.settings.data_dir / "db" / "news.sqlite3"
        if news.exists():
            con = sqlite3.connect(news, timeout=15)
            counts["raw_news_done"] = self._delete_batch(
                con, "raw_news", "state='done' AND processed_at IS NOT NULL AND processed_at<?",
                (now - self.news_done_retention_seconds,),
            )
            counts["raw_news_stale_pending"] = self._delete_batch(
                con, "raw_news", "state='pending' AND ingested_at<?",
                (now - self.news_pending_retention_seconds,),
            )
            con.commit(); con.execute("PRAGMA wal_checkpoint(PASSIVE)"); con.execute("PRAGMA incremental_vacuum(2048)"); con.close()
        control = self.settings.data_dir / "db" / "control.sqlite3"
        if control.exists():
            con = sqlite3.connect(control, timeout=15)
            con.execute("PRAGMA foreign_keys=ON")
            control_cutoff = now - self.control_retention_seconds
            counts["alerts"] = self._delete_batch(con, "alerts", "state IN ('sent','dead','expired') AND created_at<?", (control_cutoff,))
            counts["translation_jobs"] = self._delete_batch(con, "translation_jobs", "state IN ('done','dead') AND created_at<?", (control_cutoff,))
            counts["enrichment_jobs"] = self._delete_batch(con, "enrichment_jobs", "state IN ('done','dead') AND created_at<?", (control_cutoff,))
            # Canonical events and their learned semantic representations live
            # in the brain database.  The control-plane copies are short-lived
            # delivery material and can be deleted with their cascading
            # revisions/occurrences/jobs after the operational window.
            counts["stories"] = self._delete_batch(
                con,
                "stories",
                """updated_at<?
                   AND NOT EXISTS(SELECT 1 FROM alerts WHERE alerts.story_id=stories.id AND state IN ('pending','leased'))
                   AND NOT EXISTS(SELECT 1 FROM translation_jobs WHERE translation_jobs.story_id=stories.id AND state IN ('pending','leased'))
                   AND NOT EXISTS(SELECT 1 FROM enrichment_jobs WHERE enrichment_jobs.story_id=stories.id AND state IN ('pending','leased'))""",
                (control_cutoff,),
            )
            counts["translation_cache"] = self._delete_batch(
                con, "translation_cache", "last_used_at<?", (control_cutoff,)
            )
            con.commit(); con.execute("PRAGMA wal_checkpoint(PASSIVE)"); con.execute("PRAGMA incremental_vacuum(2048)"); con.close()
        structured = self.settings.data_dir / "db" / "structured.sqlite3"
        if structured.exists():
            con = sqlite3.connect(structured, timeout=15)
            counts["structured_snapshots"] = self._delete_batch(
                con, "snapshots", "observed_at<?", (now - self.structured_retention_seconds,)
            )
            con.commit(); con.execute("PRAGMA incremental_vacuum(2048)"); con.close()
        market_cutoff=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(time.time()-self.market_semantic_retention_days*86400))
        for path in self.settings.data_dir.glob("market/market_*/semantic.sqlite3"):
            con=sqlite3.connect(path,timeout=30)
            deleted=self._delete_batch(con,"events","observed_at<?",(market_cutoff,))
            counts[f"market:{path.parent.name}"]=deleted
            con.commit();con.execute("PRAGMA wal_checkpoint(PASSIVE)");con.execute("PRAGMA incremental_vacuum(2048)");con.close()
        brain = self.settings.data_dir / "brain" / "intelligence.sqlite3"
        if brain.exists():
            con = sqlite3.connect(brain, timeout=30)
            con.execute("PRAGMA foreign_keys=ON")
            reference_cutoff = time.time() - self.reference_price_retention_days * 86400
            counts["reference_prices"] = self._delete_batch(
                con, "reference_prices", "observed_at<?", (reference_cutoff,)
            )
            counts["brain_audit"] = self._delete_batch(
                con, "audit_log", "created_at<?", (now - self.brain_audit_retention_seconds,)
            )
            counts["response_unobservable"] = self._delete_batch(
                con, "response_targets", "state='unobservable' AND due_at<?", (now - self.control_retention_seconds,)
            )
            con.commit(); con.execute("PRAGMA wal_checkpoint(PASSIVE)"); con.execute("PRAGMA incremental_vacuum(2048)"); con.close()
        return counts

    def _prune_runtime_artifacts_sync(self) -> dict[str,int]:
        cutoff=time.time()-self.runtime_artifact_retention_days*86400
        removed=bytes_removed=0
        for root_name in ("runs","logs"):
            root=self.settings.data_dir/root_name
            for path in root.iterdir() if root.is_dir() else ():
                if not path.is_dir() or path.stat().st_mtime>=cutoff:continue
                size=sum(item.stat().st_size for item in path.rglob('*') if item.is_file())
                shutil.rmtree(path);removed+=1;bytes_removed+=size
        return {"runtime_directories_removed":removed,"runtime_bytes_removed":bytes_removed}

    def _prune_archives_sync(self, emergency: bool = False) -> dict[str, int]:
        retention = min(self.archive_retention_days, 3) if emergency else self.archive_retention_days
        cutoff = time.time() - retention * 86400
        removed = 0
        bytes_removed = 0
        for path in self.settings.data_dir.glob("market/market_*/raw/wire-*.jsonl.gz"):
            if path.stat().st_mtime < cutoff:
                bytes_removed += path.stat().st_size
                path.unlink()
                removed += 1
        return {"removed_archives": removed, "bytes_removed": bytes_removed}

    async def cycle(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.settings.data_dir)
        ratio = 1.0 - usage.free / max(1, usage.total)
        compressed = await asyncio.to_thread(self._compress_segments_sync)
        pruned = await asyncio.to_thread(self._prune_databases_sync)
        archives = await asyncio.to_thread(self._prune_archives_sync, ratio >= self.critical_watermark)
        runtime = await asyncio.to_thread(self._prune_runtime_artifacts_sync)
        if ratio >= self.high_watermark:
            await self.brain.set_meta("signals_enabled", "0")
            await self.brain.audit("maintenance", "DISK_GUARD", "filesystem", str(self.settings.data_dir), {"ratio": ratio, "critical": ratio >= self.critical_watermark})
            if self.settings.telegram_enabled:await self.telegram.produce({'critical_health_failure':True,'summary':f'Disk guard active: used ratio {ratio:.3f}','asset_id':'SYSTEM','event_id':f'disk:{int(ratio*1000)}'})
        result = {"disk_used_ratio": ratio, "high_watermark": self.high_watermark, "critical_watermark": self.critical_watermark, **compressed, "db_pruned": pruned, **archives, **runtime}
        self.metrics.set("storage_lifecycle", result)
        return result

    async def run(self, stop_event: asyncio.Event) -> None:
        interval = max(60.0, float(os.getenv("MAINTENANCE_CYCLE_SECONDS", "300")))
        while not stop_event.is_set():
            try:
                await self.cycle()
                self.metrics.inc("maintenance_cycle_completed")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.metrics.inc("maintenance_cycle_error")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
