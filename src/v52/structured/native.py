from __future__ import annotations

import asyncio
import base64
import csv
import io
import hashlib
import json
import zipfile
import logging
import os
import random
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Settings
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.structured import StructuredStore
from .normalization import (
    normalize_bls_series,
    normalize_cftc_rows,
    normalize_ishares_gold_html,
    normalize_nyfed_rates,
    normalize_spdr_gold_workbook,
    normalize_wgc_workbook,
    parse_treasury_curve_xml,
)

logger = logging.getLogger(__name__)


EIA_DEFAULT_ROUTES: tuple[dict[str, str], ...] = (
    {"name":"US commercial crude inventories","series_id":"PET.WCESTUS1.W","event_type":"OIL_INVENTORY","entity":"WTI"},
    {"name":"US total motor gasoline inventories","series_id":"PET.WGTSTUS1.W","event_type":"GASOLINE_INVENTORY","entity":"WTI"},
    {"name":"US distillate fuel oil inventories","series_id":"PET.WDISTUS1.W","event_type":"DISTILLATE_INVENTORY","entity":"WTI"},
    {"name":"US field production of crude oil","series_id":"PET.WCRFPUS2.W","event_type":"OIL_PRODUCTION","entity":"WTI"},
    {"name":"US refinery utilization","series_id":"PET.WPULEUS3.W","event_type":"REFINERY_UTILIZATION","entity":"WTI"},
)


@dataclass(slots=True)
class JsonSpec:
    slug: str
    source: str
    url: str
    record_type: str
    lane: str
    poll: float
    method: str = "GET"
    params: dict[str, Any] = field(default_factory=dict)
    json_body: Any = None
    item_path: tuple[Any, ...] = ()
    limit: int = 200
    headers: dict[str, str] = field(default_factory=dict)


def _next_error_delay(poll: float, current: float) -> float:
    """Back off without ever shortening a source's configured poll interval."""
    return min(max(3600.0, poll), max(poll, current * 2.0))


def _walk(obj: Any, path: tuple[Any, ...]) -> Any:
    cur = obj
    for key in path:
        if isinstance(key, int) and isinstance(cur, list) and len(cur) > key:
            cur = cur[key]
        elif isinstance(key, str) and isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


class NativePublicService:
    """Clean V52 native official/alternative-data/filing adapters.

    Each source has its own guarded polling task inside one lane-specific process. HTTP failures are
    classified by ResilientHttpClient and written to provider_status; a broken source cannot kill
    sibling sources or the hot-news processes. StructuredStore's payload hash provides durable
    deduplication without legacy mutable state files.
    """

    def __init__(self, settings: Settings, metrics: Metrics, lane: str):
        self.settings = settings
        self.metrics = metrics
        self.lane = lane
        self.store = StructuredStore(settings)
        self.http = ResilientHttpClient(
            settings, name=f"native-{lane}", concurrency=settings.public_native_concurrency,
            per_host_limit=2, retries=2, total_timeout=20.0, max_body_bytes=12_000_000,
        )
        self.tasks: set[asyncio.Task[Any]] = set()

    def official_specs(self) -> list[JsonSpec]:
        year = time.gmtime().tm_year
        specs = [
            JsonSpec("federal_register", "Federal Register API", "https://www.federalregister.gov/api/v1/documents.json", "FEDERAL_REGISTER_DOCUMENT", "RULEMAKING_POLICY", 60, params={"per_page":"100","order":"newest"}, item_path=("results",), limit=100),
            JsonSpec("bls", "BLS Public Data API", "https://api.bls.gov/publicAPI/v2/timeseries/data/", "BLS_SERIES_UPDATE", "US_MACRO_RELEASES", float(os.getenv("BLS_POLL_SECONDS", "900" if os.getenv("BLS_API_KEY", "").strip() else "21600")), method="POST", json_body={"seriesid":["CUUR0000SA0","LNS14000000","CES0000000001","CES0500000003","WPSFD4","JTS000000000000000JOL"],"startyear":str(year-1),"endyear":str(year),**({"registrationkey":os.getenv("BLS_API_KEY","").strip()} if os.getenv("BLS_API_KEY","").strip() else {})}, item_path=("Results","series"), limit=20, headers={"Content-Type":"application/json"}),
            JsonSpec("treasury_debt", "US Treasury FiscalData", "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/debt_to_penny", "US_DEBT_SNAPSHOT", "TREASURY", 120, params={"sort":"-record_date","page[size]":"10"}, item_path=("data",), limit=10),
            JsonSpec("treasury_auctions", "US Treasury FiscalData", "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query", "TREASURY_AUCTION_UPDATE", "TREASURY", 120, params={"sort":"-auction_date","page[size]":"50"}, item_path=("data",), limit=50),
            JsonSpec("gleif", "GLEIF LEI API", "https://api.gleif.org/api/v1/lei-records", "LEI_REGISTRY_UPDATE", "ENTITY_RESOLUTION", 1800, params={"page[size]":"100"}, item_path=("data",), limit=100),
            JsonSpec("fdic", "FDIC BankFind Suite API", "https://api.fdic.gov/banks/financials", "FDIC_BANK_FINANCIALS", "BANK_BALANCE_SHEET", 3600, params={"limit":"100","offset":"0","sort_by":"REPDTE","sort_order":"DESC","format":"json"}, item_path=("data",), limit=100),
        ]
        for name, did, rtype in [
            ("TFF Futures Only","gpe5-46if","POSITIONING_TFF"),
            ("Disaggregated Futures Only","72hh-3qpy","POSITIONING_DISAGGREGATED"),
            ("Legacy Combined","jun7-fc8e","POSITIONING_LEGACY"),
            ("Commodity Index Traders","j83k-qyrd","POSITIONING_CIT"),
        ]:
            market_filter = " OR ".join(
                f"contract_market_name like '%{term}%'" for term in (
                    "GOLD", "SILVER", "COPPER", "CRUDE OIL", "BITCOIN", "ETHER",
                    "U.S. DOLLAR INDEX", "EURO FX", "BRITISH POUND", "JAPANESE YEN",
                    "S&P 500", "NASDAQ-100",
                )
            )
            specs.append(JsonSpec(
                f"cftc_{did}", f"CFTC Public Reporting - {name}",
                f"https://publicreporting.cftc.gov/resource/{did}.json",
                rtype, "CFTC_POSITIONING", 3600,
                params={"$limit":"2000", "$order":"report_date_as_yyyy_mm_dd DESC", "$where":market_filter},
                limit=2000,
            ))
        for code, metric in {
            "NY.GDP.MKTP.CD":"GDP_CURRENT_USD", "FP.CPI.TOTL.ZG":"CPI_INFLATION",
            "SL.UEM.TOTL.ZS":"UNEMPLOYMENT", "NE.TRD.GNFS.ZS":"TRADE_GDP",
        }.items():
            specs.append(JsonSpec(f"worldbank_{code}", f"World Bank {metric}", f"https://api.worldbank.org/v2/country/all/indicator/{code}", "WORLD_BANK_INDICATOR_REVISION", "GLOBAL_MACRO", 3600, params={"format":"json","per_page":"400","date":f"{year-2}:{year}"}, item_path=(1,), limit=400))
        return specs

    def altdata_specs(self) -> list[JsonSpec]:
        specs = [
            JsonSpec("usgs_earthquake", "USGS Earthquake Hazards", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson", "EARTHQUAKE_DETECTED", "GEOPHYSICAL_DISRUPTION", 30, item_path=("features",), limit=500),
            JsonSpec("cisa_kev", "CISA KEV Catalog", "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json", "CISA_KNOWN_EXPLOITED_VULNERABILITY", "CYBERSECURITY", 300, item_path=("vulnerabilities",), limit=5000),
            JsonSpec("clinical_trials", "ClinicalTrials.gov API", "https://clinicaltrials.gov/api/v2/studies", "CLINICAL_TRIAL_UPDATE", "HEALTHCARE_CLINICAL", 300, params={"pageSize":"100","sort":"LastUpdatePostDate:desc","format":"json"}, item_path=("studies",), limit=100),
            JsonSpec("nws_alerts", "US National Weather Service", "https://api.weather.gov/alerts/active", "NWS_ACTIVE_ALERT", "WEATHER_CLIMATE", 30, headers={"Accept":"application/geo+json"}, item_path=("features",), limit=2000),
        ]
        fda = [
            ("openfda_drug_event","DRUG_ADVERSE_EVENT","https://api.fda.gov/drug/event.json",{"sort":"receivedate:desc","limit":"50"}),
            ("openfda_drug_enforcement","DRUG_ENFORCEMENT","https://api.fda.gov/drug/enforcement.json",{"sort":"report_date:desc","limit":"50"}),
            ("openfda_drug_label","DRUG_LABEL_UPDATE","https://api.fda.gov/drug/label.json",{"limit":"50"}),
            ("openfda_device_event","DEVICE_ADVERSE_EVENT","https://api.fda.gov/device/event.json",{"sort":"date_received:desc","limit":"50"}),
            ("openfda_device_recall","DEVICE_RECALL","https://api.fda.gov/device/recall.json",{"sort":"event_date_initiated:desc","limit":"50"}),
            ("openfda_device_enforcement","DEVICE_ENFORCEMENT","https://api.fda.gov/device/enforcement.json",{"sort":"report_date:desc","limit":"50"}),
            ("openfda_510k","FDA_510K","https://api.fda.gov/device/510k.json",{"sort":"date_received:desc","limit":"50"}),
            ("openfda_pma","FDA_PMA","https://api.fda.gov/device/pma.json",{"sort":"decision_date:desc","limit":"50"}),
            ("openfda_udi","FDA_UDI","https://api.fda.gov/device/udi.json",{"limit":"50"}),
            ("openfda_food_enforcement","FOOD_ENFORCEMENT","https://api.fda.gov/food/enforcement.json",{"sort":"report_date:desc","limit":"50"}),
        ]
        for slug, rtype, url, params in fda:
            specs.append(JsonSpec(slug, "openFDA", url, rtype, "FDA_SAFETY", 300, params=params, item_path=("results",), limit=50))
        return specs

    async def _run_json_spec(self, spec: JsonSpec, stop_event: asyncio.Event) -> None:
        wait = spec.poll
        while not stop_event.is_set():
            try:
                headers = dict(spec.headers)
                if spec.slug == "fdic" and os.getenv("FDIC_API_KEY", "").strip():
                    headers["X-API-Key"] = os.getenv("FDIC_API_KEY", "").strip()
                result = await self.http.request(spec.method, spec.url, params=spec.params or None, headers=headers or None, json_body=spec.json_body, retries=2)
                if result.ok:
                    try:
                        obj = json.loads(result.body)
                    except Exception as exc:
                        raise RuntimeError(f"invalid JSON response: {exc}") from exc
                    rows = _walk(obj, spec.item_path) if spec.item_path else obj
                    if rows is None:
                        raise RuntimeError(f"expected item path missing: {spec.item_path}")
                    if isinstance(rows, list):
                        selected = rows[:spec.limit]
                        storage_type = spec.record_type
                        if spec.slug == "bls":
                            selected = normalize_bls_series(selected)
                            storage_type = "BLS_NORMALIZED_RELEASE"
                        elif spec.slug.startswith("cftc_"):
                            selected = normalize_cftc_rows(selected, spec.record_type)
                            storage_type = "POSITIONING_ZSCORE"
                        changed = await self.store.insert_snapshots(spec.slug, spec.lane, storage_type, selected)
                        self.metrics.inc("native_records_changed", changed)
                        self.metrics.inc("native_records_seen", len(selected))
                    else:
                        changed = int(await self.store.insert_snapshot(spec.slug, spec.lane, spec.record_type, rows))
                        self.metrics.inc("native_records_changed", changed)
                        self.metrics.inc("native_records_seen")
                    await self.store.status(spec.slug, "ok", result.status)
                    self.metrics.inc("native_http_ok")
                    wait = spec.poll
                else:
                    await self.store.status(spec.slug, result.classification, result.status, result.error, result.classification == "rate_limited")
                    self.metrics.inc(f"native_{result.classification}")
                    wait = max(spec.poll, result.retry_after or 0.0)
                    if result.classification in {"blocked_or_auth", "not_found"}:
                        wait = max(wait, 1800.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.inc("native_unexpected")
                await self.store.status(spec.slug, "unexpected_error", error=f"{type(exc).__name__}:{exc}")
                logger.exception("native source failed slug=%s", spec.slug)
                wait = _next_error_delay(spec.poll, wait)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait * (1.0 + random.random() * 0.03))
            except asyncio.TimeoutError:
                continue

    async def _ecb_loop(self, stop_event: asyncio.Event) -> None:
        slug="ecb_reference_rate"; url="https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
        while not stop_event.is_set():
            wait=300.0
            try:
                result=await self.http.get(url, params={"format":"csvdata"}, headers={"Accept":"text/csv"}, retries=2)
                if result.ok:
                    rows=list(csv.DictReader(io.StringIO(result.text())))
                    if rows:
                        await self.store.insert_snapshot(slug,"CENTRAL_BANK_MARKET_DATA","ECB_REFERENCE_RATE",rows[-1])
                        self.metrics.inc("native_records_seen")
                    await self.store.status(slug,"ok",result.status)
                else:
                    await self.store.status(slug,result.classification,result.status,result.error,result.classification=="rate_limited")
                    wait=max(wait,result.retry_after or 0.0)
            except asyncio.CancelledError: raise
            except Exception as exc:
                logger.exception("ECB native adapter failed"); await self.store.status(slug,"unexpected_error",error=f"{type(exc).__name__}:{exc}"); self.metrics.inc("native_unexpected")
            try: await asyncio.wait_for(stop_event.wait(),timeout=wait)
            except asyncio.TimeoutError: continue

    async def _treasury_rates_loop(self, stop_event: asyncio.Event) -> None:
        sources = (
            ("treasury_nominal_yields", "https://home.treasury.gov/sites/default/files/interest-rates/yield.xml", False),
            ("treasury_real_yields", "https://home.treasury.gov/sites/default/files/interest-rates/real_yield.xml", True),
        )
        while not stop_event.is_set():
            for slug, url, real in sources:
                try:
                    result = await self.http.get(url, headers={"Accept":"application/xml"}, retries=2)
                    if result.ok:
                        rows = parse_treasury_curve_xml(result.body, real=real)
                        if not rows:
                            raise RuntimeError("Treasury XML contained no valid current curve")
                        changed = await self.store.insert_snapshots(
                            slug, "CENTRAL_BANK_MARKET_DATA", "TREASURY_RATE_CURVE", rows,
                        )
                        self.metrics.inc("native_records_changed", changed)
                        self.metrics.inc("native_records_seen", len(rows))
                        await self.store.status(slug, "ok", result.status)
                    else:
                        await self.store.status(slug, result.classification, result.status, result.error, result.classification=="rate_limited")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Treasury rate adapter failed slug=%s", slug)
                    await self.store.status(slug, "parse_or_runtime_error", error=f"{type(exc).__name__}:{exc}")
                    self.metrics.inc("native_unexpected")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=float(os.getenv("TREASURY_RATES_POLL_SECONDS", "900")))
            except asyncio.TimeoutError:
                continue

    async def _nyfed_reference_rates_loop(self, stop_event: asyncio.Event) -> None:
        endpoints = {
            "sofr": "https://markets.newyorkfed.org/api/rates/secured/sofr/last/5.json",
            "tgcr": "https://markets.newyorkfed.org/api/rates/secured/tgcr/last/5.json",
            "bgcr": "https://markets.newyorkfed.org/api/rates/secured/bgcr/last/5.json",
            "effr": "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/5.json",
            "obfr": "https://markets.newyorkfed.org/api/rates/unsecured/obfr/last/5.json",
        }
        while not stop_event.is_set():
            for rate, url in endpoints.items():
                slug = f"newyorkfed_{rate}"
                try:
                    result = await self.http.get(url, headers={"Accept":"application/json"}, retries=2)
                    if result.ok:
                        rows = normalize_nyfed_rates(json.loads(result.body))
                        if not rows:
                            raise RuntimeError("NY Fed response contained no reference rate")
                        changed = await self.store.insert_snapshots(
                            slug, "CENTRAL_BANK_MARKET_DATA", "NYFED_REFERENCE_RATE", rows,
                        )
                        self.metrics.inc("native_records_changed", changed)
                        self.metrics.inc("native_records_seen", len(rows))
                        await self.store.status(slug, "ok", result.status)
                    else:
                        await self.store.status(slug, result.classification, result.status, result.error, result.classification=="rate_limited")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("NY Fed reference-rate adapter failed rate=%s", rate)
                    await self.store.status(slug, "parse_or_runtime_error", error=f"{type(exc).__name__}:{exc}")
                    self.metrics.inc("native_unexpected")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=float(os.getenv("NYFED_RATES_POLL_SECONDS", "300")))
            except asyncio.TimeoutError:
                continue

    async def _gold_holdings_loop(self, stop_event: asyncio.Event) -> None:
        sources = (
            ("spdr_gld_gold_holdings", "https://api.spdrgoldshares.com/api/v1/historical-archive?product=gld&exchange=NYSE&lang=en", normalize_spdr_gold_workbook, "GLD"),
            ("ishares_iau_gold_holdings", "https://www.ishares.com/us/products/239561/ishares-gold-trust-fund", normalize_ishares_gold_html, "IAU"),
        )
        while not stop_event.is_set():
            for slug, url, normalizer, fund in sources:
                try:
                    result = await self.http.get(url, retries=2, max_body_bytes=3_000_000)
                    if result.ok:
                        rows = normalizer(result.body)
                        if not rows:
                            raise RuntimeError(f"{fund} source contained no dated tonnes-in-trust value")
                        changed = await self.store.insert_snapshots(
                            slug, "GOLD_FLOWS", "GOLD_ETF_HOLDINGS", rows,
                        )
                        self.metrics.inc("native_records_changed", changed)
                        self.metrics.inc("native_records_seen", len(rows))
                        await self.store.status(slug, "ok", result.status)
                    else:
                        await self.store.status(slug, result.classification, result.status, result.error, result.classification=="rate_limited")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("gold ETF holdings adapter failed fund=%s", fund)
                    await self.store.status(slug, "parse_or_runtime_error", error=f"{type(exc).__name__}:{exc}")
                    self.metrics.inc("native_unexpected")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=float(os.getenv("GOLD_HOLDINGS_POLL_SECONDS", "3600")))
            except asyncio.TimeoutError:
                continue

    async def _wgc_gold_loop(self, stop_event: asyncio.Event) -> None:
        slug = "world_gold_council_official_sector"
        page_url = "https://www.gold.org/goldhub/data/gold-reserves-by-country"
        while not stop_event.is_set():
            cookie = os.getenv("WGC_AUTH_COOKIE", "").strip()
            if not cookie:
                await self.store.status(
                    slug, "disabled_missing_key",
                    error="WGC_AUTH_COOKIE is required for authenticated official-reserve workbooks",
                )
            else:
                try:
                    headers={"Accept":"text/html","Cookie":cookie}
                    page=await self.http.get(page_url,headers=headers,retries=2,max_body_bytes=3_000_000)
                    if not page.ok:
                        await self.store.status(slug,page.classification,page.status,page.error,page.classification=="rate_limited")
                    else:
                        soup=BeautifulSoup(page.body,"html.parser")
                        text=soup.get_text(" ",strip=True)
                        match=re.search(r"Data as of\s+(\d{1,2}\s+[A-Za-z]+,?\s+\d{4})",text,re.I)
                        if not match:raise RuntimeError("WGC page did not expose an as-of date")
                        as_of=None
                        for fmt in ("%d %B, %Y","%d %B %Y"):
                            try:as_of=datetime.strptime(match.group(1),fmt).date().isoformat();break
                            except ValueError:continue
                        if not as_of:raise RuntimeError("WGC as-of date was not parseable")
                        links=[]
                        for anchor in soup.find_all("a",href=True):
                            href=str(anchor.get("href") or "")
                            label=anchor.get_text(" ",strip=True).lower()
                            if "/download/file/" in href and "xlsx" in (href+" "+label).lower():
                                dataset="changes" if "change" in (href+" "+label).lower() else "holdings"
                                if dataset in {"changes","holdings"}:links.append((dataset,urljoin(page_url,href)))
                        if not links:raise RuntimeError("WGC page exposed no authenticated XLSX links")
                        changed=seen=0
                        for dataset,url in dict(links).items():
                            download=await self.http.get(url,headers={"Accept":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","Cookie":cookie,"Referer":page_url},retries=2,max_body_bytes=20_000_000)
                            if not download.ok:raise RuntimeError(f"WGC {dataset} download failed status={download.status}")
                            rows=normalize_wgc_workbook(download.body,dataset,as_of)
                            if not rows:raise RuntimeError(f"WGC {dataset} workbook contained no usable measure")
                            changed+=await self.store.insert_snapshots(slug,"GOLD_FLOWS",rows[0]["record_type"],rows)
                            seen+=len(rows)
                        self.metrics.inc("native_records_changed",changed)
                        self.metrics.inc("native_records_seen",seen)
                        await self.store.status(slug,"ok",page.status)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("World Gold Council adapter failed")
                    await self.store.status(slug,"parse_or_runtime_error",error=f"{type(exc).__name__}:{exc}")
                    self.metrics.inc("native_unexpected")
            try:
                await asyncio.wait_for(stop_event.wait(),timeout=float(os.getenv("WGC_GOLD_POLL_SECONDS","21600")))
            except asyncio.TimeoutError:
                continue

    async def _dataset_watch_loop(self, stop_event: asyncio.Event) -> None:
        sites=[
            ("cms_part_d","CMS Medicare Part D Prescribers","https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers"),
            ("epa_echo","EPA ECHO Web Services","https://echo.epa.gov/tools/web-services"),
            ("fcc_uls","FCC ULS Public Database","https://www.fcc.gov/wireless/data/public-access-files-database-downloads"),
            ("usgs_minerals","USGS Mineral Commodity Data","https://www.usgs.gov/centers/national-minerals-information-center/data"),
            ("bts_faf","BTS Freight Analysis Framework","https://faf.ornl.gov/faf5/"),
            ("usgs_landsat","USGS Landsat","https://www.usgs.gov/landsat-missions/landsat-data-access"),
            ("bis_data","BIS International Banking Statistics","https://data.bis.org/"),
            ("ecb_data","ECB Securities Holdings","https://data.ecb.europa.eu/"),
            ("nyfed_primary_dealer","New York Fed Primary Dealer Statistics","https://www.newyorkfed.org/markets/counterparties/primary-dealers-statistics"),
            ("treasury_tic","Treasury TIC","https://home.treasury.gov/data/treasury-international-capital-tic-system"),
        ]
        while not stop_event.is_set():
            for slug,name,url in sites:
                if stop_event.is_set(): break
                result=await self.http.get(url,retries=1,max_body_bytes=5_000_000)
                if not result.ok:
                    await self.store.status(slug,result.classification,result.status,result.error,result.classification=="rate_limited"); continue
                soup=BeautifulSoup(result.text(),"lxml")
                links=[]
                for a in soup.find_all("a",href=True)[:1500]:
                    full=urljoin(str(result.url),str(a.get("href") or "")); low=full.lower()
                    if any(x in low for x in (".csv",".json",".xml",".zip",".xlsx",".xls",".pdf","download","dataset","api")):
                        links.append({"url":full,"text":" ".join(a.get_text(" ",strip=True).split())[:240]})
                payload={"source":name,"final_url":result.url,"links":links[:300],"bytes":len(result.body)}
                await self.store.insert_snapshot(slug,"PUBLIC_DATASET_WATCH","DATASET_SURFACE_REVISION",payload)
                await self.store.status(slug,"ok",result.status)
            try: await asyncio.wait_for(stop_event.wait(),timeout=900.0)
            except asyncio.TimeoutError: continue

    async def _filing_surface_loop(self, stop_event: asyncio.Event) -> None:
        specs=[
            ("hkex","HKEXnews","https://www.hkexnews.hk/"),
            ("asx","ASX Company Announcements","https://www.asx.com.au/asx/v2/statistics/todayAnns.do"),
            ("sgx","SGX Company Announcements","https://www.sgx.com/securities/company-announcements"),
            ("esma_esef","EU ESEF / ESMA","https://www.esma.europa.eu/issuer-disclosure/electronic-reporting"),
            ("sedar_plus","Canada SEDAR+","https://www.sedarplus.ca/"),
        ]
        while not stop_event.is_set():
            for slug,name,url in specs:
                if stop_event.is_set(): break
                result=await self.http.get(url,retries=1,max_body_bytes=8_000_000)
                if not result.ok:
                    await self.store.status(slug,result.classification,result.status,result.error,result.classification=="rate_limited"); continue
                soup=BeautifulSoup(result.text(),"lxml"); seen=set(); records=[]
                for a in soup.find_all("a",href=True)[:3000]:
                    full=urljoin(str(result.url),str(a.get("href") or "")); text=" ".join(a.get_text(" ",strip=True).split())
                    blob=(full+" "+text).lower()
                    if not full.startswith("http") or full in seen: continue
                    if any(x in blob for x in ("announcement","filing","financial","annual report","prospectus","circular","esef","xbrl",".pdf",".xml",".xhtml",".zip")):
                        seen.add(full); records.append({"title":text[:500],"url":full,"source":name})
                    if len(records)>=600: break
                await self.store.insert_snapshots(slug,"GLOBAL_ISSUER_FILINGS","ISSUER_FILING_DETECTED",records)
                await self.store.insert_snapshot(slug,"GLOBAL_ISSUER_FILINGS","FILING_SURFACE_REVISION",{"source":name,"final_url":result.url,"document_links":len(records)})
                await self.store.status(slug,"ok",result.status)
                self.metrics.inc("filing_records_seen",len(records))
            try: await asyncio.wait_for(stop_event.wait(),timeout=120.0)
            except asyncio.TimeoutError: continue

    async def _edinet_loop(self, stop_event: asyncio.Event) -> None:
        slug="edinet"; url="https://api.edinet-fsa.go.jp/api/v2/documents.json"
        while not stop_event.is_set():
            key=os.getenv("EDINET_API_KEY","").strip()
            if not key:
                await self.store.status(slug,"disabled_missing_key",error="EDINET_API_KEY not configured")
                try: await asyncio.wait_for(stop_event.wait(),timeout=60.0)
                except asyncio.TimeoutError: continue
                continue
            day=time.strftime("%Y-%m-%d",time.gmtime())
            result=await self.http.get(url,params={"date":day,"type":"2","Subscription-Key":key},retries=2)
            if result.ok:
                try:
                    obj=json.loads(result.body)
                except Exception as exc:
                    self.metrics.inc("filing_parse_error")
                    await self.store.status(slug,"parse_error",result.status,error=f"{type(exc).__name__}:{exc}")
                    logger.warning("EDINET invalid JSON error=%s",exc)
                    obj={}
                rows=obj.get("results") if isinstance(obj,dict) else []
                selected=(rows or [])[:5000]
                await self.store.insert_snapshots(slug,"GLOBAL_ISSUER_FILINGS","EDINET_FILING",selected)
                if isinstance(obj,dict) and "results" in obj:
                    await self.store.status(slug,"ok",result.status)
                self.metrics.inc("filing_records_seen",len(selected))
            else:
                await self.store.status(slug,result.classification,result.status,result.error,result.classification=="rate_limited")
            try: await asyncio.wait_for(stop_event.wait(),timeout=30.0)
            except asyncio.TimeoutError: continue

    async def _companies_house_stream(self, stop_event: asyncio.Event) -> None:
        slug="companies_house_stream"; url="https://stream.companieshouse.gov.uk/filings"
        while not stop_event.is_set():
            key=(os.getenv("COMPANIES_HOUSE_STREAM_KEY","").strip() or os.getenv("COMPANIES_HOUSE_API_KEY","").strip())
            if not key:
                await self.store.status(slug,"disabled_missing_key",error="COMPANIES_HOUSE_STREAM_KEY or COMPANIES_HOUSE_API_KEY not configured")
                try: await asyncio.wait_for(stop_event.wait(),timeout=60.0)
                except asyncio.TimeoutError: continue
                continue
            auth="Basic "+base64.b64encode((key+":").encode()).decode()
            try:
                timeout=__import__('aiohttp').ClientTimeout(total=None,connect=15,sock_read=None)
                async with self.http.session.get(url,headers={"Authorization":auth,"Accept":"application/json"},timeout=timeout) as resp:
                    if resp.status!=200:
                        await self.store.status(slug,"stream_http_error",resp.status,error=f"HTTP {resp.status}",rate_limited=resp.status==429)
                        await asyncio.sleep(10 if resp.status in {401,403,429} else 3); continue
                    await self.store.status(slug,"ok",resp.status)
                    async for raw in resp.content:
                        if stop_event.is_set(): return
                        for line in raw.splitlines():
                            if not line.strip() or len(line)>8_000_000: continue
                            try: ev=json.loads(line)
                            except Exception as exc:
                                self.metrics.inc("filing_stream_parse_error"); logger.warning("Companies House stream JSON parse failed error=%s",exc); continue
                            await self.store.insert_snapshot(slug,"GLOBAL_ISSUER_FILINGS","COMPANIES_HOUSE_FILING_STREAM",ev)
                            self.metrics.inc("filing_records_seen")
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.metrics.inc("filing_stream_error"); await self.store.status(slug,"stream_error",error=f"{type(exc).__name__}:{exc}"); logger.warning("Companies House stream reconnect error=%s",exc)
                try: await asyncio.wait_for(stop_event.wait(),timeout=2.0)
                except asyncio.TimeoutError: continue

    async def _usaspending_loop(self, stop_event: asyncio.Event) -> None:
        slug="usaspending"; last_url="https://api.usaspending.gov/api/v2/awards/last_updated/"
        while not stop_event.is_set():
            result=await self.http.get(last_url,retries=2)
            if result.ok:
                try:
                    obj=json.loads(result.body)
                    await self.store.insert_snapshot(slug,"GOVERNMENT_CONTRACTS","GOVERNMENT_AWARD_DATASET_UPDATED",obj)
                    await self.store.status(slug,"ok",result.status)
                    self.metrics.inc("usaspending_ok")
                except Exception as exc:
                    await self.store.status(slug,"parse_error",result.status,error=f"{type(exc).__name__}:{exc}"); self.metrics.inc("usaspending_parse_error")
                body_raw=os.getenv("USASPENDING_SEARCH_BODY_JSON","").strip()
                if body_raw:
                    try:
                        body=json.loads(body_raw)
                        if not isinstance(body,dict): raise ValueError("USASPENDING_SEARCH_BODY_JSON must be an object")
                        surl="https://api.usaspending.gov/api/v2/search/spending_by_award/"
                        sr=await self.http.request("POST",surl,json_body=body,retries=2,max_body_bytes=12_000_000)
                        if sr.ok:
                            sobj=json.loads(sr.body); rows=(sobj.get("results") if isinstance(sobj,dict) else []) or []
                            await self.store.insert_snapshots(slug,"GOVERNMENT_CONTRACTS","GOVERNMENT_CONTRACT_AWARDED",rows[:1000])
                        else: await self.store.status(slug,sr.classification,sr.status,sr.error,sr.classification=="rate_limited")
                    except Exception as exc:
                        await self.store.status(slug,"configured_query_error",error=f"{type(exc).__name__}:{exc}"); self.metrics.inc("usaspending_query_error")
            else: await self.store.status(slug,result.classification,result.status,result.error,result.classification=="rate_limited")
            try: await asyncio.wait_for(stop_event.wait(),timeout=float(os.getenv("USASPENDING_POLL_SECONDS","300")))
            except asyncio.TimeoutError: continue

    async def _fca_short_loop(self, stop_event: asyncio.Event) -> None:
        slug="fca_short_positions"; url="https://www.fca.org.uk/publication/documents/aggregated-current-net-short-positions.csv"
        while not stop_event.is_set():
            result=await self.http.get(url,retries=2,max_body_bytes=20_000_000)
            if result.ok:
                try:
                    rows=list(csv.DictReader(io.StringIO(result.text())))
                    cap=max(1000,min(100000,int(os.getenv('FCA_SHORT_MAX_ROWS','20000'))))
                    await self.store.insert_snapshots(slug,"GLOBAL_SHORT_POSITIONS","AGGREGATE_SHORT_POSITION",rows[:cap])
                    await self.store.status(slug,"ok",result.status); self.metrics.inc("fca_short_rows",len(rows))
                except Exception as exc:
                    await self.store.status(slug,"parse_error",result.status,error=f"{type(exc).__name__}:{exc}"); self.metrics.inc("fca_short_parse_error")
            else: await self.store.status(slug,result.classification,result.status,result.error,result.classification=="rate_limited")
            try: await asyncio.wait_for(stop_event.wait(),timeout=float(os.getenv("FCA_SHORT_POLL_SECONDS","900")))
            except asyncio.TimeoutError: continue

    async def _sec_fund_dataset_loop(self, stop_event: asyncio.Event) -> None:
        pages={
          "N-MFP":"https://www.sec.gov/data-research/sec-markets-data/dera-form-n-mfp-data-sets",
          "N-CEN":"https://www.sec.gov/data-research/sec-markets-data/form-n-cen-data-sets",
          "N-PORT":"https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets",
          "FORM-D":"https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets",
          "BDC":"https://www.sec.gov/data-research/sec-markets-data/business-development-company-data-sets",
        }
        while not stop_event.is_set():
            for form,url in pages.items():
                result=await self.http.get(url,headers={"User-Agent":self.settings.user_agent,"Accept-Encoding":"gzip, deflate"},retries=2,max_body_bytes=12_000_000)
                slug="sec_fund_"+form.lower().replace("-","_")
                if result.ok:
                    payload={"form":form,"sha256":hashlib.sha256(result.body).hexdigest(),"bytes":len(result.body),"url":str(result.url)}
                    await self.store.insert_snapshot(slug,"SEC_FUND_INTELLIGENCE","FUND_DATASET_REVISION",payload); await self.store.status(slug,"ok",result.status)
                else: await self.store.status(slug,result.classification,result.status,result.error,result.classification=="rate_limited")
            try: await asyncio.wait_for(stop_event.wait(),timeout=float(os.getenv("SEC_FUND_POLL_SECONDS","1800")))
            except asyncio.TimeoutError: continue

    async def _sec_entity_loop(self, stop_event: asyncio.Event) -> None:
        url="https://www.sec.gov/files/company_tickers.json"; slug="sec_company_tickers"
        while not stop_event.is_set():
            result=await self.http.get(url,headers={"User-Agent":self.settings.user_agent},retries=2,max_body_bytes=20_000_000)
            if result.ok:
                try:
                    obj=json.loads(result.body)
                    await self.store.insert_snapshot(slug,"ENTITY_RESOLUTION_GRAPH","IDENTIFIER_REGISTRY_UPDATED",{"count":len(obj) if isinstance(obj,dict) else 0,"sha256":hashlib.sha256(result.body).hexdigest()})
                    await self.store.status(slug,"ok",result.status)
                except Exception as exc: await self.store.status(slug,"parse_error",result.status,error=f"{type(exc).__name__}:{exc}")
            else: await self.store.status(slug,result.classification,result.status,result.error,result.classification=="rate_limited")
            names=[x.strip() for x in os.getenv("GLEIF_WATCH_NAMES","").split(",") if x.strip()]
            for name in names:
                gr=await self.http.get("https://api.gleif.org/api/v1/lei-records",params={"filter[entity.legalName]":name},retries=2,max_body_bytes=8_000_000)
                if gr.ok:
                    try:
                        obj=json.loads(gr.body); rows=(obj.get("data") if isinstance(obj,dict) else []) or []
                        await self.store.insert_snapshots("gleif_watch","ENTITY_RESOLUTION_GRAPH","LEI_ENTITY_LINK",[{"query":name,"record":r} for r in rows[:20]])
                    except Exception as exc: await self.store.status("gleif_watch","parse_error",gr.status,error=f"{type(exc).__name__}:{exc}")
            try: await asyncio.wait_for(stop_event.wait(),timeout=float(os.getenv("ENTITY_REFRESH_SECONDS","21600")))
            except asyncio.TimeoutError: continue

    async def _sec_ftd_loop(self, stop_event: asyncio.Event) -> None:
        page="https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data"; slug="sec_ftd"
        while not stop_event.is_set():
            result=await self.http.get(page,headers={"User-Agent":self.settings.user_agent,"Accept-Encoding":"gzip, deflate"},retries=2,max_body_bytes=8_000_000)
            if result.ok:
                soup=BeautifulSoup(result.text(),"lxml"); links=[]
                for a in soup.find_all("a",href=True):
                    u=urljoin(str(result.url),str(a.get("href") or ""))
                    if u.lower().endswith(".zip") and u not in links: links.append(u)
                maxfiles=max(1,int(os.getenv("SEC_FTD_FILES_PER_CHECK","1")))
                for u in links[:maxfiles]:
                    zr=await self.http.get(u,headers={"User-Agent":self.settings.user_agent},retries=2,max_body_bytes=100_000_000)
                    if not zr.ok:
                        await self.store.status(slug,zr.classification,zr.status,zr.error,zr.classification=="rate_limited"); continue
                    try:
                        z=zipfile.ZipFile(io.BytesIO(zr.body)); rows=[]
                        for name in z.namelist():
                            if name.lower().endswith((".txt",".csv")):
                                txt=z.read(name).decode("latin1","ignore")
                                for r in csv.DictReader(io.StringIO(txt),delimiter="|"):
                                    if r: rows.append({str(k).strip().lower().replace(" ","_"):str(v).strip() if v is not None else "" for k,v in r.items() if k})
                        maxrows=max(1,int(os.getenv("SEC_FTD_MAX_ROWS","60000")))
                        await self.store.insert_snapshot(slug,"SETTLEMENT_PLUMBING_FTD","FTD_DATASET_UPDATE",{"file_url":u,"sha256":hashlib.sha256(zr.body).hexdigest(),"rows":len(rows)})
                        await self.store.insert_snapshots(slug,"SETTLEMENT_PLUMBING_FTD","FTD_RECORD",rows[:maxrows])
                        await self.store.status(slug,"ok",zr.status); self.metrics.inc("sec_ftd_rows",min(len(rows),maxrows))
                    except Exception as exc:
                        await self.store.status(slug,"parse_error",zr.status,error=f"{type(exc).__name__}:{exc}"); self.metrics.inc("sec_ftd_parse_error")
            else: await self.store.status(slug,result.classification,result.status,result.error,result.classification=="rate_limited")
            try: await asyncio.wait_for(stop_event.wait(),timeout=float(os.getenv("SEC_FTD_POLL_SECONDS","21600")))
            except asyncio.TimeoutError: continue

    async def _opensky_loop(self, stop_event: asyncio.Event) -> None:
        slug="opensky"; url="https://opensky-network.org/api/states/all"
        while not stop_event.is_set():
            raw=os.getenv("OPENSKY_CONNECTORS_JSON","").strip(); specs=[]
            if raw:
                try:
                    obj=json.loads(raw); specs=obj if isinstance(obj,list) else []
                except Exception as exc:
                    await self.store.status(slug,"config_error",error=f"{type(exc).__name__}:{exc}"); specs=[]
            if not specs and os.getenv("OPENSKY_ENABLED","0").lower() not in {"1","true","yes","on"}:
                await self.store.status(slug,"disabled_config",error="Set OPENSKY_ENABLED=1 or OPENSKY_CONNECTORS_JSON")
            else:
                for cfg in (specs or [{}]):
                    params={k:cfg[k] for k in ("lamin","lomin","lamax","lomax","icao24") if k in cfg}
                    headers={}
                    user=str(cfg.get("username") or os.getenv(str(cfg.get("username_env") or ""),"")).strip()
                    pwd=str(cfg.get("password") or os.getenv(str(cfg.get("password_env") or ""),"")).strip()
                    if user: headers["Authorization"]="Basic "+base64.b64encode(f"{user}:{pwd}".encode()).decode()
                    result=await self.http.get(url,params=params or None,headers=headers or None,retries=2,max_body_bytes=40_000_000)
                    if result.ok:
                        try:
                            obj=json.loads(result.body); states=(obj.get("states") if isinstance(obj,dict) else []) or []; cap=int(cfg.get("max_states") or 5000)
                            await self.store.insert_snapshot(slug,"AVIATION_TRAVEL_MOBILITY","AIRCRAFT_STATE",{"time":obj.get("time") if isinstance(obj,dict) else None,"state_count":len(states),"states":states[:cap],"bounds":params}); await self.store.status(slug,"ok",result.status)
                        except Exception as exc: await self.store.status(slug,"parse_error",result.status,error=f"{type(exc).__name__}:{exc}")
                    else: await self.store.status(slug,result.classification,result.status,result.error,result.classification=="rate_limited")
            try: await asyncio.wait_for(stop_event.wait(),timeout=float(os.getenv("OPENSKY_POLL_SECONDS","20")))
            except asyncio.TimeoutError: continue

    async def _eia_routes_loop(self, stop_event: asyncio.Event) -> None:
        slug="eia_routes"
        while not stop_event.is_set():
            key=os.getenv("EIA_API_KEY","").strip(); raw=os.getenv("EIA_ROUTES_JSON","").strip()
            if not key:
                await self.store.status(slug,"disabled_missing_key",error="EIA_API_KEY is required; standard oil routes are built in")
            else:
                try:
                    routes=json.loads(raw) if raw else [dict(route) for route in EIA_DEFAULT_ROUTES]
                    if not isinstance(routes,list): raise ValueError("EIA_ROUTES_JSON must be a list")
                    for cfg in routes:
                        if not isinstance(cfg,dict): continue
                        url=str(cfg.get("url") or "")
                        if not url and cfg.get("series_id"):
                            url=f"https://api.eia.gov/v2/seriesid/{cfg['series_id']}"
                        if not url.startswith("https://"): continue
                        params=dict(cfg.get("params") or {}); params["api_key"]=key
                        if cfg.get("series_id"):
                            params.setdefault("length","6")
                        result=await self.http.get(url,params=params,retries=2,max_body_bytes=16_000_000)
                        if result.ok:
                            obj=json.loads(result.body)
                            data=((obj.get("response") or {}).get("data") or []) if isinstance(obj,dict) else []
                            if cfg.get("series_id") and data:
                                latest=data[0]; previous=data[1] if len(data)>1 else {}
                                value=latest.get("value"); prior=previous.get("value")
                                value=float(value) if value not in (None,"") else None
                                prior=float(prior) if prior not in (None,"") else None
                                if value is None: raise RuntimeError(f"EIA series {cfg['series_id']} returned no numeric value")
                                event_type=str(cfg.get("event_type") or "EIA_SOURCE_UPDATE")
                                payload={"id":f"{cfg['series_id']}:{latest.get('period')}","series_id":cfg["series_id"],"name":cfg.get("name"),"asset_id":cfg.get("entity"),"entity":cfg.get("entity"),"record_type":event_type,"event_time":latest.get("period"),"period":latest.get("period"),"value":value,"actual":value,"previous":prior,"change":value-prior if prior is not None else None,"unit":latest.get("units") or (obj.get("response") or {}).get("units") or ""}
                                if "INVENTORY" in event_type:payload["inventory_surprise"]=payload["change"]
                                if "PRODUCTION" in event_type:payload["production_change"]=payload["change"]
                                await self.store.insert_snapshot(slug,"ENERGY_PLANT_OPERATIONS",event_type,payload)
                            else:
                                await self.store.insert_snapshot(slug,"ENERGY_PLANT_OPERATIONS",str(cfg.get("event_type") or "EIA_SOURCE_UPDATE"),{"name":cfg.get("name"),"entity":cfg.get("entity"),"data":obj})
                            await self.store.status(slug,"ok",result.status)
                        else: await self.store.status(slug,result.classification,result.status,result.error,result.classification=="rate_limited")
                except Exception as exc: await self.store.status(slug,"config_or_runtime_error",error=f"{type(exc).__name__}:{exc}"); self.metrics.inc("eia_route_error")
            try: await asyncio.wait_for(stop_event.wait(),timeout=float(os.getenv("EIA_POLL_SECONDS","300")))
            except asyncio.TimeoutError: continue

    async def _supervise_loop(self, name: str, factory: Callable[[], Awaitable[None]], stop_event: asyncio.Event) -> None:
        backoff = 1.0
        while not stop_event.is_set():
            try:
                await factory()
                if stop_event.is_set():
                    return
                self.metrics.inc("native_worker_unexpected_return")
                logger.error("native worker returned unexpectedly lane=%s worker=%s", self.lane, name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.inc("native_worker_crash")
                logger.exception("native worker crashed lane=%s worker=%s error=%s", self.lane, name, exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                backoff = min(60.0, backoff * 2.0)
                continue

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.public_native_enabled:
            logger.info("native public plane disabled lane=%s", self.lane)
            await self.http.close()
            await stop_event.wait()
            return

        specs: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        if self.lane == "official":
            for spec in self.official_specs():
                specs.append((spec.slug, lambda spec=spec: self._run_json_spec(spec, stop_event)))
            specs.append(("ecb", lambda: self._ecb_loop(stop_event)))
            specs.extend([
                ("treasury-rates", lambda: self._treasury_rates_loop(stop_event)),
                ("nyfed-reference-rates", lambda: self._nyfed_reference_rates_loop(stop_event)),
                ("gold-etf-holdings", lambda: self._gold_holdings_loop(stop_event)),
                ("world-gold-council", lambda: self._wgc_gold_loop(stop_event)),
                ("usaspending", lambda: self._usaspending_loop(stop_event)),
                ("fca-short", lambda: self._fca_short_loop(stop_event)),
                ("sec-fund-datasets", lambda: self._sec_fund_dataset_loop(stop_event)),
                ("sec-entity-map", lambda: self._sec_entity_loop(stop_event)),
            ])
        elif self.lane == "altdata":
            for spec in self.altdata_specs():
                specs.append((spec.slug, lambda spec=spec: self._run_json_spec(spec, stop_event)))
            specs.append(("dataset-watch", lambda: self._dataset_watch_loop(stop_event)))
            specs.extend([
                ("opensky", lambda: self._opensky_loop(stop_event)),
                ("eia-routes", lambda: self._eia_routes_loop(stop_event)),
            ])
        elif self.lane == "filings":
            specs.extend([
                ("filing-surfaces", lambda: self._filing_surface_loop(stop_event)),
                ("edinet", lambda: self._edinet_loop(stop_event)),
                ("companies-house-stream", lambda: self._companies_house_stream(stop_event)),
            ])
            if os.getenv("SEC_FTD_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}:
                specs.append(("sec-ftd", lambda: self._sec_ftd_loop(stop_event)))
        else:
            await self.http.close()
            raise ValueError(f"unknown native lane {self.lane}")

        self.tasks = {
            asyncio.create_task(self._supervise_loop(name, factory, stop_event), name=f"native-supervisor:{name}")
            for name, factory in specs
        }
        self.metrics.set("native_tasks", len(self.tasks))
        logger.info("native public service started lane=%s tasks=%s", self.lane, len(self.tasks))
        try:
            await stop_event.wait()
        finally:
            for task in self.tasks:
                task.cancel()
            results = await asyncio.gather(*self.tasks, return_exceptions=True)
            unexpected = [x for x in results if isinstance(x, BaseException) and not isinstance(x, asyncio.CancelledError)]
            if unexpected:
                self.metrics.inc("native_shutdown_task_errors", len(unexpected))
                for exc in unexpected:
                    logger.error("native supervisor task terminated with error lane=%s error=%s", self.lane, exc)
            await self.http.close()
