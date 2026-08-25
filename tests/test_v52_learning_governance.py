from __future__ import annotations

import asyncio
import json
import time

from v52.intelligence.experts import brier_score, calibration_error, purged_walk_forward
from v52.intelligence.research import ResearchService, _apply_calibrator, _calibrator
from v52.intelligence.store import BrainStore
from v52.metrics import Metrics
from v52.storage.sqlite import connect


def test_purged_walk_forward_has_no_future_or_embargo_leakage() -> None:
    rows = [(float(i), [i / 100], i % 2) for i in range(60)]
    splits = purged_walk_forward(rows, folds=4, embargo=2)
    assert splits
    for train, validation in splits:
        assert max(train) < min(validation)
        assert min(validation) - max(train) >= 3


def test_calibrator_is_trained_from_labels_and_serializable() -> None:
    rows = [{"probability": .8, "label": 1} for _ in range(30)] + [{"probability": .2, "label": 0} for _ in range(30)]
    artifact = _calibrator(rows)
    assert _apply_calibrator(.8, artifact) > .8
    assert _apply_calibrator(.2, artifact) < .2
    assert artifact["samples"] == 60


def test_walk_forward_evaluation_reports_calibration_metrics() -> None:
    rows = [{"created_at": float(i), "probability": .8 if i % 2 else .2, "label": 1 if i % 2 else 0} for i in range(100)]
    result = ResearchService._evaluate_walk_forward(rows)
    assert result["valid"]
    assert result["calibrated_brier"] < .25
    assert result["calibration_error"] < .25
    assert result["folds"]


def test_brier_and_calibration_reject_fake_confidence() -> None:
    probabilities = [.99, .99, .01, .01]
    labels = [0, 0, 1, 1]
    assert brier_score(probabilities, labels) > .9
    assert calibration_error(probabilities, labels) > .9


def test_challenger_creates_real_experiment_and_model(settings) -> None:
    async def run() -> None:
        service = ResearchService(settings, Metrics("research-test", settings.state_dir))
        service.minimum_training_rows = 40
        rows = [{"created_at": float(i), "probability": .75 if i % 2 else .25, "label": 1 if i % 2 else 0} for i in range(100)]
        service._learning_rows_sync = lambda limit=10000: rows  # type: ignore[method-assign]
        result = await service.train_challenger()
        assert result["created"]
        with connect(service.path, settings) as con:
            model = con.execute("SELECT status FROM model_versions WHERE model_version_id=?", (result["model_version_id"],)).fetchone()
            experiment = con.execute("SELECT status,result_json FROM experiments WHERE experiment_id=?", (f"experiment:{result['model_version_id']}",)).fetchone()
        assert model["status"] == "CHALLENGER"
        assert experiment["status"] == "EVALUATED"
        assert json.loads(experiment["result_json"])["valid"]
    asyncio.run(run())


def test_model_rollback_is_atomic_and_audited(settings) -> None:
    async def run() -> None:
        brain = BrainStore(settings)
        now = time.time()
        with connect(brain.path, settings) as con:
            con.execute(
                """INSERT INTO model_versions(model_version_id,model_name,role,status,code_version,data_version,feature_version,
                   hyperparameters_json,metrics_json,artifact_json,created_at,promoted_at,retired_at)
                   VALUES('validated-previous','meta_ensemble','META','RETIRED','52.0.0','fixture','features-52.0.0','{}','{}','{}',?,?,?)""",
                (now - 100, now - 90, now - 10),
            )
        result = await brain.rollback_model("validated-previous")
        assert result["rolled_back"]
        with connect(brain.path, settings) as con:
            champions = con.execute("SELECT model_version_id FROM model_versions WHERE status='CHAMPION'").fetchall()
            audit = con.execute("SELECT action FROM audit_log WHERE action='MODEL_ROLLBACK'").fetchone()
        assert [row["model_version_id"] for row in champions] == ["validated-previous"]
        assert audit["action"] == "MODEL_ROLLBACK"
    asyncio.run(run())
