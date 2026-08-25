from __future__ import annotations

from v52.alerts.telegram import render_batch


def test_exit_and_daily_report_formats_are_atomic_and_bounded() -> None:
    exit_text = render_batch([({}, {
        "kind": "signal_exit", "signal_id": 9, "asset_id": "XAUUSD", "previous_action": "BUY",
        "event_title": "Central bank decision", "close_reason": "HORIZON_OUTCOME_ADVERSE",
        "return_value": -.002, "mfe": .003, "mae": -.004,
    })])
    assert "SIGNAL EXIT" in exit_text and "XAUUSD" in exit_text and len(exit_text) < 4000
    report_text = render_batch([({}, {
        "kind": "daily_learning_report", "date": "2026-08-25", "mode": "OBSERVATION",
        "summary": {"counts": {"events": 12, "market_responses": 8, "knowledge": 2, "signals": 20}},
        "lifecycle": {"closed": 1, "missed": 2}, "qualification": {"qualified": False, "brier": .24, "calibration_error": .1},
        "drift": {"detected": False},
    })])
    assert "DAILY AI LEARNING REPORT" in report_text and "OBSERVATION" in report_text and len(report_text) < 4000


def test_all_ten_intelligence_message_classes_render() -> None:
    payloads = [
        {"kind":"intelligence_signal","asset_id":"BTC","action":"BUY","event_title":"ETF decision","horizon_seconds":300,"probability":.7,"calibrated_confidence":.65,"uncertainty":.2,"risk_score":.3},
        {"kind":"active_update","asset_id":"BTC","summary":"Probability changed"},
        {"kind":"signal_exit","asset_id":"BTC","previous_action":"BUY","close_reason":"target","return_value":.01,"mfe":.02,"mae":-.003},
        {"kind":"invalidation","asset_id":"BTC","summary":"Official correction"},
        {"kind":"pump_warning","asset_id":"TOKEN","summary":"OI and flow acceleration"},
        {"kind":"dump_warning","asset_id":"TOKEN","summary":"Unlock and exchange inflow"},
        {"kind":"fundamental_intelligence","asset_id":"XAUUSD","summary":"Real-yield context changed"},
        {"kind":"validated_learning_update","asset_id":"WTI","summary":"Shadow model promoted"},
        {"kind":"daily_learning_report","date":"2026-08-25","mode":"SHADOW","summary":{"counts":{}},"lifecycle":{},"qualification":{},"drift":{}},
        {"kind":"critical_system_warning","asset_id":"SYSTEM","summary":"Disk guard active"},
    ]
    texts=[render_batch([({},payload)]) for payload in payloads]
    assert len(texts)==10 and all(text and len(text)<4000 for text in texts)
