from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from typing import Any, Iterable

from ..config import Settings
from ..storage.sqlite import connect
from .model_factory import ArtifactRegistry


@dataclass(frozen=True, slots=True)
class DecisionForecast:
    expected_return: float
    expected_mfe: float
    expected_mae: float
    magnitude_uncertainty: float
    preferred_horizon: int
    time_to_peak: float
    risk_score: float
    artifact_ids: tuple[str, ...]
    sources: dict[str, str]


class DecisionModelEngine:
    """Runtime owner for magnitude, horizon, time-to-peak and risk artifacts.

    These targets deliberately do not borrow a classifier probability or a fixed
    constant.  Each output is either produced by its own registered Champion or by
    an explicitly labelled empirical fallback based on already-resolved outcomes.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.registry = ArtifactRegistry(settings)
        self.path = self.registry.path

    @staticmethod
    def _safe(value: float, low: float, high: float) -> float:
        if not math.isfinite(value):
            return low
        return min(high, max(low, value))

    def _record(self, feature: dict[str, Any], expert: str, artifact: Any, value: float, state: str) -> None:
        feature_id = str(feature["feature_id"])
        artifact_id = str(artifact.metadata["artifact_id"])
        metrics = artifact.metadata.get("metrics") or {}
        uncertainty = self._safe(float(metrics.get("mae", metrics.get("brier", .5))), .0, 1.0)
        with connect(self.path, self.settings) as con:
            con.execute(
                "INSERT OR IGNORE INTO expert_outputs(feature_id,expert,artifact_id,model_state,probability,uncertainty,created_at) VALUES(?,?,?,?,?,?,?)",
                (feature_id, expert, artifact_id, state, float(value), uncertainty, time.time()),
            )
            if state == "SHADOW":
                con.execute(
                    "INSERT OR IGNORE INTO shadow_predictions(feature_id,artifact_id,probability,prediction_time) VALUES(?,?,?,?)",
                    (feature_id, artifact_id, float(value), float(feature["prediction_time"])),
                )

    def _models(self,experts:set[str])->dict[str,dict[str,list[Any]]]:
        result={expert:{'CHAMPION':[],'SHADOW':[]} for expert in experts}
        if not experts:return result
        query_marks=','.join('?' for _ in experts)
        with connect(self.path,self.settings) as con:rows=con.execute(f"SELECT artifact_id,expert,state FROM model_artifacts WHERE expert IN ({query_marks}) AND state IN ('CHAMPION','SHADOW')",tuple(sorted(experts))).fetchall()
        for row in rows:result[str(row['expert'])][str(row['state'])].append(self.registry.load(str(row['artifact_id'])))
        return result

    def _target(self, expert: str, feature: dict[str, Any], models:dict[str,dict[str,list[Any]]]) -> tuple[float | None, str | None, float]:
        champion_list=models.get(expert,{}).get('CHAMPION',[]);champion=champion_list[0] if champion_list else None
        value: float | None = None
        artifact_id: str | None = None
        uncertainty = .5
        if champion:
            value = float(champion.predict(feature["values"]))
            artifact_id = str(champion.metadata["artifact_id"])
            uncertainty = float((champion.metadata.get("metrics") or {}).get("mae", (champion.metadata.get("metrics") or {}).get("brier", .25)))
            self._record(feature, expert, champion, value, "CHAMPION")
        for shadow in models.get(expert,{}).get('SHADOW',[]):
            self._record(feature, expert, shadow, float(shadow.predict(feature["values"])), "SHADOW")
        return value, artifact_id, uncertainty

    def forecast(
        self,
        feature: dict[str, Any],
        candidate_horizons: Iterable[int],
        historical_returns: Iterable[float] = (),
    ) -> DecisionForecast:
        history = [float(x) for x in historical_returns if math.isfinite(float(x))]
        empirical = statistics.mean(abs(x) for x in history) if history else 0.0
        values: dict[str, float] = {}
        sources: dict[str, str] = {}
        artifacts: list[str] = []
        uncertainties: list[float] = []
        horizons = sorted({int(x) for x in candidate_horizons if int(x) > 0}) or [int(feature.get("horizon_seconds") or 300)]
        target_names={"magnitude_return", "magnitude_mfe", "magnitude_mae", "time_to_peak", "risk"}|{f"horizon_score:{horizon}" for horizon in horizons};models=self._models(target_names)
        for target in ("magnitude_return", "magnitude_mfe", "magnitude_mae", "time_to_peak", "risk"):
            value, artifact_id, uncertainty = self._target(target, feature,models)
            if value is not None:
                values[target] = value
                sources[target] = "TRAINED_CHAMPION"
                artifacts.append(str(artifact_id))
                uncertainties.append(abs(uncertainty))
            else:
                sources[target] = "EMPIRICAL_HISTORY_FALLBACK"

        expected_return = self._safe(values.get("magnitude_return", empirical), 0.0, 1.0)
        expected_mfe = self._safe(values.get("magnitude_mfe", empirical * 1.35), 0.0, 2.0)
        expected_mae = self._safe(values.get("magnitude_mae", empirical * .75), 0.0, 2.0)
        time_to_peak = self._safe(values.get("time_to_peak", float(feature.get("horizon_seconds") or 0) * .5), 0.0, 172800.0)
        empirical_risk = expected_mae / max(expected_mfe + expected_mae, 1e-9) if expected_mfe or expected_mae else .5
        risk = self._safe(values.get("risk", empirical_risk), 0.0, 1.0)

        scores: dict[int, float] = {}
        for horizon in horizons:
            score, artifact_id, _ = self._target(f"horizon_score:{horizon}", feature,models)
            if score is not None:
                scores[horizon] = float(score)
                sources[f"horizon:{horizon}"] = "TRAINED_CHAMPION"
                artifacts.append(str(artifact_id))
            else:
                # Historical target proximity is data-driven and penalises distant
                # horizons when no separately validated horizon Champion exists.
                scores[horizon] = empirical / (1.0 + abs(math.log1p(horizon) - math.log1p(max(1.0, time_to_peak))))
                sources[f"horizon:{horizon}"] = "EMPIRICAL_HISTORY_FALLBACK"
        preferred = max(scores, key=lambda h: (scores[h], -h))
        uncertainty = self._safe(statistics.mean(uncertainties) if uncertainties else (1.0 / math.sqrt(max(1, len(history)))), .02, 1.0)
        return DecisionForecast(expected_return, expected_mfe, expected_mae, uncertainty, preferred, time_to_peak, risk, tuple(dict.fromkeys(artifacts)), sources)
