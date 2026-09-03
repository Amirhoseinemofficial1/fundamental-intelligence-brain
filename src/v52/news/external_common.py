from __future__ import annotations

import hashlib
from typing import Any

from ..models import RawNewsItem
from ..timeutil import iso_now, parse_datetime, to_iso
from .parse import canonical_url, clean_text


def make_raw_news(
    *,
    source_key: str,
    source_name: str,
    source_url: str,
    item_url: str,
    title: str,
    summary: str = "",
    category: str = "GLOBAL_NEWS",
    region: str = "GLOBAL",
    language: str = "",
    published_at: Any = None,
    external_id: str = "",
    priority: int = 5,
    payload: dict[str, Any] | None = None,
) -> RawNewsItem | None:
    title = clean_text(title, 1200)
    summary = clean_text(summary, 12000)
    url = canonical_url(item_url)
    if not title or not url:
        return None
    pub = to_iso(parse_datetime(published_at)) if published_at else None
    basis = external_id.strip() or url or f"{title}|{pub or ''}"
    raw_key = hashlib.sha256(f"{source_key}|{basis}".encode("utf-8", errors="ignore")).hexdigest()
    return RawNewsItem(
        raw_key=raw_key,
        source_key=source_key,
        source_name=source_name,
        source_url=source_url,
        item_url=url,
        title=title,
        summary=summary,
        category=category,
        region=region,
        language=language,
        published_at=pub,
        observed_at=iso_now(),
        priority=max(0, int(priority)),
        payload=dict(payload or {}),
    )
