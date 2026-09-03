from __future__ import annotations

import json
import time
from typing import Any

from ..config import Settings
from ..storage.sqlite import connect
from .learning import assert_prediction_time_contract
from .model_factory import ArtifactRegistry, ModelFactory


class RegimeGovernance:
    """One-vs-rest governed regime classifiers with real runtime shadowing."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.data_dir / "brain" / "intelligence.sqlite3"
        self.registry = ArtifactRegistry(settings)
        self.factory = ModelFactory(settings)

    @staticmethod
    def expert(regime: str) -> str:
        return "regime_classifier:" + str(regime).upper().replace(" ", "_")

    def train(self, minimum_rows: int = 40) -> list[dict[str, Any]]:
        with connect(self.path, self.settings) as con:
            rows = con.execute("""SELECT fv.feature_id,fv.prediction_time,fv.available_cutoff,fv.feature_version,fv.values_json,r.regime,r.observed_at
                FROM feature_vectors fv JOIN regimes r ON r.id=(SELECT id FROM regimes x WHERE x.asset_id=fv.asset_id AND x.observed_at>=fv.prediction_time ORDER BY x.observed_at LIMIT 1)
                WHERE fv.replay=0 ORDER BY fv.prediction_time""").fetchall()
        regimes = sorted({str(row["regime"]) for row in rows if str(row["regime"]) != "UNKNOWN"})
        created: list[dict[str, Any]] = []
        for regime in regimes:
            expert = self.expert(regime)
            with connect(self.path, self.settings) as con:
                if con.execute("SELECT 1 FROM model_artifacts WHERE expert=? AND state='SHADOW'", (expert,)).fetchone():
                    continue
            training = []
            for row in rows:
                values = {str(k): float(v) for k, v in json.loads(row["values_json"]).items() if isinstance(v, (int, float))}
                contract = {"prediction_time": row["prediction_time"], "available_cutoff": row["available_cutoff"]}
                assert_prediction_time_contract(contract, values)
                training.append({"prediction_time": float(row["prediction_time"]), "available_cutoff": float(row["available_cutoff"]),
                    "label_available_at": float(row["observed_at"]), "values": values,
                    "label": int(str(row["regime"]) == regime), "feature_version": str(row["feature_version"])})
            if len(training) < minimum_rows or len({x["label"] for x in training}) < 2:
                created.append({"expert": expert, "status": "NOT_QUALIFIED", "rows": len(training)})
                continue
            try:
                created.append(self.factory.train(expert, training, state="SHADOW"))
            except ValueError as exc:
                created.append({"expert": expert, "status": "NOT_QUALIFIED", "reason": str(exc)})
        return created

    def infer(self, feature_id: str, values: dict[str, float], prediction_time: float) -> dict[str, Any]:
        with connect(self.path, self.settings) as con:
            rows = con.execute("SELECT artifact_id,expert,state FROM model_artifacts WHERE expert LIKE 'regime_classifier:%' AND state IN ('CHAMPION','SHADOW')").fetchall()
        raw: dict[str, float] = {}; artifacts: list[str] = []
        for row in rows:
            artifact = self.registry.load(str(row["artifact_id"]));probability = float(artifact.predict(values));state = str(row["state"]);regime = str(row["expert"]).split(":", 1)[1]
            with connect(self.path, self.settings) as con:
                con.execute("INSERT OR IGNORE INTO expert_outputs(feature_id,expert,artifact_id,model_state,probability,uncertainty,created_at) VALUES(?,?,?,?,?,?,?)",(feature_id,row["expert"],row["artifact_id"],state,probability,.5,time.time()))
                if state == "SHADOW":con.execute("INSERT OR IGNORE INTO shadow_predictions(feature_id,artifact_id,probability,prediction_time) VALUES(?,?,?,?)",(feature_id,row["artifact_id"],probability,prediction_time))
            if state == "CHAMPION":raw[regime]=probability;artifacts.append(str(row["artifact_id"]))
        total=sum(raw.values());probabilities={k:v/total for k,v in raw.items()} if total>0 else {}
        current=max(probabilities,key=probabilities.get) if probabilities else None
        uncertainty=1.0-max(probabilities.values()) if probabilities else 1.0
        return {"probabilities":probabilities,"current_regime":current,"uncertainty":uncertainty,"artifact_ids":artifacts}
