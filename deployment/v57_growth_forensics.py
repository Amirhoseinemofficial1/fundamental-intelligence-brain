#!/usr/bin/env python3
"""Read-only attribution of V57 database growth on the live host."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path("/var/lib/v57-fundamental")


def open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=60000")
    return con


def rows(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def db_pages(con: sqlite3.Connection) -> list[dict]:
    try:
        return rows(
            con,
            "SELECT name,COUNT(*) AS pages,SUM(pgsize) AS bytes "
            "FROM dbstat GROUP BY name ORDER BY bytes DESC LIMIT 30",
        )
    except sqlite3.DatabaseError as exc:
        return [{"error": type(exc).__name__}]


def main() -> None:
    result: dict[str, object] = {}

    structured_path = ROOT / "db" / "structured.sqlite3"
    with open_ro(structured_path) as con:
        result["structured"] = {
            "pages": db_pages(con),
            "top_providers": rows(
                con,
                "SELECT provider,record_type,COUNT(*) AS rows,COUNT(DISTINCT entity_key) AS entities,"
                "SUM(LENGTH(payload_json)) AS payload_bytes,ROUND(AVG(LENGTH(payload_json)),1) AS avg_payload,"
                "MIN(observed_at) AS first_at,MAX(observed_at) AS last_at "
                "FROM snapshots GROUP BY provider,record_type ORDER BY payload_bytes DESC LIMIT 40",
            ),
            "buckets": rows(
                con,
                "SELECT time_bucket,COUNT(*) AS rows,SUM(LENGTH(payload_json)) AS payload_bytes "
                "FROM snapshots GROUP BY time_bucket ORDER BY time_bucket DESC LIMIT 20",
            ),
        }

    news_path = ROOT / "db" / "news.sqlite3"
    with open_ro(news_path) as con:
        result["news"] = {
            "pages": db_pages(con),
            "top_sources": rows(
                con,
                "SELECT source_key,COUNT(*) AS rows,SUM(LENGTH(title)+LENGTH(summary)+LENGTH(payload_json)) AS text_bytes,"
                "ROUND(AVG(priority),2) AS avg_priority,MIN(observed_at) AS first_at,MAX(observed_at) AS last_at "
                "FROM raw_news GROUP BY source_key ORDER BY rows DESC LIMIT 40",
            ),
            "priority": rows(
                con,
                "SELECT priority,state,COUNT(*) AS rows FROM raw_news GROUP BY priority,state ORDER BY priority DESC,state",
            ),
        }

    control_path = ROOT / "db" / "control.sqlite3"
    with open_ro(control_path) as con:
        result["control"] = {
            "pages": db_pages(con),
            "stories": rows(
                con,
                "SELECT category,materiality,COUNT(*) AS rows,"
                "SUM(LENGTH(title)+LENGTH(summary)+LENGTH(COALESCE(article_text,''))) AS text_bytes "
                "FROM stories GROUP BY category,materiality ORDER BY rows DESC LIMIT 40",
            ),
            "translation_jobs": rows(
                con, "SELECT state,COUNT(*) AS rows FROM translation_jobs GROUP BY state ORDER BY rows DESC"
            ),
            "enrichment_jobs": rows(
                con, "SELECT state,COUNT(*) AS rows FROM enrichment_jobs GROUP BY state ORDER BY rows DESC"
            ),
        }

    brain_path = ROOT / "brain" / "intelligence.sqlite3"
    with open_ro(brain_path) as con:
        result["brain"] = {
            "pages": db_pages(con),
            "events": rows(
                con,
                "SELECT event_type,COUNT(*) AS rows,ROUND(AVG(importance),3) AS avg_importance "
                "FROM events GROUP BY event_type ORDER BY rows DESC LIMIT 40",
            ),
            "response_targets": rows(
                con,
                "SELECT state,horizon_seconds,COUNT(*) AS rows FROM response_targets "
                "GROUP BY state,horizon_seconds ORDER BY rows DESC LIMIT 50",
            ),
            "audit": rows(
                con,
                "SELECT actor,action,object_type,COUNT(*) AS rows,SUM(LENGTH(detail_json)) AS detail_bytes "
                "FROM audit_log GROUP BY actor,action,object_type ORDER BY detail_bytes DESC LIMIT 30",
            ),
        }

    result["market"] = {}
    for path in sorted((ROOT / "market").glob("market_*/semantic.sqlite3")):
        with open_ro(path) as con:
            result["market"][path.parent.name] = {
                "pages": db_pages(con),
                "types": rows(
                    con,
                    "SELECT event_type,COUNT(*) AS rows,SUM(LENGTH(payload_json)) AS payload_bytes "
                    "FROM events GROUP BY event_type ORDER BY rows DESC",
                ),
            }

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
