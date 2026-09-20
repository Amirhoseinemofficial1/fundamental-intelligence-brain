from __future__ import annotations

import asyncio
import io
import json
import time

from openpyxl import Workbook

from v52.intelligence.impact_router import fanout_budget, route_story
from v52.models import StoryCandidate
from v52.storage.control import ControlStore
from v52.storage.sqlite import connect, transaction
from v52.structured.native import EIA_DEFAULT_ROUTES, _next_error_delay
from v52.structured.normalization import (
    normalize_bls_series,
    normalize_cftc_rows,
    normalize_ishares_gold_html,
    normalize_nyfed_rates,
    normalize_spdr_gold_workbook,
    normalize_wgc_workbook,
    parse_treasury_curve_xml,
)


def _candidate(index: int, *, critical: bool = False) -> StoryCandidate:
    if critical:
        return StoryCandidate(
            fingerprint=f"story-{index}",
            title="BREAKING: FOMC emergency unexpected rate cut of 100 basis points",
            summary="Federal Reserve official decision announces immediate emergency action",
            canonical_url="https://www.federalreserve.gov/newsevents/pressreleases/test.htm",
            category="MONETARY_POLICY",
            region="US",
            language="en",
            published_at="2026-09-20T12:00:00Z",
            observed_at="2026-09-20T12:00:01Z",
            materiality=10,
            reasons=["official", "unexpected"],
            source_name="Federal Reserve Official",
            source_url="https://www.federalreserve.gov",
            raw_id=index,
        )
    return StoryCandidate(
        fingerprint=f"story-{index}",
        title=f"Routine corporate calendar update {index}",
        summary="Previously scheduled administrative information",
        canonical_url=f"https://publisher.example/{index}",
        category="GENERAL",
        region="US",
        language="en",
        published_at="2026-09-20T11:00:00Z",
        observed_at="2026-09-20T11:00:01Z",
        materiality=2,
        reasons=["routine"],
        source_name="General Publisher",
        source_url="https://publisher.example",
        raw_id=index,
    )


def test_global_impact_score_and_fanout_tiers() -> None:
    fed = route_story({
        "title": "BREAKING FOMC emergency unexpected rate cut 100 bp",
        "summary": "Federal Reserve official decision is immediate",
        "source_name": "Federal Reserve Official",
        "source_url": "https://www.federalreserve.gov/newsevents/pressreleases/test.htm",
        "materiality": 10,
    })
    apple = route_story({
        "title": "Apple reports quarterly earnings and guidance",
        "summary": "Company results were published after the close",
        "source_name": "Company newsroom",
        "source_url": "https://www.apple.com/newsroom/",
        "materiality": 8,
    })
    assert fed.priority == "P0" and fed.score >= 90
    assert "central_bank" in fed.matched_families
    assert apple.score < fed.score and apple.cross_asset_scope < fed.cross_asset_scope
    assert fanout_budget("P0", 12, 6) == (12, 6)


def test_native_error_backoff_never_shortens_long_poll_interval() -> None:
    assert _next_error_delay(21_600.0, 21_600.0) == 21_600.0
    assert _next_error_delay(60.0, 60.0) == 120.0
    assert _next_error_delay(60.0, 3_000.0) == 3_600.0
    assert fanout_budget("P1", 12, 6) == (5, 4)
    assert fanout_budget("P2", 12, 6) == (2, 2)
    assert fanout_budget("P3", 12, 6) == (0, 0)


def test_p0_bypasses_999_fifo_items_without_advancing_normal_cursor(settings) -> None:
    async def run() -> None:
        store = ControlStore(settings)
        # One transaction keeps this regression proof fast while preserving a
        # real 999-change FIFO backlog in the production schema.
        routine = route_story({
            "title": "Routine corporate calendar update",
            "summary": "Previously scheduled administrative information",
            "source_name": "General Publisher",
            "source_url": "https://publisher.example",
            "materiality": 2,
        })
        now = time.time()
        with connect(store.path, settings) as con:
            transaction(con)
            con.executemany(
                """INSERT INTO stories(
                       id,run_id,fingerprint,title,summary,canonical_url,category,region,language,
                       published_at,first_observed_at,last_observed_at,materiality,reasons_json,
                       source_name,source_url,original_title,original_summary,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(
                    index, "test", f"story-{index}", f"Routine corporate calendar update {index}",
                    "Previously scheduled administrative information",
                    f"https://publisher.example/{index}", "GENERAL", "US", "en",
                    "2026-09-20T11:00:00Z", "2026-09-20T11:00:01Z", "2026-09-20T11:00:01Z",
                    2, '["routine"]', "General Publisher", "https://publisher.example",
                    f"Routine corporate calendar update {index}",
                    "Previously scheduled administrative information", now, now,
                ) for index in range(1, 1000)],
            )
            con.executemany(
                """INSERT INTO story_revisions(
                       id,story_id,revision_no,revision_type,content_hash,title,summary,observed_at,created_at
                   ) VALUES(?,?,1,'INITIAL',?,?,?,?,?)""",
                [(
                    index, index, f"hash-{index}", f"Routine corporate calendar update {index}",
                    "Previously scheduled administrative information", "2026-09-20T11:00:01Z", now,
                ) for index in range(1, 1000)],
            )
            con.executemany(
                "INSERT INTO story_change_feed(id,story_id,revision_no,change_type,created_at) VALUES(?,?,1,'INITIAL',?)",
                [(index, index, now) for index in range(1, 1000)],
            )
            con.executemany(
                """INSERT INTO story_impact_routes(
                       change_id,story_id,score,priority,components_json,routed_at
                   ) VALUES(?,?,?,?,?,?)""",
                [(
                    index, index, routine.score, routine.priority,
                    json.dumps(routine.as_dict(), sort_keys=True), now,
                ) for index in range(1, 1000)],
            )
            con.execute("COMMIT")
        critical = _candidate(1000, critical=True)
        await store.record_story(critical, critical.canonical_url, False, None, 0, None, False)

        normal_first = await store.story_changes_after(0, 1)
        fast = await store.fastlane_candidates(10)
        assert normal_first[0]["fingerprint"] == "story-1"
        assert fast[0]["fingerprint"] == "story-1000"
        assert fast[0]["_impact_priority"] == "P0"
        assert fast[0]["_change_id"] == 1000

        await store.record_fastlane_receipt(
            fast[0]["_change_id"], fast[0]["id"], "P0", fast[0]["_impact_score"], 77,
        )
        assert await store.fastlane_candidates(10) == []
        assert (await store.story_changes_after(0, 1))[0]["fingerprint"] == "story-1"
        receipt = await store.fastlane_receipt(1000)
        assert receipt and receipt["event_id"] == 77 and receipt["refined_at"] is None
        await store.mark_fastlane_refined(1000)
        assert (await store.fastlane_receipt(1000))["refined_at"] is not None

    asyncio.run(run())


def test_bls_series_are_semantically_normalized() -> None:
    rows = [{
        "seriesID": "LNS14000000",
        "data": [
            {"year": "2026", "period": "M08", "value": "4.3", "footnotes": []},
            {"year": "2026", "period": "M07", "value": "4.2", "footnotes": []},
            {"year": "2025", "period": "M13", "value": "4.0", "footnotes": []},
        ],
    }]
    result = normalize_bls_series(rows)
    assert result == [{
        "id": "LNS14000000:2026:08",
        "series_id": "LNS14000000",
        "series_name": "UNEMPLOYMENT_RATE",
        "record_type": "UNEMPLOYMENT_RATE",
        "actual": 4.3,
        "value": 4.3,
        "previous": 4.2,
        "change": 0.09999999999999964,
        "unit": "percent",
        "period": "2026-08",
        "event_time": "2026-08-01T00:00:00Z",
        "footnotes": [],
    }]


def test_cftc_positioning_transformer_produces_level_deltas_and_zscore() -> None:
    rows = []
    for week in range(1, 7):
        rows.append({
            "contract_market_name": "GOLD - COMMODITY EXCHANGE INC.",
            "report_date_as_yyyy_mm_dd": f"2026-08-{week:02d}T00:00:00.000",
            "m_money_positions_long_all": str(100 + week * 10),
            "m_money_positions_short_all": "50",
        })
    result = normalize_cftc_rows(rows, "POSITIONING_DISAGGREGATED")
    by_type = {row["record_type"]: row for row in result}
    assert set(by_type) == {
        "POSITIONING_ZSCORE", "POSITIONING_NET", "POSITIONING_WEEKLY_DELTA",
        "POSITIONING_FOUR_WEEK_DELTA",
    }
    assert by_type["POSITIONING_NET"]["asset_id"] == "XAUUSD"
    assert by_type["POSITIONING_NET"]["participant"] == "MANAGED_MONEY"
    assert by_type["POSITIONING_NET"]["value"] == 110.0
    assert by_type["POSITIONING_WEEKLY_DELTA"]["value"] == 10.0
    assert by_type["POSITIONING_FOUR_WEEK_DELTA"]["value"] == 40.0
    assert by_type["POSITIONING_ZSCORE"]["value"] > 1.0


def test_official_rates_gold_and_eia_contracts() -> None:
    treasury = b"""<QR_BC_CM><G_NEW_DATE><NEW_DATE>09-18-2026</NEW_DATE><BC_2YEAR>3.10</BC_2YEAR><BC_5YEAR>3.20</BC_5YEAR><BC_10YEAR>3.55</BC_10YEAR><BC_30YEAR>4.05</BC_30YEAR></G_NEW_DATE></QR_BC_CM>"""
    curve = {row["record_type"]: row for row in parse_treasury_curve_xml(treasury)}
    assert curve["TREASURY_NOMINAL_YIELD_10Y"]["value"] == 3.55
    assert curve["YIELD_CURVE_2S10S"]["value"] == 0.44999999999999973
    real_treasury = b"""<QR_TIPS_R><G_NEW_DATE><TIPS_CURVE_DATE>18-SEP-26</TIPS_CURVE_DATE><TC_5YEAR>2.10</TC_5YEAR><TC_10YEAR>2.40</TC_10YEAR><TC_30YEAR>2.90</TC_30YEAR></G_NEW_DATE></QR_TIPS_R>"""
    real_curve = {row["record_type"]: row for row in parse_treasury_curve_xml(real_treasury, real=True)}
    assert real_curve["TREASURY_REAL_YIELD_10Y"]["value"] == 2.4

    rates = normalize_nyfed_rates({"refRates": [{
        "effectiveDate": "2026-09-18", "type": "EFFR", "percentRate": "4.33",
        "targetRateFrom": "4.25", "targetRateTo": "4.50", "volumeInBillions": "91",
    }]})
    rate_types = {row["record_type"]: row for row in rates}
    assert rate_types["EFFECTIVE_FED_FUNDS_RATE"]["value"] == 4.33
    assert rate_types["FED_TARGET_RATE_MIDPOINT"]["value"] == 4.375

    gold = normalize_ishares_gold_html(
        b"<html><body>Tonnes in Trust 460.25 as of Sep 18, 2026 Ounces in Trust 14,797,000 as of Sep 18, 2026</body></html>"
    )
    assert gold[0]["record_type"] == "GOLD_ETF_HOLDINGS"
    assert gold[0]["tonnes"] == 460.25 and gold[0]["ounces"] == 14_797_000.0

    assert {row["event_type"] for row in EIA_DEFAULT_ROUTES} == {
        "OIL_INVENTORY", "GASOLINE_INVENTORY", "DISTILLATE_INVENTORY",
        "OIL_PRODUCTION", "REFINERY_UTILIZATION",
    }
    assert all(row["series_id"].startswith("PET.") for row in EIA_DEFAULT_ROUTES)


def test_authenticated_wgc_workbook_normalization_is_bounded() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Official sector"
    sheet.append(["Country", "Change tonnes"])
    sheet.append(["Country A", 2.5])
    sheet.append(["Country B", -0.5])
    stream = io.BytesIO()
    workbook.save(stream)
    rows = normalize_wgc_workbook(stream.getvalue(), "changes", "2026-09-01")
    assert len(rows) == 1
    assert rows[0]["record_type"] == "CENTRAL_BANK_GOLD_PURCHASES"
    assert rows[0]["value"] == 2.0
    assert rows[0]["asset_id"] == "XAUUSD"


def test_spdr_gld_workbook_normalization_selects_latest_holdings() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "US GLD Historical Archive"
    sheet.append([
        "Date", "Closing Price", "Ounces of Gold per Share", "NAV", "Indicative",
        "Spread", "Premium", "Volume", "Total Ounces of Gold in the Trust",
        "Tonnes of Gold", "Total Net Asset Value in the Trust",
    ])
    sheet.append(["17-Sep-2026", 80, .09, 80, 80, 0, 0, 1, 30_000_000, 933.1, 1])
    sheet.append(["18-Sep-2026", 81, .09, 81, 81, 0, 0, 1, 30_100_000, 936.2, 1])
    stream = io.BytesIO()
    workbook.save(stream)
    rows = normalize_spdr_gold_workbook(stream.getvalue())
    assert rows == [{
        "id": "GLD:GOLD_HOLDINGS:2026-09-18",
        "record_type": "GOLD_ETF_HOLDINGS",
        "asset_id": "XAUUSD",
        "fund": "GLD",
        "value": 936.2,
        "tonnes": 936.2,
        "ounces": 30_100_000.0,
        "unit": "metric_tonnes",
        "event_time": "2026-09-18T21:00:00Z",
        "date": "2026-09-18",
    }]
