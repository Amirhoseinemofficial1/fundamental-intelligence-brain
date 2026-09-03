from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

from .types import ExpertVote, SignalDecision


@dataclass(frozen=True, slots=True)
class Specialist:
    name: str
    domains: frozenset[str]
    asset_classes: frozenset[str]
    event_prefixes: tuple[str, ...] = ()
    base_weight: float = 1.0


SPECIALISTS: tuple[Specialist, ...] = (
    Specialist("macro", frozenset({"MACRO", "ENERGY"}), frozenset({"FOREX", "INDEX", "COMMODITY", "CRYPTO", "TOKEN"}), base_weight=1.2),
    Specialist("central_bank", frozenset({"MACRO", "FOREX"}), frozenset({"FOREX", "INDEX", "COMMODITY", "CRYPTO", "TOKEN"}), ("CENTRAL_BANK", "LIQUIDITY", "FX_", "REAL_YIELD", "INFLATION", "EMPLOYMENT"), 1.3),
    Specialist("gold", frozenset({"MACRO", "GEOPOLITICAL", "COMMODITIES"}), frozenset({"COMMODITY"}), base_weight=1.25),
    Specialist("oil", frozenset({"ENERGY", "GEOPOLITICAL"}), frozenset({"COMMODITY"}), ("OPEC", "OIL_", "ENERGY_", "WAR_", "SANCTIONS"), 1.3),
    Specialist("forex", frozenset({"MACRO", "FOREX", "GEOPOLITICAL"}), frozenset({"FOREX", "INDEX"}), base_weight=1.15),
    Specialist("equity", frozenset({"EQUITIES", "MACRO", "REGULATION"}), frozenset({"EQUITY", "INDEX"}), base_weight=1.1),
    Specialist("crypto", frozenset({"CRYPTO", "MACRO", "REGULATION"}), frozenset({"CRYPTO", "TOKEN"}), base_weight=1.15),
    Specialist("token", frozenset({"CRYPTO"}), frozenset({"TOKEN"}), ("TOKEN_", "EXCHANGE_LIST", "EXCHANGE_DELIST", "PROTOCOL_"), 1.2),
    Specialist("listing", frozenset({"CRYPTO", "EQUITIES"}), frozenset({"TOKEN", "CRYPTO", "EQUITY"}), ("EXCHANGE_LIST", "IPO", "DIRECT_LIST"), 1.15),
    Specialist("etf", frozenset({"CRYPTO", "EQUITIES", "REGULATION"}), frozenset({"CRYPTO", "TOKEN", "EQUITY", "INDEX"}), ("ETF_",), 1.25),
    Specialist("regulation", frozenset({"REGULATION", "CRYPTO", "EQUITIES"}), frozenset({"CRYPTO", "TOKEN", "EQUITY", "INDEX"}), ("REGULATORY", "SANCTIONS", "ETF_"), 1.15),
    Specialist("geopolitical", frozenset({"GEOPOLITICAL"}), frozenset({"COMMODITY", "FOREX", "INDEX", "CRYPTO"}), base_weight=1.2),
    Specialist("energy", frozenset({"ENERGY", "GEOPOLITICAL"}), frozenset({"COMMODITY", "EQUITY", "INDEX"}), base_weight=1.15),
    Specialist("news_novelty", frozenset({"MACRO", "FOREX", "GEOPOLITICAL", "ENERGY", "EQUITIES", "CRYPTO", "REGULATION", "GENERAL"}), frozenset({"CRYPTO", "TOKEN", "COMMODITY", "FOREX", "INDEX", "EQUITY"}), base_weight=.7),
    Specialist("onchain", frozenset({"CRYPTO"}), frozenset({"CRYPTO", "TOKEN"}), ("WHALE_", "TOKEN_SUPPLY", "PROTOCOL_", "EXCHANGE_FLOW", "DEX_"), .85),
    Specialist("derivatives", frozenset({"CRYPTO", "EQUITIES"}), frozenset({"CRYPTO", "TOKEN", "EQUITY", "INDEX"}), ("LIQUIDATION", "OPEN_INTEREST", "FUNDING", "BASIS"), .95),
    Specialist("whale", frozenset({"CRYPTO"}), frozenset({"CRYPTO", "TOKEN"}), ("WHALE_", "EXCHANGE_FLOW"), .9),
    Specialist("liquidity", frozenset({"MACRO", "CRYPTO", "EQUITIES"}), frozenset({"CRYPTO", "TOKEN", "INDEX", "FOREX"}), ("LIQUIDITY", "LIQUIDATION", "OPEN_INTEREST", "FUNDING", "DEX_", "EXCHANGE_FLOW"), 1.0),
    Specialist("regime", frozenset({"MACRO", "FOREX", "GEOPOLITICAL", "ENERGY", "EQUITIES", "CRYPTO", "REGULATION", "GENERAL"}), frozenset({"CRYPTO", "TOKEN", "COMMODITY", "FOREX", "INDEX", "EQUITY"}), base_weight=.8),
    Specialist("pump_risk", frozenset({"CRYPTO"}), frozenset({"CRYPTO", "TOKEN"}), ("EXCHANGE_LIST", "WHALE_", "LIQUIDATION"), .8),
    Specialist("dump_risk", frozenset({"CRYPTO"}), frozenset({"CRYPTO", "TOKEN"}), ("EXCHANGE_DELIST", "PROTOCOL_EXPLOIT", "EXCHANGE_HACK", "TOKEN_UNLOCK"), 1.0),
)


def _logit(probability: float) -> float:
    p = max(.001, min(.999, probability))
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-25.0, min(25.0, value))))


def detect_regime(prices: list[float], volumes: list[float] | None = None, cross_returns:list[float]|None=None, breadth:float|None=None, risk_on_score:float|None=None) -> dict[str, Any]:
    clean = [float(x) for x in prices if x is not None and float(x) > 0]
    if len(clean) < 12:
        return {"regime": "UNKNOWN", "probabilities":{"UNKNOWN":1.0},"uncertainty":1.0,"transition_probability":1.0,"trend_score": 0.0, "volatility_score": 1.0, "change_score": 1.0, "clustering_score":0.0,"correlation_state":0.0,"breadth_score":0.5,"liquidity_score": 0.0,"risk_on_score":0.0,"confidence": 0.0, "features": {"samples": len(clean)}}
    returns = [math.log(clean[i] / clean[i - 1]) for i in range(1, len(clean))]
    short = returns[-min(12, len(returns)):]
    long = returns[-min(60, len(returns)):]
    trend = sum(short) / max(statistics.pstdev(short), 1e-8) / math.sqrt(len(short))
    vol = statistics.pstdev(long) * math.sqrt(max(1, len(long)))
    half = max(3, len(long) // 2)
    older, newer = long[:-half], long[-half:]
    change = abs((statistics.mean(newer) if newer else 0.0) - (statistics.mean(older) if older else 0.0)) / max(statistics.pstdev(long), 1e-8)
    volume_values = [float(x) for x in (volumes or []) if x is not None and float(x) >= 0]
    liquidity = 0.5
    if len(volume_values) >= 6:
        median = statistics.median(volume_values)
        liquidity = min(1.0, statistics.mean(volume_values[-3:]) / max(median * 2.0, 1e-9))
    clustering=min(1.0,abs(statistics.mean(short))/max(statistics.pstdev(long),1e-8))
    cross=[float(x) for x in (cross_returns or []) if x is not None and math.isfinite(float(x))]
    correlation_state=(sum(1 if x*returns[-1]>=0 else -1 for x in cross)/len(cross)) if cross else 0.0
    breadth_score=max(0.0,min(1.0,float(breadth if breadth is not None else .5)))
    risk_on=max(-1.0,min(1.0,float(risk_on_score if risk_on_score is not None else (breadth_score-.5)*2)))
    high_vol = vol > .035
    # A compact statistical ensemble: trend, volatility, mean-shift/change-point,
    # and liquidity each vote continuously. Softmax retains ambiguity instead of
    # forcing one brittle threshold label.
    scores={
        'BULL_TREND':1.2*trend-8*vol-.4*change+.3*clustering+.25*correlation_state+.3*risk_on,
        'BEAR_TREND':-1.2*trend-8*vol-.4*change+.3*clustering-.25*correlation_state-.3*risk_on,
        'SIDEWAYS_LOW_VOL':1.4-abs(trend)-12*vol-.3*change-.2*clustering,
        'SIDEWAYS_HIGH_VOL':18*vol-abs(trend)-.2*change,
        'REGIME_TRANSITION':1.1*change+8*vol-.3*abs(trend),
        'PANIC':-trend+1.2*change+14*vol+(1-liquidity)+(1-breadth_score)-risk_on,
        'SQUEEZE':trend+1.2*change+14*vol+(1-liquidity)+breadth_score+risk_on,
    }
    maximum=max(scores.values());normalizer=sum(math.exp(value-maximum) for value in scores.values());probabilities={name:math.exp(value-maximum)/normalizer for name,value in scores.items()}
    regime=max(probabilities,key=probabilities.get);confidence=float(probabilities[regime]);entropy=-sum(p*math.log(max(p,1e-12)) for p in probabilities.values())/math.log(len(probabilities));transition=min(1.0,max(probabilities['REGIME_TRANSITION'],_sigmoid(change-1.2)))
    return {
        "regime": regime,
        "probabilities":probabilities,
        "uncertainty":entropy,
        "transition_probability":transition,
        "trend_score": trend,
        "volatility_score": vol,
        "change_score": change,
        "clustering_score":clustering,
        "correlation_state":correlation_state,
        "breadth_score":breadth_score,
        "liquidity_score": liquidity,
        "risk_on_score":risk_on,
        "confidence": confidence,
        "features": {"samples": len(clean), "last_return": returns[-1], "mean_return": statistics.mean(long),"ensemble":"trend+volatility+change_point+clustering+correlation+breadth+liquidity+risk_on_off","high_volatility":high_vol},
    }


def _prior_probability(direction: str) -> float:
    return {"UP": .62, "DOWN": .38, "MIXED": .5}.get(direction, .5)


def build_votes(
    event: dict[str, Any],
    asset: dict[str, Any],
    horizon: int,
    regime: dict[str, Any] | None,
    outcomes: list[float],
) -> list[ExpertVote]:
    domain = str(event["domain"])
    event_type = str(event["event_type"])
    asset_class = str(asset["asset_class"])
    asset_id = str(asset["asset_id"])
    direction = str(asset.get("directional_bias") or "MIXED")
    base = _prior_probability(direction)
    novelty = float(event.get("novelty") or 0.0)
    credibility = float(event.get("source_credibility") or .5)
    importance = float(event.get("importance") or 0.0)
    votes: list[ExpertVote] = []
    for spec in SPECIALISTS:
        prefix_ok = not spec.event_prefixes or any(event_type.startswith(prefix) for prefix in spec.event_prefixes)
        if domain not in spec.domains or asset_class not in spec.asset_classes or not prefix_ok:
            continue
        probability = base
        rationale = [f"domain={domain}", f"event={event_type}"]
        if spec.name == "news_novelty":
            probability = .5 + (base - .5) * novelty * credibility
            rationale.extend((f"novelty={novelty:.2f}", f"credibility={credibility:.2f}"))
        if spec.name == "regime" and regime:
            trend = float(regime.get("trend_score") or 0.0)
            probability = _sigmoid(_logit(base) + max(-.8, min(.8, trend * .25)))
            rationale.append(f"regime={regime.get('regime')}")
        if spec.name == "pump_risk":
            probability = min(.82, .52 + .22 * importance + .12 * novelty)
            rationale.append("pump-risk-not-directional-certainty")
        if spec.name == "dump_risk":
            probability = max(.08, .48 - .28 * importance - .10 * novelty)
            rationale.append("downside-risk")
        uncertainty = max(.18, .58 - .18 * credibility - .14 * novelty - .08 * importance)
        votes.append(ExpertVote(spec.name, asset_id, horizon, probability, .0025 * importance, uncertainty, spec.base_weight, tuple(rationale)))
    if outcomes:
        positive = sum(x > 0 for x in outcomes)
        n = len(outcomes)
        posterior = (positive + 2.0) / (n + 4.0)
        magnitude = statistics.mean(abs(x) for x in outcomes)
        uncertainty = math.sqrt(posterior * (1.0 - posterior) / (n + 5.0))
        weight = min(2.0, .4 + math.log1p(n) / 2.5)
        votes.append(ExpertVote("historical_impact", asset_id, horizon, posterior, magnitude, uncertainty, weight, (f"samples={n}", "beta-binomial-posterior")))
    return votes


def decide(votes: list[ExpertVote], mode: str, minimum_samples: int = 30) -> SignalDecision:
    if not votes:
        return SignalDecision("NO_TRADE", "UNKNOWN", 0, .5, .0, .0, 1.0, 0.0, 1.0, "No entry", "No position", "No target", "No position", 0.0, 0, mode, ("no_expert_votes",))
    asset_id, horizon = votes[0].asset_id, votes[0].horizon_seconds
    total_weight = sum(max(.01, v.weight) for v in votes)
    combined_logit = sum(_logit(v.direction_probability) * max(.01, v.weight) for v in votes) / total_weight
    probability = _sigmoid(combined_logit)
    expected_magnitude = sum(v.expected_magnitude * v.weight for v in votes) / total_weight
    uncertainty = min(1.0, sum(v.uncertainty * v.weight for v in votes) / total_weight)
    directions = [1 if v.direction_probability >= .55 else -1 if v.direction_probability <= .45 else 0 for v in votes]
    nonzero = [x for x in directions if x]
    agreement = abs(sum(nonzero)) / len(nonzero) if nonzero else 0.0
    historical = next((v for v in votes if v.expert == "historical_impact"), None)
    samples = 0
    if historical:
        for reason in historical.rationale:
            if reason.startswith("samples="):
                samples = int(reason.split("=", 1)[1])
    # Confidence is explicitly shrunk until empirical evidence exists.
    evidence_factor = min(1.0, samples / max(1, minimum_samples))
    calibrated = .5 + abs(probability - .5) * evidence_factor * max(.0, 1.0 - uncertainty)
    calibrated = max(.0, min(.99, calibrated))
    edge = abs(probability - .5) * 2.0
    risk = min(1.0, uncertainty * .6 + (1.0 - agreement) * .25 + (1.0 - evidence_factor) * .15)
    expected_value = expected_magnitude * edge * (1.0 - risk)
    reasons = [f"experts={len(votes)}", f"agreement={agreement:.2f}", f"samples={samples}", f"uncertainty={uncertainty:.2f}"]
    qualified = samples >= minimum_samples and uncertainty <= .30 and agreement >= .65 and calibrated >= .58
    if not qualified:
        action = "NO_TRADE"
        reasons.append("qualification_insufficient")
    elif probability >= .66:
        action = "BUY"
    elif probability <= .34:
        action = "SELL_SHORT"
    elif probability >= .58:
        action = "ENTER_ON_CONFIRMATION"
    elif probability <= .42:
        action = "ENTER_ON_PULLBACK"
    else:
        action = "WAIT"
    volatility_buffer = max(.003, expected_magnitude * 1.5)
    entry = "Enter only after price confirms the event direction; use limit execution and bounded slippage."
    invalidation = f"Invalidate if adverse move exceeds {volatility_buffer:.4%} or event is corrected/contradicted."
    target = f"Initial target uses empirical expected magnitude {expected_magnitude:.4%}; scale only with liquidity confirmation."
    exit_logic = "Exit on probability decay, regime transition, adverse evidence, liquidity deterioration, target, or invalidation."
    return SignalDecision(action, asset_id, horizon, probability, calibrated, expected_magnitude, uncertainty, expected_value, risk, entry, invalidation, target, exit_logic, agreement, samples, mode, tuple(reasons))


def brier_score(probabilities: list[float], labels: list[int]) -> float:
    if not probabilities or len(probabilities) != len(labels):
        return math.inf
    return sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / len(probabilities)


def calibration_error(probabilities: list[float], labels: list[int], bins: int = 10) -> float:
    if not probabilities or len(probabilities) != len(labels):
        return math.inf
    total = len(probabilities)
    error = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        selected = [(p, y) for p, y in zip(probabilities, labels) if low <= p < high or (index == bins - 1 and p == 1.0)]
        if selected:
            confidence = statistics.mean(x[0] for x in selected)
            accuracy = statistics.mean(x[1] for x in selected)
            error += len(selected) / total * abs(confidence - accuracy)
    return error


def purged_walk_forward(rows: list[tuple[float, list[float], int]], folds: int = 4, embargo: int = 1) -> list[tuple[list[int], list[int]]]:
    """Return chronological train/validation indexes with purge and embargo gaps."""
    ordered = sorted(range(len(rows)), key=lambda i: rows[i][0])
    if len(ordered) < max(12, folds * 3):
        return []
    fold_size = max(2, len(ordered) // (folds + 1))
    splits: list[tuple[list[int], list[int]]] = []
    for fold in range(1, folds + 1):
        val_start = fold * fold_size
        val_end = min(len(ordered), val_start + fold_size)
        train_end = max(0, val_start - embargo)
        validation_start = min(val_end, val_start + embargo)
        train, validation = ordered[:train_end], ordered[validation_start:val_end]
        if train and validation:
            splits.append((train, validation))
    return splits
