#!/usr/bin/env python3
"""Read-only live contract check for V59's no-key official data plane.

The script never prints credentials. EIA is checked only when EIA_API_KEY is
configured; WGC remains an authenticated connector and is reported explicitly.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from v52.structured.normalization import (
    normalize_bls_series,
    normalize_cftc_rows,
    normalize_ishares_gold_html,
    normalize_nyfed_rates,
    normalize_spdr_gold_workbook,
    parse_treasury_curve_xml,
)
from v52.structured.native import EIA_DEFAULT_ROUTES


USER_AGENT = "V59-data-plane-validator/59.0 (+operator-read-only-check)"


def fetch(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if int(response.status) != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return response.read(16_000_000)


def json_fetch(url: str, **kwargs: Any) -> Any:
    return json.loads(fetch(url, **kwargs))


def checked(name: str, fn: Callable[[], dict[str, Any]], *, required: bool = True) -> dict[str, Any]:
    try:
        detail = fn()
        return {"name": name, "ok": True, **detail}
    except Exception as exc:
        return {"name": name, "ok": not required, "available": False,
                "required": required, "error": f"{type(exc).__name__}:{exc}"[:500]}


def treasury(real: bool) -> dict[str, Any]:
    filename = "real_yield.xml" if real else "yield.xml"
    rows = parse_treasury_curve_xml(
        fetch(f"https://home.treasury.gov/sites/default/files/interest-rates/{filename}"),
        real=real,
    )
    if not rows:
        raise RuntimeError("no normalized rows")
    return {"rows": len(rows), "latest": max(str(row["date"]) for row in rows)}


def nyfed(rate: str, secured: bool) -> dict[str, Any]:
    group = "secured" if secured else "unsecured"
    rows = normalize_nyfed_rates(json_fetch(
        f"https://markets.newyorkfed.org/api/rates/{group}/{rate}/last/5.json"
    ))
    if not rows:
        raise RuntimeError("no normalized rows")
    return {"rows": len(rows), "types": sorted({str(row["record_type"]) for row in rows})}


def bls() -> dict[str, Any]:
    year = datetime.now(timezone.utc).year
    payload = json.dumps({
        "seriesid": ["CUUR0000SA0", "LNS14000000", "CES0000000001",
                     "CES0500000003", "WPSFD4", "JTS000000000000000JOL"],
        "startyear": str(year - 1), "endyear": str(year),
        **({"registrationkey": os.getenv("BLS_API_KEY", "").strip()}
           if os.getenv("BLS_API_KEY", "").strip() else {}),
    }).encode()
    raw = json.loads(fetch(
        "https://api.bls.gov/publicAPI/v2/timeseries/data/", data=payload,
        headers={"Content-Type": "application/json"},
    ))
    rows = normalize_bls_series(((raw.get("Results") or {}).get("series") or []))
    if len(rows) < 6:
        raise RuntimeError(f"expected 6 normalized series, got {len(rows)}")
    return {"rows": len(rows), "types": sorted(str(row["record_type"]) for row in rows)}


def cftc() -> dict[str, Any]:
    params = urllib.parse.urlencode({
        "$limit": "500", "$order": "report_date_as_yyyy_mm_dd DESC",
        "$where": "contract_market_name like '%GOLD%'",
    })
    raw = json_fetch(f"https://publicreporting.cftc.gov/resource/gpe5-46if.json?{params}")
    rows = normalize_cftc_rows(raw, "POSITIONING_TFF")
    if not rows or not any(row["record_type"] == "POSITIONING_ZSCORE" for row in rows):
        raise RuntimeError("no normalized positioning z-score")
    return {"raw_rows": len(raw), "normalized_rows": len(rows),
            "assets": sorted({str(row["asset_id"]) for row in rows})}


def ishares() -> dict[str, Any]:
    rows = normalize_ishares_gold_html(fetch(
        "https://www.ishares.com/us/products/239561/ishares-gold-trust-fund"
    ))
    if not rows:
        raise RuntimeError("no dated IAU holdings parsed")
    return {"rows": len(rows), "date": rows[0]["date"], "tonnes": rows[0]["tonnes"]}


def spdr() -> dict[str, Any]:
    rows = normalize_spdr_gold_workbook(fetch(
        "https://api.spdrgoldshares.com/api/v1/historical-archive?product=gld&exchange=NYSE&lang=en"
    ))
    if not rows:
        raise RuntimeError("no dated GLD holdings parsed")
    return {"rows": len(rows), "date": rows[0]["date"], "tonnes": rows[0]["tonnes"]}


def eia() -> dict[str, Any]:
    key = os.getenv("EIA_API_KEY", "").strip()
    if not key:
        return {"skipped": True, "reason": "EIA_API_KEY missing", "built_in_routes": len(EIA_DEFAULT_ROUTES)}
    route = EIA_DEFAULT_ROUTES[0]
    params = urllib.parse.urlencode({"api_key": key, "length": "2"})
    raw = json_fetch(f"https://api.eia.gov/v2/seriesid/{route['series_id']}?{params}")
    rows = ((raw.get("response") or {}).get("data") or [])
    if not rows:
        raise RuntimeError("no EIA rows")
    return {"rows": len(rows), "series_id": route["series_id"]}


def main() -> int:
    results = [
        checked("treasury_nominal", lambda: treasury(False)),
        checked("treasury_real", lambda: treasury(True)),
        checked("nyfed_sofr", lambda: nyfed("sofr", True)),
        checked("nyfed_effr", lambda: nyfed("effr", False)),
        checked("bls_macro", bls),
        checked("cftc_positioning", cftc),
        checked("spdr_gld", spdr),
        checked("ishares_iau_optional", ishares, required=False),
        checked("eia_oil", eia),
        {"name": "world_gold_council", "ok": True,
         "skipped": not bool(os.getenv("WGC_AUTH_COOKIE", "").strip()),
         "reason": "WGC_AUTH_COOKIE missing" if not os.getenv("WGC_AUTH_COOKIE", "").strip()
         else "credential configured; runtime connector performs authenticated workbook checks"},
    ]
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "required_failures": [row["name"] for row in results if not row.get("ok")],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["required_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
