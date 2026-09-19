from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import os
import time
from typing import Any

from ..config import Settings
from ..storage.sqlite import connect
from .signal_state import SignalStateMachine
from .store import BrainStore
from .features import FeatureStore, canonical_feature_name, feature_ttl
from .inference import RuntimeInferenceEngine
from ..storage.control import ControlStore
from .telegram_producers import TelegramIntelligenceProducers
from .model_factory import ArtifactRegistry
from .learning import EXPERT_FEATURES, REALIZED_OUTCOME_FIELDS


POST_EXIT_HORIZONS = (60, 180, 300, 900, 1800, 3600, 14400)


class ActiveSignalMonitorService:
    """Continuously consumes market state for every open signal, without needing news."""

    def __init__(self, settings: Settings, metrics: Any | None = None):
        self.settings = settings
        self.metrics = metrics
        self.path = BrainStore(settings).path
        self.machine = SignalStateMachine(settings)
        self.registry = ArtifactRegistry(settings)
        self.features = FeatureStore(settings)
        self.inference = RuntimeInferenceEngine(settings)
        self.telegram = TelegramIntelligenceProducers(ControlStore(settings))
        self.poll_seconds = max(.05, float(os.getenv("ACTIVE_SIGNAL_MONITOR_SECONDS", "2")))
        self.exit_confirmation_seconds = max(0.0, float(os.getenv("EXIT_CONFIRMATION_SECONDS", "1")))
        self.exit_recovery_seconds = max(1.0, float(os.getenv("EXIT_PENDING_RECOVERY_SECONDS", "60")))

    @staticmethod
    def _loads(value: Any) -> dict[str, Any]:
        try:
            result = json.loads(value or "{}")
            return result if isinstance(result, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _latest_feature_state(self, con: Any, asset_id: str, now: float) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        rows = con.execute(
            """SELECT observation_type,value,event_time,available_time,source_id,source_quality,quality,metadata_json FROM canonical_observations
               WHERE available_time<=? AND (asset_ids_json LIKE ? OR asset_ids_json='[]')
               ORDER BY available_time DESC LIMIT 500""",
            (now, f'%"{asset_id}"%'),
        ).fetchall()
        values: dict[str, Any] = {}
        freshness: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = canonical_feature_name(str(row["observation_type"]))
            if name in freshness:
                continue
            available = float(row["available_time"])
            ttl = feature_ttl(name)
            valid = row["value"] is not None and now <= available + ttl
            freshness[name] = {
                "value": None if row["value"] is None else float(row["value"]),
                "observed_at": float(row["event_time"]),
                "available_at": available,
                "expires_at": available + ttl,
                "source": str(row["source_id"]),
                "source_quality": float(row["source_quality"]),
                "quality": float(row["quality"]),
                "valid": valid,
                "ttl_seconds": ttl,
            }
            if valid:
                # Zero is deliberately retained; only None represents missing.
                values[name] = float(row["value"])
        return values, freshness

    def _latest_features(self, con: Any, asset_id: str, now: float) -> dict[str, Any]:
        """Compatibility wrapper retained for diagnostics and older integrations."""
        return self._latest_feature_state(con, asset_id, now)[0]

    def _price_path(self, con: Any, asset_id: str, start: float, now: float) -> list[tuple[float, float]]:
        rows = con.execute(
            """SELECT observed_at,price FROM reference_prices WHERE asset_id=?
               AND available_at<=? AND observed_at>=? ORDER BY observed_at""",
            (asset_id, now, start),
        ).fetchall()
        if rows:
            return [(float(r["observed_at"]), float(r["price"])) for r in rows]
        entry = con.execute(
            "SELECT observed_at,price FROM reference_prices WHERE asset_id=? AND available_at<=? ORDER BY observed_at DESC LIMIT 1",
            (asset_id, start),
        ).fetchone()
        return [(float(entry["observed_at"]), float(entry["price"]))] if entry else []

    @staticmethod
    def _feature(values: dict[str, Any], *names: str) -> float | None:
        for name in names:
            if name in values and values[name] is not None:
                return float(values[name])
        return None

    def _build_active_feature(
        self,
        con: Any,
        row: Any,
        now: float,
        entry: float,
        current: float,
        current_return: float,
        mfe_so_far: float,
        mae_so_far: float,
        regime: str,
        regime_row: Any | None,
        fresh_values: dict[str, Any],
        freshness: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[float]]:
        """Create the prediction-time feature row that controls this refresh.

        This deliberately creates a new immutable feature vector.  It never
        mutates/reuses the opening vector and it cannot read market responses
        whose end time is after ``now``.
        """
        event_row = con.execute("SELECT * FROM events WHERE id=?", (row["event_id"],)).fetchone()
        asset_row = con.execute(
            """SELECT a.*,COALESCE(ea.mapping_confidence,.5) mapping_confidence
               FROM assets a LEFT JOIN event_assets ea ON ea.asset_id=a.asset_id AND ea.event_id=?
               WHERE a.asset_id=?""",
            (row["event_id"], row["asset_id"]),
        ).fetchone()
        event = dict(event_row) if event_row else {
            "id": int(row["event_id"]), "event_type": "ACTIVE_SIGNAL_REFRESH", "domain": "MARKET",
            "importance": .5, "novelty": 0.0, "source_credibility": .5,
        }
        event.setdefault("priced_in_score", .5)
        event.setdefault("historical_analogues", ())
        asset = dict(asset_row) if asset_row else {
            "asset_id": str(row["asset_id"]), "asset_class": "CRYPTO", "mapping_confidence": .5,
        }
        feature = self.features.build_sync(event, asset, int(row["horizon_seconds"]), prediction_time=now)
        values = dict(feature["values"])
        values.update(fresh_values)
        values.update({
            "current_price": float(current),
            "entry_price": float(entry),
            "current_position_return": float(current_return),
            "return_since_entry": float(current_return),
            "mfe_so_far": float(mfe_so_far),
            "mae_so_far": float(mae_so_far),
            "event_age": max(0.0, now - float(row["opened_at"])),
            "position_fraction": float(row["position_fraction"]),
            "partial_exit_count": float(row["partial_exit_count"]),
        })
        # Canonical aliases expected by governed active/exit models.
        if "oi_change" in values:
            values["open_interest_change"] = float(values["oi_change"])
        values["whale_flow"] = float(values.get("smart_money_flow", values.get("whale_transfer_value", 0.0)))
        values["whale_activity"] = float(values.get("whale_transfer_count", 0.0))
        regime_payload = self._loads(regime_row["features_json"]) if regime_row else {}
        values.update({
            "regime_transition_probability": float(regime_payload.get("transition_probability") or 0.0),
            "regime_uncertainty": float(regime_payload.get("uncertainty") or 1.0),
            "regime_trend": float(regime_row["trend_score"]) if regime_row else 0.0,
            "regime_volatility": float(regime_row["volatility_score"]) if regime_row else 0.0,
            "regime_change_point": float(regime_row["change_score"]) if regime_row else 0.0,
            "regime_liquidity": float(regime_row["liquidity_score"]) if regime_row else 0.0,
        })
        for name, probability in (regime_payload.get("probabilities") or {}).items():
            if isinstance(probability, (int, float)):
                values[f"regime_probability_{canonical_feature_name(str(name))}"] = float(probability)
        # Event revision evidence is observable at refresh time and therefore safe.
        revisions = con.execute(
            "SELECT state,COUNT(*) count FROM event_updates WHERE event_id=? AND created_at<=? GROUP BY state",
            (row["event_id"], now),
        ).fetchall()
        revision_counts = {str(item["state"]).upper(): int(item["count"]) for item in revisions}
        values["event_revision_count"] = float(sum(revision_counts.values()))
        values["adverse_evidence_count"] = float(sum(v for k, v in revision_counts.items() if k in {"CORRECTION", "RETRACTION", "CONTRADICTED"}))
        values["confirming_evidence_count"] = float(sum(v for k, v in revision_counts.items() if k in {"UPDATE", "CONFIRMED", "NEW"}))
        feature["values"] = values
        feature["freshness"] = {**feature.get("freshness", {}), **freshness}
        feature["feature_version"] = "features-57.3.0-active"
        provenance = list(feature.get("provenance") or ()) + [
            f"active_signal:{int(row['id'])}", f"price_available_at:{now:.6f}", f"regime:{regime}"
        ]
        feature["provenance"] = provenance
        # Quality is driven only by data that was actually available now.
        valid_fresh = [v for v in freshness.values() if v.get("valid")]
        if valid_fresh:
            fresh_quality = sum(float(v.get("quality") or 0.0) for v in valid_fresh) / len(valid_fresh)
            feature["quality"] = max(0.0, min(1.0, (float(feature["quality"]) + fresh_quality) / 2.0))
        con.execute(
            """UPDATE feature_vectors SET feature_version=?,quality=?,values_json=?,provenance_json=?,freshness_json=?
               WHERE feature_id=?""",
            (feature["feature_version"], feature["quality"], json.dumps(values, sort_keys=True),
             json.dumps(provenance, sort_keys=True), json.dumps(feature["freshness"], sort_keys=True), feature["feature_id"]),
        )
        outcomes_rows = con.execute(
            """SELECT mr.return_value FROM market_responses mr JOIN events e ON e.id=mr.event_id
               WHERE e.event_type=? AND mr.asset_id=? AND mr.horizon_seconds=? AND mr.measured_at<=?
               ORDER BY mr.measured_at DESC LIMIT 500""",
            (event.get("event_type"), row["asset_id"], row["horizon_seconds"], now),
        ).fetchall()
        outcomes = [float(item["return_value"]) for item in outcomes_rows]
        return feature, event, asset, outcomes

    def _snapshot(self, con: Any, row: Any, now: float) -> dict[str, Any] | None:
        path = self._price_path(con, str(row["asset_id"]), float(row["opened_at"]), now)
        if not path:
            return None
        prices = [p for _, p in path]
        entry = prices[0]
        current = prices[-1]
        long_signal = str(row["action"])=="BUY" or (str(row["action"]) in {"ENTER_ON_CONFIRMATION", "ENTER_ON_PULLBACK"} and float(row["direction_probability"])>=.5)
        signed = [(p / entry - 1.0) * (1.0 if long_signal else -1.0) for p in prices] if entry else [0.0]
        current_return = signed[-1]
        features, freshness = self._latest_feature_state(con, str(row["asset_id"]), now)
        liquidity = self._feature(features, "liquidity", "dex_liquidity", "depth", "orderbook_depth")
        volatility = self._feature(features, "volatility", "realized_volatility")
        regime_row = con.execute(
            "SELECT regime,trend_score,volatility_score,change_score,liquidity_score,confidence,features_json FROM regimes WHERE asset_id=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
            (row["asset_id"], now),
        ).fetchone()
        regime = str(regime_row["regime"]) if regime_row else "UNKNOWN"
        feature, event, asset, outcomes = self._build_active_feature(
            con, row, now, entry, current, current_return, max(signed), min(signed), regime, regime_row,
            features, freshness,
        )
        votes, refreshed_decision, active_inference = self.inference.infer(
            event, asset, int(row["horizon_seconds"]), feature, str(row["mode"]), outcomes,
            allow_bootstrap=False, candidate_horizons=(int(row["horizon_seconds"]),),
        )
        forecast = active_inference.get("decision_models") or {}
        updated_probability = float(refreshed_decision.probability) if votes else float(row["direction_probability"])
        updated_magnitude = float(forecast.get("expected_return", refreshed_decision.expected_magnitude if votes else row["expected_magnitude"]))
        updated_mfe = float(forecast.get("expected_mfe", abs(updated_magnitude)))
        updated_mae = float(forecast.get("expected_mae", float(row["risk_score"]) * abs(updated_magnitude)))
        updated_risk = float(forecast.get("risk_score", refreshed_decision.risk_score if votes else row["risk_score"]))
        updated_uncertainty = float(forecast.get("magnitude_uncertainty", refreshed_decision.uncertainty if votes else row["risk_score"]))
        expected = updated_magnitude * abs(updated_probability - .5) * 2.0 * (1.0 - updated_risk)
        adverse_liquidity = liquidity is not None and liquidity < .20
        adverse_volatility = volatility is not None and volatility > .80
        remaining_ev = expected + current_return - max(0.0, -min(signed)) * .5
        if adverse_liquidity:
            remaining_ev -= .04
        if adverse_volatility:
            remaining_ev -= .02
        # The governed Exit Champion is the primary decision owner.  The values
        # below are all observable at ``now``; realised future outcomes never enter
        # this vector.  Deterministic rules remain only as an explicitly identified
        # safety fallback when no qualified Exit Champion exists.
        # The Exit policy receives the complete active-time numeric state, not a
        # stale subset copied from signal opening.
        policy_values = {str(name): float(value) for name, value in feature["values"].items() if isinstance(value, (int, float))}
        policy_values.update({
            "prediction_probability": updated_probability,
            "calibrated_probability": updated_probability,
            "predicted_magnitude": updated_magnitude,
            "predicted_mfe": updated_mfe,
            "predicted_mae": updated_mae,
            "current_volatility": 0.0 if volatility is None else float(volatility),
            "current_liquidity": 0.0 if liquidity is None else float(liquidity),
            "current_regime": float(feature["values"].get("regime_probability", 0.0)),
            "event_age": max(0.0, now - float(row["opened_at"])),
            "expert_disagreement": updated_uncertainty,
            "current_price_path": current_return,
            "historical_analogue_mean": sum(outcomes) / len(outcomes) if outcomes else 0.0,
            "data_quality": float(feature["quality"]),
            "remaining_expected_value": remaining_ev,
            "updated_risk": updated_risk,
            "updated_uncertainty": updated_uncertainty,
        })
        if REALIZED_OUTCOME_FIELDS.intersection(policy_values):
            raise RuntimeError("realized future outcome leaked into active Exit policy")
        exit_model = self.registry.champion("exit_policy") or self.registry.champion("exit")
        exit_probability = float(exit_model.predict(policy_values)) if exit_model else None
        exit_artifact = str(exit_model.metadata["artifact_id"]) if exit_model else None
        if exit_model:
            con.execute(
                """INSERT OR IGNORE INTO expert_outputs(feature_id,expert,artifact_id,model_state,probability,uncertainty,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (feature["feature_id"], str(exit_model.metadata.get("expert") or "exit_policy"), exit_artifact,
                 "CHAMPION", exit_probability, updated_uncertainty, now),
            )
        for family, target_type in (
            ("exit_policy", "exit_action"),
            ("exit_quality", "exit_quality"),
            ("post_exit_optimizer", "post_exit_return"),
        ):
            for shadow in self.registry.shadows(family):
                probability = float(shadow.predict(policy_values))
                con.execute(
                    """INSERT OR IGNORE INTO expert_outputs(feature_id,expert,artifact_id,model_state,probability,uncertainty,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (feature["feature_id"], family, shadow.metadata["artifact_id"], "SHADOW", probability, .5, now),
                )
                con.execute(
                    """INSERT OR IGNORE INTO shadow_predictions(feature_id,artifact_id,probability,prediction_time,signal_id,target_type,label_text)
                       VALUES(?,?,?,?,?,?,NULL)""",
                    (feature["feature_id"], shadow.metadata["artifact_id"], probability, now, int(row["id"]), target_type),
                )
        if exit_probability is not None:
            if exit_probability >= .86 and current_return < 0:
                action = "INVALIDATE"
            elif exit_probability >= .68:
                action = "EXIT"
            elif exit_probability >= .55 and current_return > 0 and int(row["partial_exit_count"]) == 0:
                action = "PARTIAL_EXIT"
            elif exit_probability >= .40 or max(signed) >= .02:
                action = "TRAIL"
            else:
                action = "HOLD"
            decision_source = "EXIT_CHAMPION"
        else:
            if adverse_liquidity and current_return <= -.015:
                action = "EXIT"
            elif current_return <= -.06 or remaining_ev <= -.05:
                action = "INVALIDATE"
            elif current_return <= -.025 or remaining_ev <= -.02:
                action = "EXIT"
            elif current_return >= .03 and (adverse_liquidity or adverse_volatility) and int(row["partial_exit_count"]) == 0:
                action = "PARTIAL_EXIT"
            elif max(signed) >= .02:
                action = "TRAIL"
            else:
                action = "HOLD"
            decision_source = "DOCUMENTED_SAFETY_FALLBACK_NO_CHAMPION"
        groups = {
            "derivatives": {k: v for k, v in features.items() if any(x in k for x in ("funding", "interest", "basis", "liquidation"))},
            "onchain": {k: v for k, v in features.items() if any(x in k for x in ("whale", "exchange_flow", "holder", "bridge"))},
            "microstructure": {k: v for k, v in features.items() if any(x in k for x in ("spread", "depth", "orderbook", "imbalance"))},
            "fundamental": {k: v for k, v in features.items() if k not in {"liquidity", "volatility"}},
        }
        return {
            "price": current, "return_since_entry": current_return, "mfe_so_far": max(signed),
            "mae_so_far": min(signed), "liquidity": liquidity, "volatility": volatility,
            "regime": regime, "remaining_ev": remaining_ev, "action": action,
            "exit_probability": exit_probability, "exit_artifact": exit_artifact,
            "decision_source": decision_source, "feature_available_at": now,
            "feature_id": feature["feature_id"], "feature_freshness": feature["freshness"],
            "exit_features": policy_values,
            "active_inference": {
                **active_inference,
                "votes": [dataclasses.asdict(vote) for vote in votes],
                "decision": dataclasses.asdict(refreshed_decision),
                "updated_probability": updated_probability,
                "updated_magnitude": updated_magnitude,
                "updated_mfe": updated_mfe,
                "updated_mae": updated_mae,
                "updated_risk": updated_risk,
                "updated_uncertainty": updated_uncertainty,
            },
            **groups,
        }

    def _record_snapshot(self, con: Any, signal_id: int, now: float, snapshot: dict[str, Any]) -> None:
        con.execute(
            """INSERT INTO active_signal_snapshots(signal_id,observed_at,price,return_since_entry,mfe_so_far,mae_so_far,
               derivatives_json,onchain_json,liquidity,microstructure_json,volatility,regime,fundamental_json,remaining_ev,action,evidence_json,
               feature_id,inference_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, now, snapshot["price"], snapshot["return_since_entry"], snapshot["mfe_so_far"], snapshot["mae_so_far"],
             json.dumps(snapshot["derivatives"]), json.dumps(snapshot["onchain"]), snapshot["liquidity"],
             json.dumps(snapshot["microstructure"]), snapshot["volatility"], snapshot["regime"],
             json.dumps(snapshot["fundamental"]), snapshot["remaining_ev"], snapshot["action"], json.dumps(snapshot, default=str),
             snapshot.get("feature_id"), json.dumps(snapshot.get("active_inference") or {}, default=str)),
        )

    def _schedule_post_exit(self, con: Any, signal_id: int, exited_at: float, horizon: int) -> None:
        eligible = [horizon] if 0<horizon<60 else [seconds for seconds in POST_EXIT_HORIZONS if seconds <= horizon]
        for seconds in eligible:
            con.execute(
                "INSERT OR IGNORE INTO post_exit_targets(signal_id,horizon_seconds,due_at) VALUES(?,?,?)",
                (signal_id, seconds, exited_at + seconds),
            )

    def _complete_pending_exits(self, now: float) -> int:
        completed = 0
        with connect(self.path, self.settings) as con:
            rows = con.execute(
                """SELECT s.id,s.last_updated_at,p.horizon_seconds FROM signals s JOIN predictions p ON p.id=s.prediction_id
                   WHERE s.state='EXIT_PENDING'"""
            ).fetchall()
            for row in rows:
                age = now - float(row["last_updated_at"])
                if age < self.exit_confirmation_seconds and age < self.exit_recovery_seconds:
                    continue
                self.machine.transition_sync(int(row["id"]), "EXITED", "MONITOR_EXIT_CONFIRMED", {"consumer": "active_signal_monitor", "age": age,"effective_at":float(row['last_updated_at'])})
                with connect(self.path, self.settings) as write_con:
                    closed=write_con.execute('SELECT closed_at FROM signals WHERE id=?',(row['id'],)).fetchone()
                    self._schedule_post_exit(write_con, int(row["id"]), float(closed['closed_at']), int(row["horizon_seconds"]))
                completed += 1
        return completed

    @staticmethod
    def _classify(ret: float, gain: float, loss: float) -> str:
        if ret >= .02 or gain >= .035:
            return "MISSED_CONTINUATION" if ret > 0 else "EARLY_EXIT"
        if loss <= -.03:
            return "LOSS_AVOIDED"
        if ret <= -.02 and gain < .01:
            return "GOOD_EXIT"
        if gain >= .015 and ret < 0:
            return "FALSE_REVERSAL"
        return "LATE_EXIT" if ret < -.005 else "GOOD_EXIT"

    def _track_post_exit(self, now: float) -> int:
        completed = 0
        with connect(self.path, self.settings) as con:
            targets = con.execute(
                """SELECT pet.*,s.closed_at,s.action signal_action,p.asset_id FROM post_exit_targets pet JOIN signals s ON s.id=pet.signal_id
                   JOIN predictions p ON p.id=s.prediction_id WHERE pet.state='PENDING' AND pet.due_at<=? ORDER BY pet.due_at""",
                (now,),
            ).fetchall()
        for target in targets:
            with connect(self.path, self.settings) as con:
                exit_quote = con.execute(
                    "SELECT observed_at,price FROM reference_prices WHERE asset_id=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
                    (target["asset_id"], target["closed_at"]),
                ).fetchone()
                path = con.execute(
                    """SELECT observed_at,price FROM reference_prices WHERE asset_id=? AND observed_at>? AND observed_at<=?
                       ORDER BY observed_at""",
                    (target["asset_id"], target["closed_at"], now),
                ).fetchall()
                if not exit_quote or not path or float(path[-1]['observed_at'])<float(target['due_at']):
                    con.execute("UPDATE post_exit_targets SET attempts=attempts+1,last_error='PRICE_UNAVAILABLE' WHERE signal_id=? AND horizon_seconds=?", (target["signal_id"], target["horizon_seconds"]))
                    continue
                cutoff_index=next(index for index,item in enumerate(path) if float(item['observed_at'])>=float(target['due_at']))
                path=path[:cutoff_index+1]
                base = float(exit_quote["price"])
                direction = 1.0 if str(target["signal_action"]) in {"BUY", "ENTER_ON_CONFIRMATION", "ENTER_ON_PULLBACK"} else -1.0
                returns = [(float(r["observed_at"]), (float(r["price"]) / base - 1.0) * direction) for r in path] if base else []
                final = returns[-1][1]; peak_t, gain = max(returns, key=lambda x: x[1]); trough_t, loss = min(returns, key=lambda x: x[1])
                classification = self._classify(final, gain, loss)
                evidence = {"exit_price": base, "samples": len(returns), "due_at": target["due_at"], "direction_normalized": True, "signal_action": target["signal_action"]}
                con.execute(
                    """INSERT OR REPLACE INTO post_exit(signal_id,horizon_seconds,classification,return_after_exit,evidence_json,created_at,
                       max_gain_after_exit,max_loss_after_exit,time_to_post_exit_peak,time_to_post_exit_trough) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (target["signal_id"], target["horizon_seconds"], classification, final, json.dumps(evidence), now,
                     gain, loss, peak_t-float(target["closed_at"]), trough_t-float(target["closed_at"])),
                )
                con.execute("UPDATE post_exit_targets SET state='COMPLETE',attempts=attempts+1,last_error=NULL WHERE signal_id=? AND horizon_seconds=?", (target["signal_id"], target["horizon_seconds"]))
                completed += 1
            with connect(self.path, self.settings) as con:
                pending = con.execute("SELECT 1 FROM post_exit_targets WHERE signal_id=? AND state='PENDING'", (target["signal_id"],)).fetchone()
            if not pending:
                current = self.machine.transition_sync(int(target["signal_id"]), "POST_EXIT", "POST_EXIT_WINDOWS_COMPLETE", {"consumer": "active_signal_monitor"})
                current = self.machine.transition_sync(int(target["signal_id"]), "EVALUATED", "EXIT_QUALITY_CLASSIFIED", {"consumer": "active_signal_monitor"})
                self.machine.transition_sync(int(target["signal_id"]), "LEARNED", "EXIT_OUTCOME_AVAILABLE_FOR_TRAINING", {"consumer": "active_signal_monitor"})
        return completed

    def cycle(self, now: float | None = None) -> dict[str, int]:
        now = float(now or time.time())
        changed = snapshots = 0
        with connect(self.path, self.settings) as con:
            rows = con.execute(
                """SELECT s.*,p.event_id,p.asset_id,p.horizon_seconds,p.expected_value,p.expected_magnitude,p.direction_probability,p.risk_score,p.mode,p.evidence_json prediction_evidence
                   FROM signals s JOIN predictions p ON p.id=s.prediction_id WHERE s.state IN ('ACTIVE','UPDATED','PARTIAL_EXIT')"""
            ).fetchall()
        for row in rows:
            with connect(self.path, self.settings) as con:
                snapshot = self._snapshot(con, row, now)
                if snapshot:
                    self._record_snapshot(con, int(row["id"]), now, snapshot)
            if not snapshot:
                continue
            snapshots += 1
            action = snapshot["action"]
            state = str(row["state"])
            if action in {"EXIT", "INVALIDATE"}:
                if state == "ACTIVE":
                    state = self.machine.transition_sync(int(row["id"]), "UPDATED", "MONITOR_ADVERSE_STATE", snapshot)
                if action == "INVALIDATE":
                    self.machine.transition_sync(int(row["id"]), "INVALIDATED", "MONITOR_REMAINING_EV_INVALID", snapshot)
                else:
                    self.machine.transition_sync(int(row["id"]), "EXIT_PENDING", "MONITOR_EXIT_POLICY", snapshot)
                changed += 1
            elif action == "PARTIAL_EXIT":
                if state == "ACTIVE":
                    state = self.machine.transition_sync(int(row["id"]), "UPDATED", "MONITOR_PARTIAL_RISK_DETECTED", snapshot)
                if state == "UPDATED":
                    self.machine.transition_sync(int(row["id"]), "PARTIAL_EXIT", "MONITOR_PARTIAL_RISK_REDUCTION", snapshot)
                    with connect(self.path, self.settings) as con:
                        previous_fraction = float(row["position_fraction"])
                        new_fraction = max(.25, previous_fraction * .5)
                        exited_fraction = max(0.0, previous_fraction - new_fraction)
                        con.execute(
                            """UPDATE signals SET
                               original_position_fraction=COALESCE(original_position_fraction,position_fraction),
                               position_fraction=?,partial_exit_count=partial_exit_count+1,
                               partial_exit_price=?,partial_exit_time=?,
                               partial_realized_result=COALESCE(partial_realized_result,0)+?
                               WHERE id=?""",
                            (new_fraction, snapshot["price"], now, snapshot["return_since_entry"] * exited_fraction, row["id"]),
                        )
                    self.machine.transition_sync(int(row["id"]), "ACTIVE", "MONITOR_PARTIAL_EXIT_FILLED_REMAINDER_ACTIVE", {**snapshot, "remaining_position_fraction": max(.25, float(row["position_fraction"]) * .5)})
                    changed += 1
            elif state == "PARTIAL_EXIT":
                self.machine.transition_sync(int(row["id"]), "ACTIVE", "MONITOR_PARTIAL_EXIT_RECOVERY", snapshot); changed += 1
            elif action == "TRAIL" and state == "ACTIVE":
                self.machine.transition_sync(int(row["id"]), "UPDATED", "MONITOR_TRAILING_PROTECTION", snapshot); changed += 1
        # Inference can be materially slower when the full temporal stack is
        # installed.  A transition records its own wall-clock last_updated_at,
        # so reusing the cycle-start timestamp here can make the pending age
        # negative and leave a zero-confirmation exit pending until another
        # tick.  Complete/recover against a timestamp captured after inference.
        completion_now = max(now, time.time())
        exited = self._complete_pending_exits(completion_now)
        post_exit = self._track_post_exit(completion_now)
        return {"snapshots": snapshots, "changed": changed, "exited": exited, "post_exit": post_exit}

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            # The cycle is bounded SQLite work. Running it inline avoids creating a
            # default executor whose non-daemon worker can outlive service shutdown.
            result = self.cycle()
            if self.metrics:
                self.metrics.inc("active_signal_snapshots", result["snapshots"])
                self.metrics.inc("active_signal_state_changes", result["changed"] + result["exited"])
            if self.settings.telegram_enabled:
                with connect(self.path,self.settings) as con:
                    alerts=con.execute("""SELECT s.id,p.asset_id,p.mode,s.state,s.close_reason FROM signals s JOIN predictions p ON p.id=s.prediction_id WHERE s.state IN ('EXITED','INVALIDATED','UPDATED','PARTIAL_EXIT') AND s.last_updated_at>=?""",(time.time()-max(5.0,self.poll_seconds*2),)).fetchall()
                for item in alerts:await self.telegram.produce({'signal_id':int(item['id']),'asset_id':str(item['asset_id']),'signal_state':str(item['state']),'live_gate':str(item['mode'])=='LIVE','state_changed':True,'summary':str(item['close_reason'] or 'active signal monitor update')})
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                continue
