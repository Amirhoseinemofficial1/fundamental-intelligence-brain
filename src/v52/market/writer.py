from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..metrics import Metrics
from ..models import MarketEvent
from ..storage.market import MarketSemanticStore
from ..timeutil import iso_now

logger = logging.getLogger(__name__)


class RawWireJournal:
    """Lossless-with-backpressure raw wire journal for one market process.

    Network producers await queue.put(). The writer performs batched blocking disk IO in a worker
    thread. A writer failure is fatal to future producers instead of becoming a silent drop.
    Graceful stop drains the queue and fdatasyncs all accepted records.
    """

    def __init__(self, settings: Settings, metrics: Metrics, service: str):
        self.settings = settings
        self.metrics = metrics
        self.service = service
        self.dir = settings.data_dir / "market" / service / "raw"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=settings.market_raw_queue_size)
        self.task: asyncio.Task[Any] | None = None
        self.fd: int | None = None
        existing = []
        for path in self.dir.glob("wire-*.jsonl"):
            try:
                existing.append(int(path.stem.split("-")[-1]))
            except (ValueError, IndexError):
                logger.warning("ignoring malformed raw-wire segment name service=%s path=%s", service, path.name)
        self.segment = max(existing, default=0)
        self.segment_bytes = 0
        self.max_segment_bytes = int(os.getenv("MARKET_RAW_SEGMENT_BYTES", str(1_000_000_000)))
        self._written_records = 0
        self._synced_records = 0
        self._bytes_since_sync = 0
        self._last_sync = time.monotonic()
        self._fatal: BaseException | None = None
        self._accepting = False
        self._enqueued_records = 0
        self.enabled = os.getenv("MARKET_RAW_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}

    def _open_segment(self) -> None:
        if self.fd is not None:
            sync = getattr(os, "fdatasync", os.fsync)
            sync(self.fd)
            os.close(self.fd)
        self.segment += 1
        path = self.dir / f"wire-{self.segment:06d}.jsonl"
        self.fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o640)
        self.segment_bytes = os.path.getsize(path) if path.exists() else 0

    def _write_batch_sync(self, batch: list[bytes], force_sync: bool = False) -> tuple[int, int, bool]:
        if self.fd is None:
            self._open_segment()
        assert self.fd is not None
        bytes_written = 0
        records = 0
        for row in batch:
            if self.segment_bytes + len(row) > self.max_segment_bytes and self.segment_bytes > 0:
                self._open_segment()
            view = memoryview(row)
            while view:
                n = os.write(self.fd, view)
                if n <= 0:
                    raise OSError("short raw-wire write")
                view = view[n:]
                bytes_written += n
                self.segment_bytes += n
            records += 1
        self._bytes_since_sync += bytes_written
        now = time.monotonic()
        should_sync = force_sync or self._bytes_since_sync >= self.settings.market_raw_sync_bytes or (now - self._last_sync) * 1000 >= self.settings.market_raw_sync_interval_ms
        if should_sync:
            sync = getattr(os, "fdatasync", os.fsync)
            sync(self.fd)
            self._last_sync = now
            self._bytes_since_sync = 0
        return records, bytes_written, should_sync

    async def start(self) -> None:
        self._accepting = True
        if not self.enabled:
            self.metrics.set("market_raw_disabled", 1)
            return
        self.task = asyncio.create_task(self._writer_loop(), name=f"{self.service}:raw-wire-writer")

    async def append(self, venue: str, stream: str, symbol: str, raw: str | bytes, metadata: dict[str, Any] | None = None) -> None:
        if not self._accepting:
            raise RuntimeError("raw wire journal is not accepting records")
        if not self.enabled:
            self.metrics.inc("market_raw_filtered")
            return
        if self._fatal is not None:
            if self.task is not None and self.task.done():
                self.task.exception()
            raise RuntimeError("raw wire writer failed") from self._fatal
        if self.task is not None and self.task.done():
            exc = self.task.exception()
            raise RuntimeError("raw wire writer terminated") from exc
        if isinstance(raw, bytes):
            payload: Any = {"encoding": "base64", "data": base64.b64encode(raw).decode("ascii")}
        else:
            payload = raw
        obj = {
            "schema": "52.0-market-raw-wire",
            "observed_at": iso_now(),
            "monotonic_ns": time.monotonic_ns(),
            "venue": venue,
            "stream": stream,
            "symbol": symbol,
            "metadata": metadata or {},
            "raw": payload,
        }
        row = (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        await self.queue.put(row)
        self._enqueued_records += 1
        self.metrics.inc("market_raw_enqueued")
        self.metrics.set("market_raw_qsize", self.queue.qsize())

    async def _writer_loop(self) -> None:
        try:
            batch: list[bytes] = []
            while True:
                item = await self.queue.get()
                if item is None:
                    self.queue.task_done()
                    break
                batch.append(item)
                self.queue.task_done()
                # Collect a micro-batch without blocking.
                while len(batch) < 512:
                    try:
                        nxt = self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nxt is None:
                        self.queue.task_done()
                        # Re-insert sentinel after batch flush.
                        await self.queue.put(None)
                        break
                    batch.append(nxt)
                    self.queue.task_done()
                records, nbytes, synced = await asyncio.to_thread(self._write_batch_sync, batch, False)
                self._written_records += records
                self.metrics.inc("market_raw_written", records)
                self.metrics.inc("market_raw_bytes", nbytes)
                if synced:
                    self._synced_records = self._written_records
                    self.metrics.set("market_raw_synced", self._synced_records)
                batch.clear()
                self.metrics.set("market_raw_qsize", self.queue.qsize())
            # Queue sentinel is observed only after all previously queued records by FIFO ordering.
            if batch:
                records, nbytes, _ = await asyncio.to_thread(self._write_batch_sync, batch, False)
                self._written_records += records
                self.metrics.inc("market_raw_written", records)
                self.metrics.inc("market_raw_bytes", nbytes)
            await asyncio.to_thread(self._write_batch_sync, [], True)
            self._synced_records = self._written_records
            self.metrics.set("market_raw_synced", self._synced_records)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fatal = exc
            self.metrics.inc("market_raw_writer_fatal")
            logger.exception("raw wire writer fatal service=%s", self.service)
            raise

    async def stop(self) -> None:
        self._accepting = False
        if not self.enabled:
            self.metrics.set("market_raw_invariant_ok", 1)
            return
        if self.task is None:
            return
        if self._fatal is not None:
            if self.task.done():
                self.task.exception()
            raise RuntimeError("raw wire writer already failed") from self._fatal
        await self.queue.put(None)
        await asyncio.wait_for(self.task, timeout=self.settings.shutdown_grace_seconds)
        if self.fd is not None:
            await asyncio.to_thread(os.close, self.fd)
            self.fd = None
        self.metrics.set("market_raw_qsize", self.queue.qsize())
        if not (self._enqueued_records == self._written_records == self._synced_records):
            raise RuntimeError(
                f"raw wire durability invariant failed enqueued={self._enqueued_records} "
                f"written={self._written_records} synced={self._synced_records}"
            )
        self.metrics.set("market_raw_invariant_ok", 1)


class SemanticWriter:
    def __init__(self, settings: Settings, metrics: Metrics, service: str):
        self.settings = settings
        self.metrics = metrics
        self.service = service
        self.store = MarketSemanticStore(settings, service)
        self.queue: asyncio.Queue[MarketEvent | None] = asyncio.Queue(maxsize=max(10_000, settings.market_raw_queue_size // 4))
        self.task: asyncio.Task[Any] | None = None
        self._fatal: BaseException | None = None
        self._accepting = False
        self._enqueued = 0
        self._persisted = 0

    async def start(self) -> None:
        self._accepting = True
        self.task = asyncio.create_task(self._loop(), name=f"{self.service}:semantic-writer")

    async def append(self, event: MarketEvent) -> None:
        if not self._accepting:
            raise RuntimeError("semantic writer not accepting")
        if self._fatal is not None:
            if self.task is not None and self.task.done():
                self.task.exception()
            raise RuntimeError("semantic writer failed") from self._fatal
        if self.task is not None and self.task.done():
            exc = self.task.exception()
            raise RuntimeError("semantic writer terminated") from exc
        await self.queue.put(event)
        self._enqueued += 1
        self.metrics.inc("market_semantic_enqueued")
        self.metrics.set("market_semantic_qsize", self.queue.qsize())

    async def _loop(self) -> None:
        try:
            while True:
                item = await self.queue.get()
                if item is None:
                    self.queue.task_done()
                    break
                batch = [item]
                self.queue.task_done()
                while len(batch) < self.settings.market_semantic_batch:
                    try:
                        nxt = self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nxt is None:
                        self.queue.task_done()
                        await self.queue.put(None)
                        break
                    batch.append(nxt)
                    self.queue.task_done()
                inserted = await asyncio.to_thread(self.store.insert_many, batch)
                self._persisted += inserted
                self.metrics.inc("market_semantic_persisted", inserted)
                self.metrics.set("market_semantic_qsize", self.queue.qsize())
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fatal = exc
            self.metrics.inc("market_semantic_writer_fatal")
            logger.exception("semantic writer fatal service=%s", self.service)
            raise

    async def stop(self) -> None:
        self._accepting = False
        if self.task is None:
            return
        if self._fatal is not None:
            if self.task.done():
                self.task.exception()
            raise RuntimeError("semantic writer failed") from self._fatal
        await self.queue.put(None)
        await asyncio.wait_for(self.task, timeout=self.settings.shutdown_grace_seconds)
        self.metrics.set("market_semantic_qsize", self.queue.qsize())
        self.store.close()
        if self._enqueued != self._persisted:
            raise RuntimeError(f"semantic durability invariant failed enqueued={self._enqueued} persisted={self._persisted}")
        self.metrics.set("market_semantic_invariant_ok", 1)
