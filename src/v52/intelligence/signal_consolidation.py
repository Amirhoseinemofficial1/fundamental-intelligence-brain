from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class ConsolidatedSignal:
    item: dict[str, Any]
    score: float
    suppressed: int


class SignalConsolidator:
    """Select one user-facing decision per event/asset while retaining all internals."""

    @staticmethod
    def score(item: dict[str, Any]) -> float:
        decision = item["decision"]
        feature = item["feature"]
        action_factor = 0.0 if decision.action in {"NO_TRADE", "WAIT"} else 1.0
        return action_factor * float(decision.calibrated_confidence) * (1.0 - float(decision.risk_score)) * max(0.0, float(decision.expected_value)) * max(.05, float(feature.get("quality") or 0.0))

    def consolidate(self, items: Iterable[dict[str, Any]]) -> ConsolidatedSignal | None:
        candidates = list(items)
        if not candidates:
            return None
        ranked = sorted(candidates, key=lambda item: (self.score(item), -int(item["decision"].horizon_seconds)), reverse=True)
        best = ranked[0]
        return ConsolidatedSignal(best, self.score(best), max(0, len(ranked) - 1))
