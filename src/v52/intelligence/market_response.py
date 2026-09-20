from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..metrics import Metrics
from ..timeutil import parse_datetime
from ..storage.sqlite import connect
from .experts import detect_regime
from .model_factory import ArtifactRegistry
from .store import BrainStore
from .reference_market import ActiveAssetWatchlist,BrainReferenceMarketProvider,CexUniverseMarketProvider,DexContractMarketProvider,PublicChartMarketProvider,ReferenceProviderChain
from .regime_governance import RegimeGovernance


PRICE_KEYS = ("price", "lastPrice", "last", "markPrice", "indexPrice", "close", "p")
VOLUME_KEYS = ("volume", "volume24h", "size", "qty", "q")


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError):
        return None


def _find_number(payload: Any, keys: tuple[str, ...], depth: int = 0) -> float | None:
    if depth > 4:
        return None
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                value = _number(payload[key])
                if value is not None:
                    return value
        for value in payload.values():
            found = _find_number(value, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload[:20]:
            found = _find_number(value, keys, depth + 1)
            if found is not None:
                return found
    return None


@dataclass(frozen=True, slots=True)
class PricePoint:
    epoch: float
    price: float
    volume: float | None


class SemanticMarketReader:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Live-capable providers precede the persistent cache for quote refresh;
        # otherwise one cached DEX quote can permanently prevent future polling.
        self.reference = ReferenceProviderChain([DexContractMarketProvider(settings),CexUniverseMarketProvider(settings),PublicChartMarketProvider(settings),BrainReferenceMarketProvider(settings)])

    def _dbs(self) -> list[Path]:
        return sorted(self.settings.data_dir.glob("market/market_*/semantic.sqlite3"))

    @staticmethod
    def _symbol_candidates(symbol: str) -> tuple[str, ...]:
        upper = symbol.upper().replace("/", "").replace("-", "")
        base = upper.removesuffix("USDT").removesuffix("USD")
        return tuple(dict.fromkeys((upper, base, f"{base}USDT", f"{base}-USD", f"{base}/USD")))

    def series_sync(self, market_symbol: str, start_epoch: float, end_epoch: float, limit: int = 5000) -> list[PricePoint]:
        reference = self.reference.get_history(market_symbol, start_epoch, end_epoch, limit)
        if len(reference) >= 2:
            return [PricePoint(x.observed_at, x.price, x.volume) for x in reference]
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_epoch - 15.0))
        end_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end_epoch + 15.0))
        candidates = self._symbol_candidates(market_symbol)
        points: list[PricePoint] = []
        for path in self._dbs():
            try:
                con = sqlite3.connect(path, timeout=2.0)
                con.row_factory = sqlite3.Row
                q = ",".join("?" for _ in candidates)
                rows = con.execute(
                    f"""SELECT observed_at,payload_json FROM events WHERE symbol IN ({q})
                        AND observed_at>=? AND observed_at<=? ORDER BY observed_at LIMIT ?""",
                    (*candidates, start_iso, end_iso, limit),
                ).fetchall()
                con.close()
            except (sqlite3.Error, OSError):
                continue
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                    price = _find_number(payload, PRICE_KEYS)
                    if price is None:
                        continue
                    parsed = parse_datetime(str(row["observed_at"]))
                    if parsed is None:
                        continue
                    points.append(PricePoint(parsed.timestamp(), price, _find_number(payload, VOLUME_KEYS)))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        points.sort(key=lambda x: x.epoch)
        dedup: dict[tuple[int, float], PricePoint] = {(int(p.epoch * 10), p.price): p for p in points}
        return sorted(dedup.values(), key=lambda x: x.epoch)[:limit]

    async def series(self, market_symbol: str, start_epoch: float, end_epoch: float, limit: int = 5000) -> list[PricePoint]:
        return await asyncio.to_thread(self.series_sync, market_symbol, start_epoch, end_epoch, limit)

    @staticmethod
    def measure(points: list[PricePoint], event_epoch: float, due_at: float) -> dict[str, Any] | None:
        before = [p for p in points if p.epoch <= event_epoch + 15.0]
        after = [p for p in points if p.epoch >= event_epoch and p.epoch <= due_at + 30.0]
        if not before or not after:
            return None
        start = min(before, key=lambda p: abs(p.epoch - event_epoch))
        end = min(after, key=lambda p: abs(p.epoch - due_at))
        window = [p for p in points if start.epoch <= p.epoch <= end.epoch]
        if len(window) < 2 or abs(end.epoch - due_at) > 90.0:
            return None
        returns = [p.price / start.price - 1.0 for p in window]
        log_returns = [math.log(window[i].price / window[i - 1].price) for i in range(1, len(window))]
        final_return = end.price / start.price - 1.0
        mfe, mae = max(returns), min(returns)
        mfe_point = window[returns.index(mfe)]
        mae_point = window[returns.index(mae)]
        peak = max(window, key=lambda p: abs(p.price / start.price - 1.0))
        half = max(1, len(window) // 2)
        first_move = window[half].price / start.price - 1.0
        reversal = first_move * final_return < 0
        continuation = first_move * final_return > 0 and abs(final_return) >= abs(first_move)
        volume_values = [p.volume for p in window if p.volume is not None]
        volume_change = volume_values[-1] / volume_values[0] - 1.0 if len(volume_values) >= 2 and volume_values[0] else None
        peak_impact = max(abs(mfe), abs(mae))
        return {
            "start_price": start.price,
            "end_price": end.price,
            "return_value": final_return,
            "log_return": math.log(end.price / start.price),
            "volatility": statistics.pstdev(log_returns) if len(log_returns) > 1 else 0.0,
            "volume": sum(volume_values) if volume_values else None,
            "mfe": mfe,
            "mae": mae,
            "direction": "UP" if final_return > 0 else "DOWN" if final_return < 0 else "FLAT",
            "reversal": reversal,
            "continuation": continuation,
            "time_to_peak_seconds": peak.epoch - event_epoch,
            "time_to_mfe_seconds": mfe_point.epoch - event_epoch,
            "time_to_mae_seconds": mae_point.epoch - event_epoch,
            "volume_change": volume_change,
            "peak_impact": peak_impact,
            "impact_decay": max(0.0, 1.0 - abs(final_return) / max(peak_impact, 1e-12)),
            "observed_samples": len(window),
        }


class MarketResponseService:
    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.brain = BrainStore(settings)
        self.reader = SemanticMarketReader(settings)
        self.artifacts = ArtifactRegistry(settings)
        self.regime_governance = RegimeGovernance(settings)
        self.watchlist=ActiveAssetWatchlist(settings);self.watchlist_provider=self.reader.reference
        self._last_regime: dict[str, float] = {}
        from .barrier_evaluation import SignalEvaluator
        self.signal_evaluator=SignalEvaluator(settings)
        from .learning_insights import LearningInsightEngine
        self.learning_insights=LearningInsightEngine(settings)
        self._last_signal_evaluation=0.0

    async def _process_target(self, target: dict[str, Any]) -> None:
        event_epoch = float(target["event_epoch"])
        due_at = float(target["due_at"])
        # ``pending_responses`` normally enriches the target with the asset's
        # provider symbol.  Keep direct/replayed targets valid as well: older
        # checkpoints and deterministic fixtures only carry ``asset_id``.
        market_symbol = str(target["asset_id"] if str(target.get('asset_class') or '').upper()=='TOKEN' else target.get("market_symbol") or target["asset_id"])
        points = await self.reader.series(market_symbol, event_epoch, due_at)
        response = self.reader.measure(points, event_epoch, due_at)
        if response is None:
            attempts = int(target.get("attempts") or 0) + 1
            dead = time.time() - due_at > 172800 or attempts >= 40
            delay = min(3600.0, 30.0 * (2 ** min(7, attempts)))
            await self.brain.retry_response(int(target["id"]), "market_price_unavailable", delay, dead)
            self.metrics.inc("response_unobservable" if dead else "response_retry")
            return
        await self.brain.complete_response(target, response)
        self.metrics.inc("market_response_completed")
        prices = [p.price for p in points]
        volumes = [p.volume or 0.0 for p in points]
        regime = detect_regime(prices, volumes)
        with connect(self.brain.path,self.settings) as con:
            fv=con.execute("SELECT feature_id,values_json,prediction_time FROM feature_vectors WHERE event_id=? AND asset_id=? AND horizon_seconds=? ORDER BY prediction_time DESC LIMIT 1",(target['event_id'],target['asset_id'],target['horizon_seconds'])).fetchone()
        if fv:
            model_values={str(k):float(v) for k,v in json.loads(fv['values_json']).items() if isinstance(v,(int,float))};governed=self.regime_governance.infer(str(fv['feature_id']),model_values,float(fv['prediction_time']))
            if governed['current_regime']:
                regime['probabilities']=governed['probabilities'];regime['regime']=governed['current_regime'];regime['uncertainty']=governed['uncertainty'];regime['features']['governed_regime_artifacts']=governed['artifact_ids']
        trained=self.artifacts.champion('regime_transition')
        if trained:
            model_values={'trend_score':float(regime['trend_score']),'volatility_score':float(regime['volatility_score']),'change_score':float(regime['change_score']),'liquidity_score':float(regime['liquidity_score'])}
            trained_probability=float(trained.predict(model_values));regime['transition_probability']=.5*float(regime['transition_probability'])+.5*trained_probability
            regime['features']['trained_regime_artifact']=trained.metadata['artifact_id'];regime['features']['trained_transition_probability']=trained_probability
            if regime['transition_probability']>=.65:regime['regime']='REGIME_TRANSITION'
        await self.brain.record_regime(str(target["asset_id"]), regime)
        self.metrics.inc("regime_observations")

    async def run(self, stop_event: asyncio.Event) -> None:
      try:
        while not stop_event.is_set():
            # Collection and target resolution are independent.  A permanent
            # pending-target backlog must never starve the quotes needed to make
            # those very targets observable.
            poll=await asyncio.to_thread(self.watchlist.poll,self.watchlist_provider,float(os.getenv('ACTIVE_ASSET_POLL_SECONDS','60')),8)
            self.metrics.set('active_asset_poll',poll)
            targets = await self.brain.pending_responses(time.time(), 100)
            # Due response targets are latency-sensitive and must not sit behind
            # barrier evaluation or learning-report work on process startup.
            # Those bounded maintenance tasks run only after the current target
            # batch has had an opportunity to resolve.
            if not targets:
                self.metrics.set("brain", await self.brain.summary())
                if time.monotonic()-self._last_signal_evaluation>=30:
                    self.metrics.set('signal_barrier_evaluation',await asyncio.to_thread(self.signal_evaluator.cycle))
                    self.metrics.set('adaptive_learning_report',await self.learning_insights.maybe_report())
                    self._last_signal_evaluation=time.monotonic()
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                break
            for target in targets:
                try:
                    await self._process_target(target)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.metrics.inc("market_response_error")
                    await self.brain.retry_response(int(target["id"]), f"{type(exc).__name__}:{exc}", 60.0)
            if time.monotonic()-self._last_signal_evaluation>=30:
                self.metrics.set('signal_barrier_evaluation',await asyncio.to_thread(self.signal_evaluator.cycle))
                self.metrics.set('adaptive_learning_report',await self.learning_insights.maybe_report())
                self._last_signal_evaluation=time.monotonic()
      finally:
        await self.learning_insights.close()
