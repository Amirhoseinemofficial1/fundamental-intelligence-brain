from __future__ import annotations

import json
import hashlib
import os
import time
from typing import Any, Iterable

from ..config import Settings
from ..models import MarketEvent
from .sqlite import connect, serialized_initialization, transaction


class MarketSemanticStore:
    """Per-market-process semantic SQLite store.

    Connections are intentionally opened inside each blocking call. The semantic writer invokes
    these methods through ``asyncio.to_thread``; keeping a connection created on the event-loop
    thread would violate sqlite3's thread-affinity contract. Each exchange has a separate DB, so
    connection-per-batch does not introduce cross-exchange writer contention.
    """

    def __init__(self, settings: Settings, service: str):
        self.settings = settings
        self.service = service
        self.path = settings.data_dir / "market" / service / "semantic.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_sync()

    @serialized_initialization
    def _init_sync(self) -> None:
        with connect(self.path, self.settings) as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_ts TEXT,
                    observed_at TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    time_bucket INTEGER NOT NULL,
                    UNIQUE(venue,symbol,event_type,time_bucket)
                );
                CREATE INDEX IF NOT EXISTS idx_market_events_time ON events(observed_at);
                CREATE INDEX IF NOT EXISTS idx_market_events_kind ON events(venue,symbol,event_type,id);
                CREATE TABLE IF NOT EXISTS sequence_state(
                    stream_key TEXT PRIMARY KEY,
                    last_sequence INTEGER,
                    gap_count INTEGER NOT NULL DEFAULT 0,
                    recovered_count INTEGER NOT NULL DEFAULT 0,
                    reconnect_count INTEGER NOT NULL DEFAULT 0,
                    recovery_pending INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                """
            )

    def insert_many(self, rows: Iterable[MarketEvent]) -> int:
        bucket_seconds=max(1,int(os.getenv('MARKET_SEMANTIC_BUCKET_SECONDS','5')))
        payloads=[]
        now=time.time()
        for x in rows:
            text=json.dumps(x.payload,ensure_ascii=False,separators=(",",":"),sort_keys=True,default=str)
            digest=hashlib.sha256(text.encode()).hexdigest()
            payloads.append((x.event_type,x.venue,x.symbol,x.exchange_ts,x.observed_at,x.lane,text,digest,int(now//bucket_seconds)))
        if not payloads:
            return 0
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                con.executemany(
                    """INSERT INTO events(event_type,venue,symbol,exchange_ts,observed_at,lane,payload_json,payload_hash,time_bucket)
                       VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(venue,symbol,event_type,time_bucket) DO UPDATE SET
                       exchange_ts=excluded.exchange_ts,observed_at=excluded.observed_at,lane=excluded.lane,
                       payload_json=excluded.payload_json,payload_hash=excluded.payload_hash""",
                    payloads,
                )
                con.execute("COMMIT")
                return len(payloads)
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def load_sequence_states(self) -> dict[str, dict[str, Any]]:
        with connect(self.path, self.settings) as con:
            rows = con.execute("SELECT stream_key,last_sequence,recovery_pending,updated_at FROM sequence_state").fetchall()
            return {
                str(r["stream_key"]): {
                    "last_sequence": int(r["last_sequence"]) if r["last_sequence"] is not None else None,
                    "recovery_pending": bool(r["recovery_pending"]),
                    "updated_at": float(r["updated_at"] or 0.0),
                }
                for r in rows
            }

    def checkpoint_sequences(self, states: dict[str, tuple[int | None, bool]]) -> None:
        if not states:
            return
        now = time.time()
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                for stream_key, (last_sequence, recovery_pending) in states.items():
                    con.execute(
                        """INSERT INTO sequence_state(stream_key,last_sequence,recovery_pending,updated_at)
                           VALUES(?,?,?,?) ON CONFLICT(stream_key) DO UPDATE SET
                           last_sequence=excluded.last_sequence,
                           recovery_pending=excluded.recovery_pending,
                           updated_at=excluded.updated_at""",
                        (stream_key, last_sequence, int(recovery_pending), now),
                    )
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def pending_recovery_count(self) -> int:
        with connect(self.path, self.settings) as con:
            row = con.execute("SELECT COUNT(*) c FROM sequence_state WHERE recovery_pending=1").fetchone()
            return int(row["c"])

    def close(self) -> None:
        with connect(self.path, self.settings) as con:
            con.execute("PRAGMA wal_checkpoint(PASSIVE)")
