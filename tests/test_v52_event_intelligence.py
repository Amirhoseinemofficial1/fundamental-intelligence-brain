from __future__ import annotations

from v52.intelligence.experts import build_votes, decide, detect_regime
from v52.intelligence.taxonomy import classify_story
from v52.news.relevance import materiality_score


def _story(title: str, summary: str = "") -> dict:
    return {
        "id": 1,
        "fingerprint": "fixture",
        "title": title,
        "summary": summary,
        "category": "ECONOMY",
        "materiality": 9,
        "source_name": "Federal Reserve Official",
        "source_url": "https://federalreserve.gov/release",
        "published_at": "2026-08-25T10:00:00Z",
        "first_observed_at": "2026-08-25T10:00:01Z",
    }


def test_taxonomy_maps_macro_event_to_multi_asset_graph() -> None:
    event = classify_story(_story("Federal Reserve cuts interest rate after inflation report"))
    assert event.event_type == "CENTRAL_BANK_DECISION"
    assert event.domain == "MACRO"
    assert {x["asset_id"] for x in event.assets} >= {"DXY", "XAUUSD", "SPX", "BTC"}
    assert any(x["entity_id"] == "FED" for x in event.entities)
    assert event.priority == "P0"
    assert 30 in event.expected_horizons and 86400 in event.expected_horizons


def test_fed_rate_direction_is_asset_specific() -> None:
    event = classify_story(_story("Federal Reserve raises interest rate after FOMC meeting"))
    biases = {x["asset_id"]: x["directional_bias"] for x in event.assets}
    assert biases["DXY"] == "UP"
    assert biases["XAUUSD"] == "DOWN"
    assert biases["SPX"] == "DOWN"
    assert biases["BTC"] == "DOWN"


def test_local_central_bank_decision_does_not_map_us_global_assets() -> None:
    story = _story("Central Bank of Nigeria cuts interest rate")
    story.update({"source_name": "Central Bank of Nigeria", "source_url": "https://www.cbn.gov.ng/rates"})
    event = classify_story(story)
    assert event.event_type == "CENTRAL_BANK_DECISION"
    assert event.assets == ()


def test_short_keywords_do_not_match_inside_unrelated_words() -> None:
    score, reasons = materiality_score("Chair shares awareness campaign", "A fair aviation event", "SPORTS", False)
    joined = " ".join(reasons)
    assert "high:war" not in joined
    assert "medium:ai" not in joined
    assert "high:sec" not in joined
    assert score == 0


def test_regime_detector_is_statistical_and_handles_unknown() -> None:
    assert detect_regime([100.0] * 5)["regime"] == "UNKNOWN"
    rising = [100.0 * (1.002 ** i) for i in range(80)]
    result = detect_regime(rising, [1000 + i * 10 for i in range(80)])
    assert result["regime"].startswith("BULL_TREND")
    assert result["confidence"] > .5
    assert result["features"]["samples"] == 80


def test_no_signal_until_empirical_evidence_is_sufficient() -> None:
    event = {"domain": "MACRO", "event_type": "INFLATION_RELEASE", "novelty": .9, "source_credibility": .95, "importance": .9}
    asset = {"asset_id": "XAUUSD", "asset_class": "COMMODITY", "directional_bias": "UP"}
    votes = build_votes(event, asset, 900, None, [.01, .02, -.005])
    decision = decide(votes, "SHADOW", minimum_samples=30)
    assert decision.action == "NO_TRADE"
    assert decision.evidence_count == 3
    assert decision.calibrated_confidence < .6


def test_sufficient_consistent_evidence_can_qualify_internal_decision() -> None:
    event = {"domain": "ENERGY", "event_type": "ENERGY_SUPPLY_DISRUPTION", "novelty": .9, "source_credibility": .95, "importance": .95}
    asset = {"asset_id": "WTI", "asset_class": "COMMODITY", "directional_bias": "UP"}
    outcomes = [.01 + i * .00001 for i in range(80)]
    votes = build_votes(event, asset, 900, {"regime": "BULL_TREND", "trend_score": 1.5}, outcomes)
    decision = decide(votes, "SHADOW", minimum_samples=30)
    assert decision.action in {"BUY", "ENTER_ON_CONFIRMATION"}
    assert decision.evidence_count == 80
    assert decision.mode == "SHADOW"
