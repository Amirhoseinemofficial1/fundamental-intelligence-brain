#!/usr/bin/env python3
"""Read-only acquisition and queue audit for the running news pipeline."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 3)


def report(data_dir: Path, hours: float = 1.0) -> dict[str, object]:
    cutoff = time.time() - hours * 3600
    control = sqlite3.connect(f"file:{data_dir / 'db' / 'control.sqlite3'}?mode=ro", uri=True)
    news = sqlite3.connect(f"file:{data_dir / 'db' / 'news.sqlite3'}?mode=ro", uri=True)
    try:
        rows = control.execute(
            "SELECT published_at,first_observed_at,created_at,source_url,source_name,materiality "
            "FROM stories WHERE created_at>=?", (cutoff,)
        ).fetchall()
        pending = news.execute("SELECT COUNT(*) FROM raw_news WHERE state IN ('pending','leased')").fetchone()[0]
        oldest = news.execute(
            "SELECT MIN(ingested_at) FROM raw_news WHERE state IN ('pending','leased')"
        ).fetchone()[0]
        failing = news.execute(
            "SELECT name,consecutive_failures,last_status FROM source_health "
            "WHERE consecutive_failures>=3 AND last_seen_at>=? "
            "ORDER BY consecutive_failures DESC LIMIT 10", (cutoff,)
        ).fetchall()
    finally:
        control.close()
        news.close()

    age_all: list[float] = []
    age_direct: list[float] = []
    age_aggregator: list[float] = []
    processing: list[float] = []
    untimed = 0
    low_value = 0
    other_events = 0
    news_stories = 0
    for published_at, observed_at, created_at, source_url, source_name, materiality in rows:
        parsed_source = urlsplit(source_url or "")
        host = (parsed_source.hostname or "").lower()
        social = host in {"x.com", "twitter.com", "bsky.app", "reddit.com", "www.reddit.com"} or host.endswith((".bsky.social", ".reddit.com"))
        structured = "_" in str(source_name or "") and str(source_name or "").upper() == str(source_name or "")
        if parsed_source.scheme not in {"http", "https"} or social or structured:
            other_events += 1
            continue
        news_stories += 1
        low_value += int(materiality < 4)
        published, observed = timestamp(published_at), timestamp(observed_at)
        if published is None or observed is None:
            untimed += 1
            continue
        age = observed - published
        if age < -30:
            continue
        age_all.append(age)
        (age_aggregator if host == "news.google.com" or host.endswith("gdeltproject.org") or "gdelt" in str(source_name or "").lower() else age_direct).append(age)
        processing.append(max(0.0, created_at - observed))

    def distribution(values: list[float]) -> dict[str, object]:
        return {
            "count": len(values), "p50_seconds": percentile(values, .50),
            "p95_seconds": percentile(values, .95),
            "under_60_seconds_ratio": round(sum(0 <= x <= 60 for x in values) / len(values), 4) if values else None,
        }

    return {
        "window_hours": hours,
        "all_control_stories": len(rows), "news_stories": news_stories,
        "social_or_structured_events_excluded": other_events,
        "low_materiality_below_4": low_value,
        "untimed": untimed,
        "publisher_to_first_observed": distribution(age_all),
        "direct_source": distribution(age_direct),
        "aggregator": distribution(age_aggregator),
        "observed_to_story_p95_seconds": percentile(processing, .95),
        "raw_pending": pending,
        "oldest_raw_pending_age_seconds": round(time.time() - oldest, 1) if oldest else None,
        "sources_with_repeated_failures": [
            {"name": name, "failures": failures, "status": status}
            for name, failures, status in failing
        ],
        "direct_source_24h_p95_target_met": hours >= 24 and len(age_direct) >= 30 and percentile(age_direct, .95) <= 60,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/v57-fundamental"))
    parser.add_argument("--hours", type=float, default=1.0)
    args = parser.parse_args()
    print(json.dumps(report(args.data_dir, args.hours), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
