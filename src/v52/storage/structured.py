from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Any

from ..config import Settings
from .sqlite import connect, serialized_initialization, transaction


class StructuredStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.data_dir / "db" / "structured.sqlite3"
        self.state_bucket_seconds = max(60, int(os.getenv("STRUCTURED_STATE_BUCKET_SECONDS", "300")))
        self.max_payload_bytes = max(16_384, int(os.getenv("STRUCTURED_MAX_PAYLOAD_BYTES", "262144")))
        self._init_sync()

    @serialized_initialization
    def _init_sync(self) -> None:
        with connect(self.path, self.settings) as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    record_type TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    time_bucket INTEGER NOT NULL,
                    UNIQUE(provider,record_type,entity_key,time_bucket)
                );
                CREATE INDEX IF NOT EXISTS idx_structured_time ON snapshots(observed_at);
                CREATE INDEX IF NOT EXISTS idx_structured_entity ON snapshots(provider,record_type,entity_key,observed_at);
                CREATE TABLE IF NOT EXISTS provider_status(
                    provider TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    last_status INTEGER,
                    last_success_at REAL,
                    last_attempt_at REAL,
                    failures INTEGER NOT NULL DEFAULT 0,
                    rate_limits INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                """
            )

    def _encoded(self, payload: Any) -> tuple[str, str]:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        raw = text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) <= self.max_payload_bytes:
            return digest, text
        # Retain identity/provenance and a bounded representative sample.  The
        # full response hash proves whether the upstream object changed without
        # copying multi-megabyte arrays into SQLite every poll.
        if isinstance(payload, dict):
            scalars = {str(k): v for k, v in payload.items() if isinstance(v, (str, int, float, bool)) or v is None}
            samples = {str(k): v[:32] for k, v in payload.items() if isinstance(v, list) and v}
            compact: Any = {"_compacted": True, "original_sha256": digest, "original_bytes": len(raw), "scalars": scalars, "samples": samples}
        elif isinstance(payload, list):
            compact = {"_compacted": True, "original_sha256": digest, "original_bytes": len(raw), "item_count": len(payload), "sample": payload[:32]}
        else:
            compact = {"_compacted": True, "original_sha256": digest, "original_bytes": len(raw), "preview": str(payload)[:4000]}
        bounded = json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        if len(bounded.encode("utf-8")) > self.max_payload_bytes:
            bounded = json.dumps({"_compacted": True, "original_sha256": digest, "original_bytes": len(raw)}, separators=(",", ":"))
        return digest, bounded

    @staticmethod
    def _identity(payload: Any, digest: str) -> str:
        if not isinstance(payload, dict):
            return digest
        keys = (
            "id", "entry_id", "accessionNumber", "accession_number", "filing_id",
            "ticker", "symbol", "asset_id", "market_id", "conditionId", "slug",
            "url", "file_url", "date", "report_date", "title",
        )
        parts = [f"{key}={payload[key]}" for key in keys if payload.get(key) not in (None, "")]
        if parts:
            return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
        if any(isinstance(value, (list, dict)) for value in payload.values()):
            return "aggregate"
        return digest

    def _time_bucket(self, record_type: str, observed_at: float, digest: str, entity_key: str) -> int:
        immutable = ("FILING", "RECORD", "AWARD", "ANNOUNCEMENT", "LISTING", "UNLOCK", "DOCUMENT")
        if any(token in record_type.upper() for token in immutable):
            # Immutable records deduplicate forever.  A fallback content hash is
            # already the entity key, while identified entities retain revisions
            # only when the upstream identity itself changes.
            return 0
        return int(observed_at // self.state_bucket_seconds)

    async def insert_snapshot(self, provider: str, lane: str, record_type: str, payload: Any) -> bool:
        return (await self.insert_snapshots(provider, lane, record_type, [payload])) == 1

    async def insert_snapshots(self, provider: str, lane: str, record_type: str, payloads: list[Any]) -> int:
        if not payloads:
            return 0
        return await asyncio.to_thread(self._insert_snapshots_sync, provider, lane, record_type, payloads)

    def _insert_snapshots_sync(self, provider: str, lane: str, record_type: str, payloads: list[Any]) -> int:
        now = time.time()
        rows = []
        for payload in payloads:
            h, text = self._encoded(payload)
            entity = self._identity(payload, h)
            bucket = self._time_bucket(record_type, now, h, entity)
            rows.append((provider, lane, record_type, now, h, text, entity, bucket))
        inserted = 0
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                immutable = all(int(row[7]) == 0 for row in rows)
                latest: dict[str, str] = {}
                if not immutable:
                    # State feeds often return the complete unchanged universe on
                    # every poll.  Keep a new row only when that entity's content
                    # actually changes.  Looking at the latest row (instead of a
                    # global hash) still preserves A -> B -> A transitions.
                    latest = {
                        str(row["entity_key"]): str(row["payload_hash"])
                        for row in con.execute(
                            """SELECT current.entity_key,current.payload_hash
                               FROM snapshots AS current
                               JOIN (
                                   SELECT entity_key,MAX(id) AS id
                                   FROM snapshots
                                   WHERE provider=? AND record_type=?
                                   GROUP BY entity_key
                               ) AS newest ON newest.id=current.id""",
                            (provider, record_type),
                        ).fetchall()
                    }
                persisted_keys: set[tuple[str, int]] = set()
                for row in rows:
                    entity_key, payload_hash, bucket = str(row[6]), str(row[4]), int(row[7])
                    if bucket != 0 and latest.get(entity_key) == payload_hash:
                        continue
                    cur = con.execute(
                        """INSERT INTO snapshots(provider,lane,record_type,observed_at,payload_hash,payload_json,entity_key,time_bucket)
                           VALUES(?,?,?,?,?,?,?,?)
                           ON CONFLICT(provider,record_type,entity_key,time_bucket) DO UPDATE SET
                           lane=excluded.lane,observed_at=excluded.observed_at,payload_hash=excluded.payload_hash,
                           payload_json=excluded.payload_json
                           WHERE snapshots.payload_hash<>excluded.payload_hash""",
                        row,
                    )
                    persisted_key = (entity_key, bucket)
                    inserted += int(cur.rowcount > 0 and persisted_key not in persisted_keys)
                    if cur.rowcount > 0:
                        persisted_keys.add(persisted_key)
                    if bucket != 0 and cur.rowcount > 0:
                        latest[entity_key] = payload_hash
                con.execute("COMMIT")
                return inserted
            except BaseException:
                con.execute("ROLLBACK")
                raise

    async def status(self, provider: str, state: str, http_status: int | None = None, error: str = "", rate_limited: bool = False) -> None:
        await asyncio.to_thread(self._status_sync, provider, state, http_status, error, rate_limited)

    def _status_sync(self, provider: str, state: str, http_status: int | None, error: str, rate_limited: bool) -> None:
        now = time.time()
        ok = state == "ok"
        with connect(self.path, self.settings) as con:
            con.execute(
                """INSERT INTO provider_status(provider,state,last_status,last_success_at,last_attempt_at,failures,rate_limits,last_error)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider) DO UPDATE SET state=excluded.state,last_status=excluded.last_status,
                   last_success_at=CASE WHEN excluded.state='ok' THEN excluded.last_attempt_at ELSE provider_status.last_success_at END,
                   last_attempt_at=excluded.last_attempt_at,failures=provider_status.failures+excluded.failures,
                   rate_limits=provider_status.rate_limits+excluded.rate_limits,last_error=excluded.last_error""",
                (provider, state, http_status, now if ok else None, now, int(not ok), int(rate_limited), error[:2000]),
            )

    async def snapshots_after(self, cursor: int, limit: int = 500) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._snapshots_after_sync, cursor, limit)

    def _snapshots_after_sync(self, cursor: int, limit: int) -> list[dict[str, Any]]:
        with connect(self.path, self.settings) as con:
            rows = con.execute("SELECT * FROM snapshots WHERE id>? ORDER BY id LIMIT ?", (cursor, limit)).fetchall()
            return [dict(row) for row in rows]
