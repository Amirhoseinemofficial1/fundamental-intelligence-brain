#!/usr/bin/env python3
"""Read-only post-deployment audit for the live V57 data directory."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


DATA = Path("/var/lib/v57-fundamental")


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def audit_database(path: Path) -> dict[str, object]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=60000")
    tables = [
        str(row[0])
        for row in con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    counts: dict[str, int | str] = {}
    for table in tables:
        try:
            counts[table] = int(
                con.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0]
            )
        except sqlite3.DatabaseError as exc:
            counts[table] = f"ERROR:{type(exc).__name__}"

    result: dict[str, object] = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "tables": counts,
    }
    if "metadata" in tables:
        wanted = (
            "schema_version",
            "observation_runtime_seconds",
            "shadow_runtime_seconds",
            "signals_enabled",
        )
        placeholders = ",".join("?" for _ in wanted)
        result["metadata"] = {
            str(row[0]): str(row[1])
            for row in con.execute(
                f"SELECT key,value FROM metadata WHERE key IN ({placeholders}) ORDER BY key",
                wanted,
            )
        }
    if "semantic_memories" in tables:
        result["semantic_memories"] = [
            dict(row)
            for row in con.execute(
                "SELECT embedding_model,dimensions,COUNT(*) AS memories "
                "FROM semantic_memories GROUP BY embedding_model,dimensions "
                "ORDER BY embedding_model,dimensions"
            )
        ]
    if "raw_news" in tables:
        result["news_state"] = [
            dict(row)
            for row in con.execute(
                "SELECT state,COUNT(*) AS rows,ROUND(AVG(LENGTH(payload_json)),2) AS average_payload_bytes,"
                "MAX(LENGTH(payload_json)) AS max_payload_bytes,MIN(observed_at) AS first_observed_at,"
                "MAX(observed_at) AS last_observed_at FROM raw_news GROUP BY state ORDER BY state"
            )
        ]
        result["news_duplicate_raw_keys"] = int(
            con.execute(
                "SELECT COUNT(*) FROM (SELECT raw_key FROM raw_news GROUP BY raw_key HAVING COUNT(*)>1)"
            ).fetchone()[0]
        )
    if "snapshots" in tables:
        result["structured_payload"] = dict(
            con.execute(
                "SELECT COUNT(*) AS rows,ROUND(AVG(LENGTH(payload_json)),2) AS average_payload_bytes,"
                "MAX(LENGTH(payload_json)) AS max_payload_bytes,MIN(observed_at) AS first_observed_at,"
                "MAX(observed_at) AS last_observed_at FROM snapshots"
            ).fetchone()
        )
        result["structured_duplicate_keys"] = int(
            con.execute(
                "SELECT COUNT(*) FROM (SELECT provider,record_type,entity_key,time_bucket FROM snapshots "
                "GROUP BY provider,record_type,entity_key,time_bucket HAVING COUNT(*)>1)"
            ).fetchone()[0]
        )
    if "events" in tables and "/market/" in str(path).replace("\\", "/"):
        result["market_payload"] = dict(
            con.execute(
                "SELECT COUNT(*) AS rows,ROUND(AVG(LENGTH(payload_json)),2) AS average_payload_bytes,"
                "MAX(LENGTH(payload_json)) AS max_payload_bytes,MIN(observed_at) AS first_observed_at,"
                "MAX(observed_at) AS last_observed_at FROM events"
            ).fetchone()
        )
        result["market_duplicate_buckets"] = int(
            con.execute(
                "SELECT COUNT(*) FROM (SELECT venue,symbol,event_type,time_bucket FROM events "
                "GROUP BY venue,symbol,event_type,time_bucket HAVING COUNT(*)>1)"
            ).fetchone()[0]
        )
    con.close()
    return result


def main() -> None:
    databases = [audit_database(path) for path in sorted(DATA.rglob("*.sqlite3"))]
    statuses = sorted((DATA / "state").glob("*/status.json"))
    print(
        json.dumps(
            {
                "data_root": str(DATA.resolve()),
                "status_files": len(statuses),
                "databases": databases,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
