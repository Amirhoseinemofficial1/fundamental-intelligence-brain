from __future__ import annotations

import builtins

import pytest

from v52.intelligence.expert_lifecycle import TrainableExpertLifecycle
from v52.intelligence.experts import SPECIALISTS
from v52.intelligence.store import BrainStore


@pytest.mark.parametrize("expert", [spec.name for spec in SPECIALISTS])
def test_every_specialist_has_persisted_train_shadow_promote_runtime_lifecycle(settings, monkeypatch, expert):
    monkeypatch.setenv("MODEL_MIN_TRAIN_ROWS", "20")
    BrainStore(settings)
    original_import = builtins.__import__

    def core_only(name, *args, **kwargs):
        if name.startswith("sklearn"):
            raise ImportError("fixture exercises dependency-safe core tier")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", core_only)
    rows = [
        {
            "prediction_time": 1000 + i,
            "feature_version": "features-52.true.1",
            "values": {"event_importance": -1 + 2 * i / 23, "source_quality": .9},
            "label": int(i >= 12),
        }
        for i in range(24)
    ]
    lifecycle = TrainableExpertLifecycle(settings, expert)
    result = lifecycle.train_candidates(rows)
    assert lifecycle.evaluate_candidates(result)["validation_samples"] > 0
    assert lifecycle.calibrate(result["artifact_id"])["method"] == "platt"
    assert lifecycle.serialize_artifact(result["artifact_id"])["sha256_verified"]
    assert lifecycle.shadow_infer({"event_importance": .8})[0]["artifact_id"] == result["artifact_id"]
    lifecycle.promote(result["artifact_id"])
    inference = TrainableExpertLifecycle(settings, expert).infer({"event_importance": .8, "source_quality": .9})
    assert inference["artifact_id"] == result["artifact_id"]
    assert 0 <= inference["probability"] <= 1


@pytest.mark.parametrize("expert", ["entry", "exit"])
def test_entry_and_exit_are_trainable_artifact_policies(settings, monkeypatch, expert):
    monkeypatch.setenv("MODEL_MIN_TRAIN_ROWS", "20")
    BrainStore(settings)
    rows = [
        {
            "prediction_time": 2000 + i,
            "feature_version": "features-52.true.1",
            "values": {"event_importance": -1 + 2 * i / 39, "mfe_context": i / 40, "mae_context": 1 - i / 40},
            "label": int(i >= 20),
        }
        for i in range(40)
    ]
    lifecycle = TrainableExpertLifecycle(settings, expert)
    result = lifecycle.train_candidates(rows)
    lifecycle.promote(result["artifact_id"])
    low = lifecycle.infer({"event_importance": -.8, "mfe_context": .1, "mae_context": .9})["probability"]
    high = lifecycle.infer({"event_importance": .8, "mfe_context": .9, "mae_context": .1})["probability"]
    assert low != high
