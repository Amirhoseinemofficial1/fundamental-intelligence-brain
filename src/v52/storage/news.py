from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import RawNewsItem
from .sqlite import connect, serialized_initialization, transaction


class NewsStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.run_id = os.getenv("V52_RUN_ID", "standalone")
        self.max_payload_bytes = max(8_192, int(os.getenv("NEWS_MAX_PAYLOAD_BYTES", "65536")))
        self.source_window_seconds = max(60, int(os.getenv("NEWS_SOURCE_WINDOW_SECONDS", "300")))
        self.max_rows_per_source_window = max(1, int(os.getenv("NEWS_MAX_ROWS_PER_SOURCE_WINDOW", "10000")))
        self.path = settings.data_dir / "db" / "news.sqlite3"
        self._init_sync()

    @serialized_initialization
    def _init_sync(self) -> None:
        with connect(self.path, self.settings) as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS raw_news(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL DEFAULT 'legacy',
                    raw_key TEXT NOT NULL UNIQUE,
                    source_key TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    item_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    category TEXT NOT NULL,
                    region TEXT NOT NULL,
                    language TEXT NOT NULL,
                    published_at TEXT,
                    observed_at TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    lease_until REAL,
                    lease_owner TEXT,
                    last_error TEXT,
                    processed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_raw_news_pending
                    ON raw_news(state,next_attempt,priority DESC,id);
                CREATE TABLE IF NOT EXISTS source_health(
                    source_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    successes INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    rate_limits INTEGER NOT NULL DEFAULT 0,
                    timeouts INTEGER NOT NULL DEFAULT 0,
                    new_items INTEGER NOT NULL DEFAULT 0,
                    parsed_items INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT,
                    last_http_status INTEGER,
                    last_latency_ms REAL,
                    last_seen_at REAL,
                    last_success_at REAL,
                    last_new_at REAL,
                    next_due REAL NOT NULL DEFAULT 0,
                    adaptive_poll_seconds REAL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS discovered_feeds(
                    url TEXT PRIMARY KEY,
                    source_page TEXT NOT NULL,
                    discovered_at REAL NOT NULL,
                    last_verified_at REAL,
                    status TEXT NOT NULL DEFAULT 'candidate'
                );
                CREATE TABLE IF NOT EXISTS source_cursors(
                    source_key TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_http_cache(
                    source_key TEXT PRIMARY KEY,
                    etag TEXT,
                    last_modified TEXT,
                    updated_at REAL NOT NULL
                );
                """
            )
            columns = {str(r["name"]) for r in con.execute("PRAGMA table_info(raw_news)").fetchall()}
            if "run_id" not in columns:
                con.execute("ALTER TABLE raw_news ADD COLUMN run_id TEXT NOT NULL DEFAULT 'legacy'")
            if "ingested_at" not in columns:
                con.execute("ALTER TABLE raw_news ADD COLUMN ingested_at REAL NOT NULL DEFAULT 0")
            con.execute("CREATE INDEX IF NOT EXISTS idx_raw_news_run ON raw_news(run_id,id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_raw_news_source_window ON raw_news(source_name,ingested_at)")

    async def pending_count(self) -> int:
        return await asyncio.to_thread(self._pending_count_sync)

    def _pending_count_sync(self) -> int:
        with connect(self.path, self.settings) as con:
            row = con.execute("SELECT COUNT(*) c FROM raw_news WHERE state IN ('pending','leased')").fetchone()
            return int(row["c"])

    async def http_cache_headers(self, source_key: str) -> dict[str, str]:
        return await asyncio.to_thread(self._http_cache_headers_sync, source_key)

    def _http_cache_headers_sync(self, source_key: str) -> dict[str, str]:
        with connect(self.path, self.settings) as con:
            row = con.execute("SELECT etag,last_modified FROM source_http_cache WHERE source_key=?", (source_key,)).fetchone()
            if not row:
                return {}
            headers: dict[str, str] = {}
            if row["etag"]:
                headers["If-None-Match"] = str(row["etag"])
            if row["last_modified"]:
                headers["If-Modified-Since"] = str(row["last_modified"])
            return headers

    async def update_http_cache(self, source_key: str, headers: dict[str, str]) -> None:
        await asyncio.to_thread(self._update_http_cache_sync, source_key, headers)

    def _update_http_cache_sync(self, source_key: str, headers: dict[str, str]) -> None:
        etag = headers.get("ETag") or headers.get("Etag") or headers.get("etag")
        modified = headers.get("Last-Modified") or headers.get("last-modified")
        if not etag and not modified:
            return
        with connect(self.path, self.settings) as con:
            con.execute(
                """INSERT INTO source_http_cache(source_key,etag,last_modified,updated_at) VALUES(?,?,?,?)
                   ON CONFLICT(source_key) DO UPDATE SET etag=COALESCE(excluded.etag,etag),
                   last_modified=COALESCE(excluded.last_modified,last_modified),updated_at=excluded.updated_at""",
                (source_key, etag, modified, time.time()),
            )

    def _bounded_payload(self, payload: Any) -> str:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        raw = text.encode("utf-8")
        if len(raw) <= self.max_payload_bytes:
            return text
        digest = hashlib.sha256(raw).hexdigest()
        if isinstance(payload, dict):
            compact = {
                "_compacted": True,
                "original_sha256": digest,
                "original_bytes": len(raw),
                "scalars": {str(k): v for k, v in payload.items() if isinstance(v, (str, int, float, bool)) or v is None},
                "samples": {str(k): v[:20] for k, v in payload.items() if isinstance(v, list) and v},
            }
        elif isinstance(payload, list):
            compact = {"_compacted": True, "original_sha256": digest, "original_bytes": len(raw), "item_count": len(payload), "sample": payload[:20]}
        else:
            compact = {"_compacted": True, "original_sha256": digest, "original_bytes": len(raw), "preview": str(payload)[:2000]}
        bounded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(bounded.encode("utf-8")) > self.max_payload_bytes:
            bounded = json.dumps({"_compacted": True, "original_sha256": digest, "original_bytes": len(raw)}, separators=(",", ":"))
        return bounded

    def _raw_values(self, item: RawNewsItem) -> tuple[Any, ...]:
        return (
            item.raw_key, item.source_key, item.source_name, item.source_url, item.item_url,
            item.title, item.summary, item.category, item.region, item.language, item.published_at,
            item.observed_at, item.priority, self._bounded_payload(item.payload),
        )

    async def insert_raw(self, item: RawNewsItem) -> bool:
        return (await self.insert_raw_many([item])) == 1

    async def insert_raw_many(self, items: list[RawNewsItem]) -> int:
        if not items:
            return 0
        return await asyncio.to_thread(self._insert_raw_many_sync, items)

    def _insert_raw_many_sync(self, items: list[RawNewsItem]) -> int:
        inserted = 0
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                # Enforce one durable admission limit for every collector.  The
                # collector-side checks are advisory and cannot prevent several
                # independent services from filling the shared queue at once.
                # BEGIN IMMEDIATE serializes this count with all other writers,
                # so pending + leased work cannot exceed the configured cap.
                queued = int(
                    con.execute(
                        "SELECT COUNT(*) c FROM raw_news WHERE state IN ('pending','leased')"
                    ).fetchone()["c"]
                )
                available = max(0, self.settings.news_raw_backpressure_rows - queued)
                if available <= 0:
                    con.execute("COMMIT")
                    return 0
                now = time.time()
                source_names = sorted({str(item.source_name) for item in items})
                admitted: dict[str, int] = {}
                for source_name in source_names:
                    admitted[source_name] = int(con.execute(
                        "SELECT COUNT(*) c FROM raw_news WHERE source_name=? AND ingested_at>=?",
                        (source_name, now - self.source_window_seconds),
                    ).fetchone()["c"])
                for item in items:
                    if inserted >= available:
                        break
                    source_name = str(item.source_name)
                    if admitted.get(source_name, 0) >= self.max_rows_per_source_window:
                        continue
                    cur = con.execute(
                        """INSERT OR IGNORE INTO raw_news(
                            run_id,raw_key,source_key,source_name,source_url,item_url,title,summary,category,region,language,
                            published_at,observed_at,priority,payload_json,ingested_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (self.run_id, *self._raw_values(item), now),
                    )
                    was_inserted = int(cur.rowcount > 0)
                    inserted += was_inserted
                    admitted[source_name] = admitted.get(source_name, 0) + was_inserted
                con.execute("COMMIT")
                return inserted
            except BaseException:
                con.execute("ROLLBACK")
                raise

    async def lease_raw(self, owner: str, limit: int, lease_seconds: float) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._lease_raw_sync, owner, limit, lease_seconds)

    def _lease_raw_sync(self, owner: str, limit: int, lease_seconds: float) -> list[dict[str, Any]]:
        now = time.time()
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                ids = [
                    int(r["id"])
                    for r in con.execute(
                        """SELECT id FROM raw_news
                           WHERE (state='pending' AND next_attempt<=?)
                              OR (state='leased' AND lease_until IS NOT NULL AND lease_until<?)
                           ORDER BY priority DESC,id DESC LIMIT ?""",
                        (now, now, limit),
                    ).fetchall()
                ]
                if not ids:
                    con.execute("COMMIT")
                    return []
                q = ",".join("?" for _ in ids)
                con.execute(
                    f"UPDATE raw_news SET state='leased',lease_owner=?,lease_until=?,attempts=attempts+1 WHERE id IN ({q})",
                    (owner, now + lease_seconds, *ids),
                )
                rows = con.execute(f"SELECT * FROM raw_news WHERE id IN ({q}) ORDER BY priority DESC,id DESC", ids).fetchall()
                con.execute("COMMIT")
                return [dict(r) for r in rows]
            except BaseException:
                con.execute("ROLLBACK")
                raise

    async def ack_raw(self, raw_id: int) -> None:
        await asyncio.to_thread(self._ack_raw_sync, raw_id)

    def _ack_raw_sync(self, raw_id: int) -> None:
        with connect(self.path, self.settings) as con:
            con.execute(
                """UPDATE raw_news SET state='done',lease_owner=NULL,lease_until=NULL,
                   processed_at=?,last_error=NULL,payload_json='{"_released_after_processing":true}' WHERE id=?""",
                (time.time(), raw_id),
            )

    async def retry_raw(self, raw_id: int, error: str, delay_seconds: float) -> None:
        await asyncio.to_thread(self._retry_raw_sync, raw_id, error[:2000], delay_seconds)

    def _retry_raw_sync(self, raw_id: int, error: str, delay_seconds: float) -> None:
        with connect(self.path, self.settings) as con:
            con.execute(
                "UPDATE raw_news SET state='pending',lease_owner=NULL,lease_until=NULL,next_attempt=?,last_error=? WHERE id=?",
                (time.time() + max(0.0, delay_seconds), error, raw_id),
            )

    async def source_result(
        self,
        source_key: str,
        name: str,
        url: str,
        *,
        ok: bool,
        status: str,
        http_status: int | None,
        latency_ms: float,
        parsed: int = 0,
        new_items: int = 0,
        rate_limited: bool = False,
        timeout: bool = False,
        error: str = "",
        next_due: float = 0.0,
        adaptive_poll_seconds: float | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._source_result_sync,
            source_key, name, url, ok, status, http_status, latency_ms, parsed, new_items,
            rate_limited, timeout, error, next_due, adaptive_poll_seconds,
        )

    def _source_result_sync(
        self, source_key: str, name: str, url: str, ok: bool, status: str, http_status: int | None,
        latency_ms: float, parsed: int, new_items: int, rate_limited: bool, timeout: bool, error: str,
        next_due: float, adaptive_poll_seconds: float | None,
    ) -> None:
        now = time.time()
        with connect(self.path, self.settings) as con:
            con.execute(
                """INSERT INTO source_health(
                    source_key,name,url,successes,failures,rate_limits,timeouts,new_items,parsed_items,consecutive_failures,
                    last_status,last_http_status,last_latency_ms,last_seen_at,last_success_at,last_new_at,next_due,
                    adaptive_poll_seconds,last_error
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_key) DO UPDATE SET
                    name=excluded.name,url=excluded.url,
                    successes=source_health.successes+excluded.successes,
                    failures=source_health.failures+excluded.failures,
                    rate_limits=source_health.rate_limits+excluded.rate_limits,
                    timeouts=source_health.timeouts+excluded.timeouts,
                    new_items=source_health.new_items+excluded.new_items,
                    parsed_items=source_health.parsed_items+excluded.parsed_items,
                    consecutive_failures=CASE WHEN excluded.successes>0 THEN 0 ELSE source_health.consecutive_failures+1 END,
                    last_status=excluded.last_status,last_http_status=excluded.last_http_status,
                    last_latency_ms=excluded.last_latency_ms,last_seen_at=excluded.last_seen_at,
                    last_success_at=CASE WHEN excluded.successes>0 THEN excluded.last_seen_at ELSE source_health.last_success_at END,
                    last_new_at=CASE WHEN excluded.new_items>0 THEN excluded.last_seen_at ELSE source_health.last_new_at END,
                    next_due=excluded.next_due,adaptive_poll_seconds=excluded.adaptive_poll_seconds,last_error=excluded.last_error""",
                (
                    source_key, name, url, int(ok), int(not ok), int(rate_limited), int(timeout), new_items, parsed,
                    0 if ok else 1, status, http_status, latency_ms, now, now if ok else None,
                    now if new_items else None, next_due, adaptive_poll_seconds, error[:2000],
                ),
            )

    async def add_discovered_feed(self, url: str, source_page: str, status: str = "candidate") -> bool:
        return await asyncio.to_thread(self._add_discovered_feed_sync, url, source_page, status)

    def _add_discovered_feed_sync(self, url: str, source_page: str, status: str) -> bool:
        with connect(self.path, self.settings) as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO discovered_feeds(url,source_page,discovered_at,status) VALUES(?,?,?,?)",
                (url, source_page, time.time(), status),
            )
            return cur.rowcount > 0

    async def discovered_feeds(self, limit: int = 5000) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._discovered_feeds_sync, limit)

    def _discovered_feeds_sync(self, limit: int) -> list[dict[str, Any]]:
        with connect(self.path, self.settings) as con:
            return [dict(r) for r in con.execute("SELECT * FROM discovered_feeds ORDER BY discovered_at DESC LIMIT ?", (limit,)).fetchall()]
    async def get_cursor(self, source_key: str) -> str:
        return await asyncio.to_thread(self._get_cursor_sync, source_key)

    def _get_cursor_sync(self, source_key: str) -> str:
        with connect(self.path, self.settings) as con:
            row = con.execute("SELECT cursor FROM source_cursors WHERE source_key=?", (source_key,)).fetchone()
            return str(row["cursor"]) if row else ""

    async def set_cursor(self, source_key: str, cursor: str) -> None:
        await asyncio.to_thread(self._set_cursor_sync, source_key, str(cursor))

    def _set_cursor_sync(self, source_key: str, cursor: str) -> None:
        with connect(self.path, self.settings) as con:
            con.execute(
                "INSERT INTO source_cursors(source_key,cursor,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(source_key) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                (source_key, cursor, time.time()),
            )
