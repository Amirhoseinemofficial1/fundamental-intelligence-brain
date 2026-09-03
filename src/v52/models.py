from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(slots=True)
class NewsSource:
    key: str
    name: str
    url: str
    category: str = "GENERAL"
    region: str = "GLOBAL"
    language: str = ""
    official: bool = False
    tier: int = 2
    mode: str = "feed"
    base_poll_seconds: float = 60.0


@dataclasses.dataclass(slots=True)
class RawNewsItem:
    raw_key: str
    source_key: str
    source_name: str
    source_url: str
    item_url: str
    title: str
    summary: str
    category: str
    region: str
    language: str
    published_at: str | None
    observed_at: str
    priority: int
    payload: dict[str, Any]


@dataclasses.dataclass(slots=True)
class StoryCandidate:
    fingerprint: str
    title: str
    summary: str
    canonical_url: str
    category: str
    region: str
    language: str
    published_at: str | None
    observed_at: str
    materiality: int
    reasons: list[str]
    source_name: str
    source_url: str
    raw_id: int


@dataclasses.dataclass(slots=True)
class MarketEvent:
    event_type: str
    venue: str
    symbol: str
    exchange_ts: str | None
    observed_at: str
    lane: str
    payload: dict[str, Any]
