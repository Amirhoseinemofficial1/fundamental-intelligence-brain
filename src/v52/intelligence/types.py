from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class EventUnderstanding:
    event_type: str
    domain: str
    event_state: str
    cluster_key: str
    update_key: str
    title: str
    summary: str
    occurred_at: str
    observed_at: str
    entities: tuple[dict[str, str], ...]
    assets: tuple[dict[str, Any], ...]
    relations: tuple[dict[str, str], ...]
    importance: float
    novelty: float
    surprise: float | None
    source_credibility: float
    priority: str
    expected_horizons: tuple[int, ...]
    rationale: tuple[str, ...]
    extraction_method: str = "deterministic"
    extraction_version: str = "52.0-taxonomy-1"
    subtype: str = "GENERAL"
    country: str = "GLOBAL"
    sector: str = "GENERAL"
    priced_in_score: float = 0.5
    historical_analogues: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ExpertVote:
    expert: str
    asset_id: str
    horizon_seconds: int
    direction_probability: float
    expected_magnitude: float
    uncertainty: float
    weight: float
    rationale: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SignalDecision:
    action: str
    asset_id: str
    horizon_seconds: int
    probability: float
    calibrated_confidence: float
    expected_magnitude: float
    uncertainty: float
    expected_value: float
    risk_score: float
    entry_logic: str
    invalidation_logic: str
    target_logic: str
    exit_logic: str
    expert_agreement: float
    evidence_count: int
    mode: str
    reasons: tuple[str, ...]
