from __future__ import annotations

import math
import io
import re
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup


BLS_SERIES = {
    "CUUR0000SA0": ("CPI", "index_1982_84_100"),
    "LNS14000000": ("UNEMPLOYMENT_RATE", "percent"),
    "CES0000000001": ("NONFARM_PAYROLLS", "thousands_persons"),
    "CES0500000003": ("AVERAGE_HOURLY_EARNINGS", "usd_per_hour"),
    "WPSFD4": ("PPI", "index_1982_100"),
    "JTS000000000000000JOL": ("JOLTS_JOB_OPENINGS", "thousands"),
}


def _number(value: Any) -> float | None:
    try:
        if value in (None, "", ".", "NA", "N/A"):
            return None
        result = float(str(value).replace(",", "").replace("%", ""))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def normalize_bls_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse each BLS series response to its latest released point."""
    normalized: list[dict[str, Any]] = []
    for series in rows:
        series_id = str(series.get("seriesID") or series.get("series_id") or "")
        definition = BLS_SERIES.get(series_id)
        if not definition:
            continue
        points = [
            item for item in (series.get("data") or [])
            if isinstance(item, dict) and re.fullmatch(r"M(?:0[1-9]|1[0-2])", str(item.get("period") or ""))
        ]
        points.sort(
            key=lambda item: (int(item.get("year") or 0), int(str(item.get("period"))[1:])),
            reverse=True,
        )
        if not points:
            continue
        latest = points[0]
        actual = _number(latest.get("value"))
        previous = _number(points[1].get("value")) if len(points) > 1 else None
        if actual is None:
            continue
        year = int(latest["year"])
        month = int(str(latest["period"])[1:])
        record_type, unit = definition
        payload = {
            "id": f"{series_id}:{year}:{month:02d}",
            "series_id": series_id,
            "series_name": record_type,
            "record_type": record_type,
            "actual": actual,
            "value": actual,
            "previous": previous,
            "change": actual - previous if previous is not None else None,
            "unit": unit,
            "period": f"{year}-{month:02d}",
            "event_time": f"{year}-{month:02d}-01T00:00:00Z",
            "footnotes": latest.get("footnotes") or [],
        }
        normalized.append(payload)
    return normalized


_CFTC_ASSETS = (
    (("gold",), "XAUUSD"),
    (("silver",), "XAGUSD"),
    (("copper",), "COPPER"),
    (("crude oil, light sweet", "wti", "crude oil light sweet"), "WTI"),
    (("bitcoin",), "BTC"),
    (("ether", "ethereum"), "ETH"),
    (("u.s. dollar index", "us dollar index"), "DXY"),
    (("euro fx", "eurocurrency"), "EURUSD"),
    (("british pound",), "GBPUSD"),
    (("japanese yen",), "USDJPY"),
    (("s&p 500", "e-mini s&p"), "SPX"),
    (("nasdaq-100", "nasdaq 100"), "NDX"),
)
_PARTICIPANTS = {
    "DEALER": (("dealer_positions_long_all", "dealer_positions_long"),
               ("dealer_positions_short_all", "dealer_positions_short")),
    "ASSET_MANAGER": (("asset_mgr_positions_long_all", "asset_mgr_positions_long"),
                      ("asset_mgr_positions_short_all", "asset_mgr_positions_short")),
    "LEVERAGED_MONEY": (("lev_money_positions_long_all", "lev_money_positions_long"),
                        ("lev_money_positions_short_all", "lev_money_positions_short")),
    "MANAGED_MONEY": (("m_money_positions_long_all", "m_money_positions_long"),
                      ("m_money_positions_short_all", "m_money_positions_short")),
    "PRODUCER_MERCHANT": (("prod_merc_positions_long_all", "prod_merc_positions_long"),
                          ("prod_merc_positions_short_all", "prod_merc_positions_short")),
    "SWAP_DEALER": (("swap_positions_long_all", "swap_positions_long"),
                    ("swap_positions_short_all", "swap__positions_short_all", "swap_positions_short")),
    "NONCOMMERCIAL": (("noncomm_positions_long_all", "ncomm_positions_long_all", "noncomm_positions_long"),
                      ("noncomm_positions_short_all", "ncomm_positions_short_all", "noncomm_positions_short")),
    "COMMERCIAL": (("comm_positions_long_all", "comm_positions_long_all_nocit", "comm_positions_long"),
                   ("comm_positions_short_all", "comm_positions_short")),
    "COMMODITY_INDEX": (("cit_positions_long_all", "cit_positions_long"),
                        ("cit_positions_short_all", "cit_positions_short")),
}


def _first(row: dict[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        value = _number(row.get(name))
        if value is not None:
            return value
    return None


def _cftc_asset(row: dict[str, Any]) -> str | None:
    text = " ".join(str(row.get(key) or "") for key in (
        "contract_market_name", "commodity_name", "commodity", "market_and_exchange_names",
    )).lower()
    for needles, asset in _CFTC_ASSETS:
        if any(needle in text for needle in needles):
            return asset
    return None


def normalize_cftc_rows(rows: list[dict[str, Any]], report_type: str) -> list[dict[str, Any]]:
    """Produce bounded, asset-specific positioning level/delta/percentile/z-score rows."""
    series: dict[tuple[str, str], list[tuple[str, float, float, float, dict[str, Any]]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        asset_id = _cftc_asset(row)
        report_date = str(
            row.get("report_date_as_yyyy_mm_dd") or row.get("report_date") or
            row.get("as_of_date_in_form_yyyymmdd") or ""
        )[:10]
        if not asset_id or not report_date:
            continue
        for participant, (long_names, short_names) in _PARTICIPANTS.items():
            long_value, short_value = _first(row, long_names), _first(row, short_names)
            if long_value is None or short_value is None:
                continue
            series.setdefault((asset_id, participant), []).append(
                (report_date, long_value - short_value, long_value, short_value, row)
            )
    output: list[dict[str, Any]] = []
    for (asset_id, participant), values in series.items():
        values.sort(key=lambda item: item[0])
        # Socrata can return duplicate exchange rows. Keep one point per date.
        dated = {item[0]: item for item in values}
        history = [dated[key] for key in sorted(dated)][-104:]
        if not history:
            continue
        date, net, long_value, short_value, raw = history[-1]
        nets = [item[1] for item in history]
        mean = statistics.mean(nets)
        deviation = statistics.pstdev(nets)
        z_score = (net - mean) / deviation if deviation > 0 else 0.0
        percentile = sum(value <= net for value in nets) / len(nets)
        weekly_delta = net - nets[-2] if len(nets) >= 2 else 0.0
        four_week_delta = net - nets[-5] if len(nets) >= 5 else weekly_delta
        base = {
            "source_record_type": report_type,
            "asset_id": asset_id,
            "participant": participant,
            "contract_market_name": raw.get("contract_market_name"),
            "event_time": f"{date}T20:00:00Z",
            "report_date": date,
            "z_score": z_score,
            "net_position": net,
            "long_position": long_value,
            "short_position": short_value,
            "weekly_delta": weekly_delta,
            "four_week_delta": four_week_delta,
            "percentile": percentile,
            "history_points": len(nets),
        }
        for observation_type, value, unit in (
            ("POSITIONING_ZSCORE", z_score, "zscore"),
            ("POSITIONING_NET", net, "contracts"),
            ("POSITIONING_WEEKLY_DELTA", weekly_delta, "contracts"),
            ("POSITIONING_FOUR_WEEK_DELTA", four_week_delta, "contracts"),
        ):
            output.append({
                **base,
                "id": f"{report_type}:{asset_id}:{participant}:{observation_type}:{date}",
                "record_type": observation_type,
                "value": value,
                "unit": unit,
            })
    return output


def parse_treasury_curve_xml(body: bytes, *, real: bool = False) -> list[dict[str, Any]]:
    """Read the latest valid official Treasury nominal or real curve."""
    root = ET.fromstring(body)
    observations: list[tuple[str, dict[str, str]]] = []
    for day in root.iter():
        if day.tag.rsplit("}", 1)[-1] != "G_NEW_DATE":
            continue
        values = {
            child.tag.rsplit("}", 1)[-1]: (child.text or "").strip()
            for child in day.iter()
        }
        raw_date = (
            values.get("NEW_DATE") or values.get("BID_CURVE_DATE") or
            values.get("TIPS_CURVE_DATE") or ""
        )
        raw_date = raw_date.replace(" MARKET CLOSED", "").strip()
        parsed = None
        for fmt in ("%m-%d-%Y", "%d-%b-%y", "%m/%d/%Y"):
            try:
                parsed = datetime.strptime(raw_date, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if parsed:
            observations.append((parsed.date().isoformat(), values))
    if not observations:
        return []
    date, values = max(observations, key=lambda item: item[0])
    prefix = "TC_" if real else "BC_"
    tenors = {"2Y":"2YEAR", "5Y":"5YEAR", "10Y":"10YEAR", "30Y":"30YEAR"}
    output: list[dict[str, Any]] = []
    curve: dict[str, float] = {}
    for tenor, suffix in tenors.items():
        value = _number(values.get(prefix + suffix))
        if value is None:
            continue
        curve[tenor] = value
        kind = "REAL" if real else "NOMINAL"
        output.append({
            "id": f"UST:{kind}:{tenor}:{date}",
            "record_type": f"TREASURY_{kind}_YIELD_{tenor}",
            "tenor": tenor,
            "value": value,
            "unit": "percent",
            "event_time": f"{date}T20:00:00Z",
            "date": date,
        })
    if not real:
        for name, left, right in (
            ("YIELD_CURVE_2S10S", "2Y", "10Y"),
            ("YIELD_CURVE_5S30S", "5Y", "30Y"),
        ):
            if left in curve and right in curve:
                output.append({
                    "id": f"UST:{name}:{date}",
                    "record_type": name,
                    "value": curve[right] - curve[left],
                    "unit": "percentage_points",
                    "event_time": f"{date}T20:00:00Z",
                    "date": date,
                    "short_tenor": left,
                    "long_tenor": right,
                })
    return output


def normalize_nyfed_rates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("refRates") if isinstance(payload, dict) else []
    output: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rate_type = str(row.get("type") or "").upper()
        value = _number(row.get("percentRate"))
        date = str(row.get("effectiveDate") or "")[:10]
        if not rate_type or value is None or not date:
            continue
        record_type = {
            "EFFR": "EFFECTIVE_FED_FUNDS_RATE",
            "SOFR": "SOFR",
            "OBFR": "OBFR",
            "TGCR": "TGCR",
            "BGCR": "BGCR",
        }.get(rate_type, rate_type)
        item = {
            "id": f"NYFED:{rate_type}:{date}",
            "record_type": record_type,
            "value": value,
            "unit": "percent",
            "event_time": f"{date}T12:00:00Z",
            "date": date,
            "volume_billions": _number(row.get("volumeInBillions")),
            "revision_indicator": row.get("revisionIndicator") or "",
        }
        low = _number(row.get("targetRateFrom"))
        high = _number(row.get("targetRateTo"))
        if low is not None and high is not None:
            item.update({"target_rate_lower": low, "target_rate_upper": high})
            output.append({
                "id": f"NYFED:FED_TARGET:{date}",
                "record_type": "FED_TARGET_RATE_MIDPOINT",
                "value": (low + high) / 2.0,
                "lower": low,
                "upper": high,
                "unit": "percent",
                "event_time": f"{date}T12:00:00Z",
                "date": date,
            })
        output.append(item)
    # APIs return newest first; a single current row per rate is enough.
    latest: dict[str, dict[str, Any]] = {}
    for item in output:
        latest.setdefault(str(item["record_type"]), item)
    return list(latest.values())


def normalize_ishares_gold_html(body: bytes) -> list[dict[str, Any]]:
    text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
    tonnes = re.search(
        r"Tonnes\s+in\s+Trust\s+([0-9,]+(?:\.[0-9]+)?)\s+as\s+of\s+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
        text, flags=re.I,
    )
    ounces = re.search(
        r"Ounces\s+in\s+Trust\s+([0-9,]+(?:\.[0-9]+)?)\s+as\s+of\s+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
        text, flags=re.I,
    )
    if not tonnes:
        return []
    parsed_date = None
    for date_format in ("%b %d, %Y", "%B %d, %Y"):
        try:
            parsed_date = datetime.strptime(tonnes.group(2), date_format).date()
            break
        except ValueError:
            continue
    if parsed_date is None:
        return []
    date = parsed_date.isoformat()
    value = _number(tonnes.group(1))
    if value is None:
        return []
    return [{
        "id": f"IAU:GOLD_HOLDINGS:{date}",
        "record_type": "GOLD_ETF_HOLDINGS",
        "asset_id": "XAUUSD",
        "fund": "IAU",
        "value": value,
        "tonnes": value,
        "ounces": _number(ounces.group(1)) if ounces else None,
        "unit": "metric_tonnes",
        "event_time": f"{date}T21:00:00Z",
        "date": date,
    }]


def normalize_spdr_gold_workbook(body: bytes) -> list[dict[str, Any]]:
    """Read the latest GLD ounces/tonnes row from SPDR's official archive."""
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(body), read_only=True, data_only=True)
    candidates: list[tuple[datetime, float, float | None]] = []
    for sheet in workbook.worksheets:
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            continue
        labels = [str(value or "").strip().lower() for value in header]
        try:
            date_index = labels.index("date")
            tonnes_index = labels.index("tonnes of gold")
        except ValueError:
            continue
        ounces_index = next(
            (index for index, label in enumerate(labels) if label == "total ounces of gold in the trust"),
            None,
        )
        for row in rows:
            if date_index >= len(row) or tonnes_index >= len(row):
                continue
            raw_date, tonnes = row[date_index], _number(row[tonnes_index])
            if tonnes is None:
                continue
            parsed = raw_date if isinstance(raw_date, datetime) else None
            if parsed is None:
                for date_format in ("%d-%b-%Y", "%b %d, %Y", "%Y-%m-%d"):
                    try:
                        parsed = datetime.strptime(str(raw_date), date_format)
                        break
                    except ValueError:
                        continue
            if parsed is None:
                continue
            ounces = _number(row[ounces_index]) if ounces_index is not None and ounces_index < len(row) else None
            candidates.append((parsed, tonnes, ounces))
    if not candidates:
        return []
    parsed, tonnes, ounces = max(candidates, key=lambda item: item[0])
    date = parsed.date().isoformat()
    return [{
        "id": f"GLD:GOLD_HOLDINGS:{date}",
        "record_type": "GOLD_ETF_HOLDINGS",
        "asset_id": "XAUUSD",
        "fund": "GLD",
        "value": tonnes,
        "tonnes": tonnes,
        "ounces": ounces,
        "unit": "metric_tonnes",
        "event_time": f"{date}T21:00:00Z",
        "date": date,
    }]


def normalize_wgc_workbook(body: bytes, dataset: str, as_of: str) -> list[dict[str, Any]]:
    """Extract a bounded global official-sector measure from a WGC workbook."""
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(body), read_only=True, data_only=True)
    candidates: list[tuple[str, float]] = []
    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(min_row=1, max_row=5000, values_only=True))
        if not rows:
            continue
        for header_index, header in enumerate(rows[:40]):
            labels = [str(value or "").strip() for value in header]
            country_index = next(
                (index for index, label in enumerate(labels) if "country" in label.lower()),
                None,
            )
            if country_index is None:
                continue
            numeric_columns: list[int] = []
            for index, label in enumerate(labels):
                if index == country_index:
                    continue
                lowered = label.lower()
                if any(token in lowered for token in ("tonnes", "holding", "change", "q1", "q2", "q3", "q4", "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")):
                    numeric_columns.append(index)
            if not numeric_columns:
                numeric_columns = list(range(country_index + 1, len(labels)))
            for column in reversed(numeric_columns):
                world_value = None
                values = []
                for row in rows[header_index + 1:]:
                    country = str(row[country_index] or "").strip() if country_index < len(row) else ""
                    value = _number(row[column]) if column < len(row) else None
                    if not country or value is None:
                        continue
                    if country.lower() in {"world", "total", "global total"}:
                        world_value = value
                        break
                    if not any(token in country.lower() for token in ("source", "note", "subtotal")):
                        values.append(value)
                total = world_value if world_value is not None else sum(values)
                if values or world_value is not None:
                    candidates.append((f"{sheet.title}:{labels[column]}", total))
                    break
            break
    if not candidates:
        return []
    # Prefer the most information-rich sheet while emitting one bounded global row.
    label, value = max(candidates, key=lambda item: abs(item[1]))
    record_type = (
        "CENTRAL_BANK_GOLD_PURCHASES"
        if dataset == "changes" else "CENTRAL_BANK_GOLD_RESERVES"
    )
    return [{
        "id": f"WGC:{dataset}:{as_of}",
        "record_type": record_type,
        "asset_id": "XAUUSD",
        "value": value,
        "unit": "metric_tonnes",
        "event_time": f"{as_of}T00:00:00Z",
        "date": as_of,
        "worksheet_measure": label,
        "dataset": dataset,
    }]
