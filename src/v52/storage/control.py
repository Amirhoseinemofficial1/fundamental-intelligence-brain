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
from ..intelligence.impact_router import route_story
from ..models import StoryCandidate
from .sqlite import connect, serialized_initialization, transaction


class ControlStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.run_id = os.getenv("V52_RUN_ID", "standalone")
        self.path = settings.data_dir / "db" / "control.sqlite3"
        self._init_sync()

    @serialized_initialization
    def _init_sync(self) -> None:
        with connect(self.path, self.settings) as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS stories(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL DEFAULT 'legacy',
                    fingerprint TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    category TEXT NOT NULL,
                    region TEXT NOT NULL,
                    language TEXT NOT NULL,
                    published_at TEXT,
                    first_observed_at TEXT NOT NULL,
                    last_observed_at TEXT NOT NULL,
                    materiality INTEGER NOT NULL,
                    reasons_json TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    original_title TEXT NOT NULL,
                    original_summary TEXT NOT NULL,
                    english_title TEXT,
                    english_summary TEXT,
                    article_text TEXT,
                    article_enriched_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_stories_created ON stories(created_at);
                CREATE INDEX IF NOT EXISTS idx_stories_materiality ON stories(materiality,created_at);
                CREATE TABLE IF NOT EXISTS occurrences(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
                    raw_id INTEGER NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    item_url TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    UNIQUE(story_id,raw_id)
                );
                CREATE TABLE IF NOT EXISTS translation_jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    story_id INTEGER NOT NULL UNIQUE REFERENCES stories(id) ON DELETE CASCADE,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_until REAL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_translation_pending ON translation_jobs(state,next_attempt,id);
                CREATE TABLE IF NOT EXISTS translation_cache(
                    text_hash TEXT PRIMARY KEY,
                    source_text TEXT NOT NULL,
                    english_text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_key TEXT NOT NULL UNIQUE,
                    story_id INTEGER REFERENCES stories(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_until REAL,
                    send_attempt_id TEXT,
                    sent_at REAL,
                    telegram_message_id TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_alert_pending ON alerts(state,next_attempt,priority DESC,id);
                CREATE TABLE IF NOT EXISTS enrichment_jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    story_id INTEGER NOT NULL UNIQUE REFERENCES stories(id) ON DELETE CASCADE,
                    url TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_until REAL,
                    last_error TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_enrich_pending ON enrichment_jobs(state,next_attempt,priority DESC,id);
                CREATE TABLE IF NOT EXISTS story_revisions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
                    revision_no INTEGER NOT NULL,
                    revision_type TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(story_id,revision_no),
                    UNIQUE(story_id,content_hash)
                );
                CREATE TABLE IF NOT EXISTS story_change_feed(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
                    revision_no INTEGER NOT NULL,
                    change_type TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(story_id,revision_no)
                );
                CREATE INDEX IF NOT EXISTS idx_story_change_feed ON story_change_feed(id,story_id);
                CREATE TABLE IF NOT EXISTS story_impact_routes(
                    change_id INTEGER PRIMARY KEY REFERENCES story_change_feed(id) ON DELETE CASCADE,
                    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
                    score INTEGER NOT NULL,
                    priority TEXT NOT NULL,
                    components_json TEXT NOT NULL,
                    routed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_story_impact_priority
                    ON story_impact_routes(priority,score DESC,change_id);
                CREATE TABLE IF NOT EXISTS story_fastlane_receipts(
                    change_id INTEGER PRIMARY KEY REFERENCES story_change_feed(id) ON DELETE CASCADE,
                    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
                    priority TEXT NOT NULL,
                    impact_score INTEGER NOT NULL,
                    event_id INTEGER NOT NULL,
                    processed_at REAL NOT NULL,
                    refined_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_story_fastlane_processed
                    ON story_fastlane_receipts(processed_at,change_id);
                CREATE TABLE IF NOT EXISTS story_fastlane_state(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            columns = {str(r["name"]) for r in con.execute("PRAGMA table_info(stories)").fetchall()}
            if "run_id" not in columns:
                con.execute("ALTER TABLE stories ADD COLUMN run_id TEXT NOT NULL DEFAULT 'legacy'")
            con.execute("CREATE INDEX IF NOT EXISTS idx_stories_run ON stories(run_id,id)")
            # Upgrade path: existing V51 stories become initial revisions exactly once.
            rows = con.execute("SELECT id,title,summary,first_observed_at,created_at FROM stories WHERE NOT EXISTS(SELECT 1 FROM story_revisions r WHERE r.story_id=stories.id)").fetchall()
            for row in rows:
                content_hash = hashlib.sha256(f"{row['title']}\n{row['summary']}".encode("utf-8", errors="ignore")).hexdigest()
                con.execute("INSERT OR IGNORE INTO story_revisions(story_id,revision_no,revision_type,content_hash,title,summary,observed_at,created_at) VALUES(?,1,'INITIAL',?,?,?,?,?)", (row["id"], content_hash, row["title"], row["summary"], row["first_observed_at"], row["created_at"]))
                con.execute("INSERT OR IGNORE INTO story_change_feed(story_id,revision_no,change_type,created_at) VALUES(?,1,'INITIAL',?)", (row["id"], row["created_at"]))
            # Upgrade boundary: historical rows continue through the durable
            # normal cursor, but are not replayed as fresh Fast-Lane alerts.
            # On a new database this value is zero; all subsequently recorded
            # P0/P1 changes are immediately eligible.
            con.execute(
                """INSERT OR IGNORE INTO story_fastlane_state(key,value,updated_at)
                   VALUES('activation_change_id',
                          (SELECT CAST(COALESCE(MAX(id),0) AS TEXT) FROM story_change_feed),?)""",
                (time.time(),),
            )
            unrouted = con.execute(
                """SELECT s.*,f.id AS _change_id,r.title AS _revision_title,
                          r.summary AS _revision_summary,r.observed_at AS _revision_observed_at
                   FROM story_change_feed f JOIN stories s ON s.id=f.story_id
                   JOIN story_revisions r ON r.story_id=f.story_id AND r.revision_no=f.revision_no
                   LEFT JOIN story_impact_routes ir ON ir.change_id=f.id
                   WHERE ir.change_id IS NULL ORDER BY f.id"""
            ).fetchall()
            for row in unrouted:
                item = dict(row)
                item["title"] = item.pop("_revision_title")
                item["summary"] = item.pop("_revision_summary")
                item["first_observed_at"] = item.pop("_revision_observed_at")
                self._route_change_con(con, int(item["_change_id"]), item)

    @staticmethod
    def _route_change_con(con: sqlite3.Connection, change_id: int, story: dict[str, Any]) -> None:
        route = route_story(story)
        con.execute(
            """INSERT OR REPLACE INTO story_impact_routes(
                   change_id,story_id,score,priority,components_json,routed_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                change_id, int(story["id"]), route.score, route.priority,
                json.dumps(route.as_dict(), sort_keys=True, separators=(",", ":")), time.time(),
            ),
        )

    async def record_story(
        self,
        candidate: StoryCandidate,
        item_url: str,
        translation_needed: bool,
        alert_payload: dict[str, Any] | None,
        alert_priority: int,
        alert_expires_at: float | None,
        enrichment: bool,
    ) -> tuple[int, bool]:
        return await asyncio.to_thread(
            self._record_story_sync,
            candidate, item_url, translation_needed, alert_payload, alert_priority, alert_expires_at, enrichment,
        )

    def _record_story_sync(
        self, candidate: StoryCandidate, item_url: str, translation_needed: bool,
        alert_payload: dict[str, Any] | None, alert_priority: int, alert_expires_at: float | None,
        enrichment: bool,
    ) -> tuple[int, bool]:
        now = time.time()
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                change_id: int | None = None
                row = con.execute("SELECT id,title,summary FROM stories WHERE fingerprint=?", (candidate.fingerprint,)).fetchone()
                is_new = row is None
                if is_new:
                    cur = con.execute(
                        """INSERT INTO stories(
                            run_id,fingerprint,title,summary,canonical_url,category,region,language,published_at,
                            first_observed_at,last_observed_at,materiality,reasons_json,source_name,source_url,
                            original_title,original_summary,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            self.run_id, candidate.fingerprint, candidate.title, candidate.summary, candidate.canonical_url,
                            candidate.category, candidate.region, candidate.language, candidate.published_at,
                            candidate.observed_at, candidate.observed_at, candidate.materiality,
                            json.dumps(candidate.reasons, ensure_ascii=False), candidate.source_name,
                            candidate.source_url, candidate.title, candidate.summary, now, now,
                        ),
                    )
                    story_id = int(cur.lastrowid)
                    content_hash = hashlib.sha256(f"{candidate.title}\n{candidate.summary}".encode("utf-8", errors="ignore")).hexdigest()
                    con.execute(
                        "INSERT INTO story_revisions(story_id,revision_no,revision_type,content_hash,title,summary,observed_at,created_at) VALUES(?,1,'INITIAL',?,?,?,?,?)",
                        (story_id, content_hash, candidate.title, candidate.summary, candidate.observed_at, now),
                    )
                    feed = con.execute("INSERT INTO story_change_feed(story_id,revision_no,change_type,created_at) VALUES(?,1,'INITIAL',?)", (story_id, now))
                    change_id = int(feed.lastrowid)
                else:
                    story_id = int(row["id"])
                    content_hash = hashlib.sha256(f"{candidate.title}\n{candidate.summary}".encode("utf-8", errors="ignore")).hexdigest()
                    revision = con.execute("SELECT revision_no FROM story_revisions WHERE story_id=? AND content_hash=?", (story_id, content_hash)).fetchone()
                    if revision is None:
                        latest = con.execute("SELECT COALESCE(MAX(revision_no),0) n FROM story_revisions WHERE story_id=?", (story_id,)).fetchone()
                        revision_no = int(latest["n"]) + 1
                        lower = f"{candidate.title} {candidate.summary}".lower()
                        revision_type = "CORRECTION" if any(x in lower for x in ("correction", "corrected", "retract")) else "FOLLOW_UP" if any(x in lower for x in ("follow-up", "follow up", "confirmed")) else "UPDATE"
                        con.execute(
                            "INSERT INTO story_revisions(story_id,revision_no,revision_type,content_hash,title,summary,observed_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (story_id, revision_no, revision_type, content_hash, candidate.title, candidate.summary, candidate.observed_at, now),
                        )
                        feed = con.execute("INSERT INTO story_change_feed(story_id,revision_no,change_type,created_at) VALUES(?,?,?,?)", (story_id, revision_no, revision_type, now))
                        change_id = int(feed.lastrowid)
                    con.execute(
                        "UPDATE stories SET title=?,summary=?,last_observed_at=?,updated_at=?,materiality=MAX(materiality,?) WHERE id=?",
                        (candidate.title, candidate.summary, candidate.observed_at, now, candidate.materiality, story_id),
                    )
                con.execute(
                    "INSERT OR IGNORE INTO occurrences(story_id,raw_id,source_name,source_url,item_url,observed_at) VALUES(?,?,?,?,?,?)",
                    (story_id, candidate.raw_id, candidate.source_name, candidate.source_url, item_url, candidate.observed_at),
                )
                if is_new and translation_needed:
                    con.execute(
                        "INSERT OR IGNORE INTO translation_jobs(story_id,created_at,updated_at) VALUES(?,?,?)",
                        (story_id, now, now),
                    )
                if is_new and alert_payload is not None:
                    client_key = f"story:{candidate.fingerprint}:telegram"
                    con.execute(
                        """INSERT OR IGNORE INTO alerts(client_key,story_id,kind,priority,payload_json,created_at,expires_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (client_key, story_id, "story", alert_priority,
                         json.dumps(alert_payload, ensure_ascii=False, separators=(",", ":")), now, alert_expires_at),
                    )
                if is_new and enrichment and candidate.canonical_url.startswith(("http://", "https://")):
                    con.execute(
                        "INSERT OR IGNORE INTO enrichment_jobs(story_id,url,priority,created_at) VALUES(?,?,?,?)",
                        (story_id, candidate.canonical_url, candidate.materiality, now),
                    )
                if change_id is not None:
                    self._route_change_con(con, change_id, {
                        "id": story_id,
                        "title": candidate.title,
                        "summary": candidate.summary,
                        "category": candidate.category,
                        "materiality": candidate.materiality,
                        "source_name": candidate.source_name,
                        "source_url": candidate.source_url,
                        "canonical_url": candidate.canonical_url,
                    })
                con.execute("COMMIT")
                return story_id, is_new
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def _lease_table_sync(self, table: str, owner: str, limit: int, lease_seconds: float) -> list[dict[str, Any]]:
        if table not in {"translation_jobs", "alerts", "enrichment_jobs"}:
            raise ValueError(table)
        now = time.time()
        order = "priority DESC,id" if table in {"alerts", "enrichment_jobs"} else "id"
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                ids = [
                    int(r["id"])
                    for r in con.execute(
                        f"""SELECT id FROM {table}
                             WHERE (state='pending' AND next_attempt<=?)
                                OR (state='leased' AND lease_until IS NOT NULL AND lease_until<?)
                             ORDER BY {order} LIMIT ?""",
                        (now, now, limit),
                    ).fetchall()
                ]
                if not ids:
                    con.execute("COMMIT")
                    return []
                q = ",".join("?" for _ in ids)
                con.execute(
                    f"UPDATE {table} SET state='leased',lease_owner=?,lease_until=?,attempts=attempts+1 WHERE id IN ({q})",
                    (owner, now + lease_seconds, *ids),
                )
                rows = con.execute(f"SELECT * FROM {table} WHERE id IN ({q}) ORDER BY {order}", ids).fetchall()
                con.execute("COMMIT")
                return [dict(r) for r in rows]
            except BaseException:
                con.execute("ROLLBACK")
                raise

    async def lease_translations(self, owner: str, limit: int, lease_seconds: float) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._lease_table_sync, "translation_jobs", owner, limit, lease_seconds)

    async def lease_alerts(self, owner: str, limit: int, lease_seconds: float) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._lease_table_sync, "alerts", owner, limit, lease_seconds)

    async def lease_enrichment(self, owner: str, limit: int, lease_seconds: float) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._lease_table_sync, "enrichment_jobs", owner, limit, lease_seconds)

    async def get_story(self, story_id: int) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_story_sync, story_id)

    def _get_story_sync(self, story_id: int) -> dict[str, Any] | None:
        with connect(self.path, self.settings) as con:
            row = con.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
            return dict(row) if row else None

    async def stories_after(self, story_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._stories_after_sync, story_id, limit)

    def _stories_after_sync(self, story_id: int, limit: int) -> list[dict[str, Any]]:
        with connect(self.path, self.settings) as con:
            rows = con.execute("SELECT * FROM stories WHERE id>? ORDER BY id LIMIT ?", (story_id, limit)).fetchall()
            return [dict(row) for row in rows]

    async def story_changes_after(self, change_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._story_changes_after_sync, change_id, limit)

    def _story_changes_after_sync(self, change_id: int, limit: int) -> list[dict[str, Any]]:
        with connect(self.path, self.settings) as con:
            rows = con.execute(
                """SELECT s.*,f.id AS _change_id,f.revision_no AS _revision_no,f.change_type AS _change_type,
                          ir.score AS _impact_score,ir.priority AS _impact_priority,
                          ir.components_json AS _impact_json,
                          r.title AS _revision_title,r.summary AS _revision_summary,r.observed_at AS _revision_observed_at
                   FROM story_change_feed f JOIN stories s ON s.id=f.story_id
                   JOIN story_revisions r ON r.story_id=f.story_id AND r.revision_no=f.revision_no
                   LEFT JOIN story_impact_routes ir ON ir.change_id=f.id
                   WHERE f.id>? ORDER BY f.id LIMIT ?""",
                (change_id, limit),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["title"] = item.pop("_revision_title")
                item["summary"] = item.pop("_revision_summary")
                item["first_observed_at"] = item.pop("_revision_observed_at")
                try:
                    item["_impact_route"] = json.loads(item.pop("_impact_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    item["_impact_route"] = {}
                result.append(item)
            return result

    async def fastlane_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._fastlane_candidates_sync, limit)

    def _fastlane_candidates_sync(self, limit: int) -> list[dict[str, Any]]:
        with connect(self.path, self.settings) as con:
            rows = con.execute(
                """SELECT s.*,f.id AS _change_id,f.revision_no AS _revision_no,
                          f.change_type AS _change_type,ir.score AS _impact_score,
                          ir.priority AS _impact_priority,ir.components_json AS _impact_json,
                          r.title AS _revision_title,r.summary AS _revision_summary,
                          r.observed_at AS _revision_observed_at
                   FROM story_impact_routes ir
                   JOIN story_change_feed f ON f.id=ir.change_id
                   JOIN stories s ON s.id=f.story_id
                   JOIN story_revisions r ON r.story_id=f.story_id AND r.revision_no=f.revision_no
                   LEFT JOIN story_fastlane_receipts fr ON fr.change_id=f.id
                   WHERE ir.priority IN ('P0','P1') AND fr.change_id IS NULL
                     AND f.id>COALESCE((
                         SELECT CAST(value AS INTEGER) FROM story_fastlane_state
                         WHERE key='activation_change_id'
                     ),0)
                   ORDER BY CASE ir.priority WHEN 'P0' THEN 0 ELSE 1 END,
                            ir.score DESC,f.id ASC LIMIT ?""",
                (max(1, min(100, int(limit))),),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["title"] = item.pop("_revision_title")
                item["summary"] = item.pop("_revision_summary")
                item["first_observed_at"] = item.pop("_revision_observed_at")
                try:
                    item["_impact_route"] = json.loads(item.pop("_impact_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    item["_impact_route"] = {}
                result.append(item)
            return result

    async def record_fastlane_receipt(
        self, change_id: int, story_id: int, priority: str, score: int, event_id: int,
    ) -> None:
        await asyncio.to_thread(
            self._record_fastlane_receipt_sync,
            change_id, story_id, priority, score, event_id,
        )

    def _record_fastlane_receipt_sync(
        self, change_id: int, story_id: int, priority: str, score: int, event_id: int,
    ) -> None:
        with connect(self.path, self.settings) as con:
            con.execute(
                """INSERT OR REPLACE INTO story_fastlane_receipts(
                       change_id,story_id,priority,impact_score,event_id,processed_at,refined_at
                   ) VALUES(?,?,?,?,?,?,COALESCE(
                       (SELECT refined_at FROM story_fastlane_receipts WHERE change_id=?),NULL
                   ))""",
                (change_id, story_id, priority, score, event_id, time.time(), change_id),
            )

    async def fastlane_receipt(self, change_id: int) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._fastlane_receipt_sync, change_id)

    def _fastlane_receipt_sync(self, change_id: int) -> dict[str, Any] | None:
        with connect(self.path, self.settings) as con:
            row = con.execute(
                "SELECT * FROM story_fastlane_receipts WHERE change_id=?", (change_id,),
            ).fetchone()
            return dict(row) if row else None

    async def mark_fastlane_refined(self, change_id: int) -> None:
        await asyncio.to_thread(self._mark_fastlane_refined_sync, change_id)

    def _mark_fastlane_refined_sync(self, change_id: int) -> None:
        with connect(self.path, self.settings) as con:
            con.execute(
                "UPDATE story_fastlane_receipts SET refined_at=? WHERE change_id=?",
                (time.time(), change_id),
            )

    async def fastlane_summary(self) -> dict[str, int]:
        return await asyncio.to_thread(self._fastlane_summary_sync)

    def _fastlane_summary_sync(self) -> dict[str, int]:
        with connect(self.path, self.settings) as con:
            rows = con.execute(
                "SELECT priority,COUNT(*) AS count FROM story_fastlane_receipts GROUP BY priority"
            ).fetchall()
            return {str(row["priority"]): int(row["count"]) for row in rows}

    async def translation_cache_get(self, text_hash: str) -> str | None:
        return await asyncio.to_thread(self._translation_cache_get_sync, text_hash)

    def _translation_cache_get_sync(self, text_hash: str) -> str | None:
        with connect(self.path, self.settings) as con:
            row = con.execute("SELECT english_text FROM translation_cache WHERE text_hash=?", (text_hash,)).fetchone()
            if not row:
                return None
            con.execute("UPDATE translation_cache SET last_used_at=? WHERE text_hash=?", (time.time(), text_hash))
            return str(row["english_text"])

    async def complete_translation(self, job_id: int, story_id: int, title: str, summary: str, cache_rows: list[tuple[str, str, str]]) -> None:
        await asyncio.to_thread(self._complete_translation_sync, job_id, story_id, title, summary, cache_rows)

    def _complete_translation_sync(self, job_id: int, story_id: int, title: str, summary: str, cache_rows: list[tuple[str, str, str]]) -> None:
        now = time.time()
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                con.execute("UPDATE stories SET english_title=?,english_summary=?,updated_at=? WHERE id=?", (title, summary, now, story_id))
                for h, src, eng in cache_rows:
                    con.execute(
                        """INSERT INTO translation_cache(text_hash,source_text,english_text,created_at,last_used_at)
                           VALUES(?,?,?,?,?) ON CONFLICT(text_hash) DO UPDATE SET english_text=excluded.english_text,last_used_at=excluded.last_used_at""",
                        (h, src, eng, now, now),
                    )
                con.execute("UPDATE translation_jobs SET state='done',lease_owner=NULL,lease_until=NULL,last_error=NULL,updated_at=? WHERE id=?", (now, job_id))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    async def retry_job(self, table: str, job_id: int, error: str, delay_seconds: float, dead: bool = False) -> None:
        await asyncio.to_thread(self._retry_job_sync, table, job_id, error[:2000], delay_seconds, dead)

    def _retry_job_sync(self, table: str, job_id: int, error: str, delay_seconds: float, dead: bool) -> None:
        if table not in {"translation_jobs", "alerts", "enrichment_jobs"}:
            raise ValueError(table)
        state = "dead" if dead else "pending"
        with connect(self.path, self.settings) as con:
            updated = ",updated_at=?" if table == "translation_jobs" else ""
            args: list[Any] = [state, time.time() + max(0.0, delay_seconds), error]
            if table == "translation_jobs":
                args.append(time.time())
            args.append(job_id)
            con.execute(
                f"UPDATE {table} SET state=?,lease_owner=NULL,lease_until=NULL,next_attempt=?,last_error=?{updated} WHERE id=?",
                args,
            )

    async def mark_alert_sending(self, alert_ids: list[int], attempt_id: str, lease_seconds: float) -> None:
        await asyncio.to_thread(self._mark_alert_sending_sync, alert_ids, attempt_id, lease_seconds)

    def _mark_alert_sending_sync(self, alert_ids: list[int], attempt_id: str, lease_seconds: float) -> None:
        if not alert_ids:
            return
        q = ",".join("?" for _ in alert_ids)
        with connect(self.path, self.settings) as con:
            con.execute(
                f"UPDATE alerts SET state='leased',send_attempt_id=?,lease_until=? WHERE id IN ({q})",
                (attempt_id, time.time() + lease_seconds, *alert_ids),
            )

    async def complete_alerts(self, alert_ids: list[int], telegram_message_id: str | None) -> None:
        await asyncio.to_thread(self._complete_alerts_sync, alert_ids, telegram_message_id)

    def _complete_alerts_sync(self, alert_ids: list[int], telegram_message_id: str | None) -> None:
        if not alert_ids:
            return
        q = ",".join("?" for _ in alert_ids)
        with connect(self.path, self.settings) as con:
            con.execute(
                f"UPDATE alerts SET state='sent',sent_at=?,telegram_message_id=?,lease_owner=NULL,lease_until=NULL,last_error=NULL WHERE id IN ({q})",
                (time.time(), telegram_message_id, *alert_ids),
            )

    async def expire_alerts(self) -> int:
        return await asyncio.to_thread(self._expire_alerts_sync)

    def _expire_alerts_sync(self) -> int:
        now = time.time()
        with connect(self.path, self.settings) as con:
            cur = con.execute(
                "UPDATE alerts SET state='expired',lease_owner=NULL,lease_until=NULL WHERE state IN ('pending','leased') AND expires_at IS NOT NULL AND expires_at<?",
                (now,),
            )
            return int(cur.rowcount)

    async def complete_enrichment(self, job_id: int, story_id: int, article_text: str) -> None:
        await asyncio.to_thread(self._complete_enrichment_sync, job_id, story_id, article_text)

    def _complete_enrichment_sync(self, job_id: int, story_id: int, article_text: str) -> None:
        now = time.time()
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                con.execute("UPDATE stories SET article_text=?,article_enriched_at=?,updated_at=? WHERE id=?", (article_text, now, now, story_id))
                con.execute("UPDATE enrichment_jobs SET state='done',lease_owner=NULL,lease_until=NULL,last_error=NULL WHERE id=?", (job_id,))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise


    async def enqueue_alert(self, client_key: str, kind: str, priority: int, payload: dict[str, Any], expires_at: float | None = None) -> bool:
        return await asyncio.to_thread(self._enqueue_alert_sync, client_key, kind, priority, payload, expires_at)

    def _enqueue_alert_sync(self, client_key: str, kind: str, priority: int, payload: dict[str, Any], expires_at: float | None) -> bool:
        with connect(self.path, self.settings) as con:
            transaction(con)
            cur = con.execute(
                "INSERT OR IGNORE INTO alerts(client_key,story_id,kind,priority,payload_json,created_at,expires_at) VALUES(?,NULL,?,?,?,?,?)",
                (client_key, kind, priority, json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), time.time(), expires_at),
            )
            inserted=cur.rowcount>0
            return inserted

    async def queue_counts(self) -> dict[str, int]:
        return await asyncio.to_thread(self._queue_counts_sync)

    def _queue_counts_sync(self) -> dict[str, int]:
        with connect(self.path, self.settings) as con:
            out: dict[str, int] = {}
            states = {
                "translation_jobs": ("pending", "leased", "done", "dead"),
                "alerts": ("pending", "leased", "sent", "dead", "expired"),
                "enrichment_jobs": ("pending", "leased", "done", "dead"),
            }
            for table, table_states in states.items():
                for state in table_states:
                    row = con.execute(f"SELECT COUNT(*) c FROM {table} WHERE state=?", (state,)).fetchone()
                    out[f"{table}_{state}"] = int(row["c"])
            return out
