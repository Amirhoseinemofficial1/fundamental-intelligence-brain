from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class ImpactRoute:
    score: int
    priority: str
    source_authority: int
    event_type: int
    cross_asset_scope: int
    surprise: int
    novelty: int
    urgency: int
    official_confirmation: int
    market_reaction: int
    systemic_risk: int
    matched_families: tuple[str, ...]
    relevant_experts: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


_OFFICIAL_HOSTS = (
    "federalreserve.gov", "newyorkfed.org", "ecb.europa.eu", "bankofengland.co.uk",
    "boj.or.jp", "pbc.gov.cn", "snb.ch", "treasury.gov", "bls.gov", "bea.gov",
    "eia.gov", "opec.org", "sec.gov", "cftc.gov", "fdic.gov", "imf.org",
)
_WIRE_NAMES = ("reuters", "associated press", "ap news", "bloomberg", "dow jones")

# name, lexical triggers, event score, cross-market score, systemic score, experts
_FAMILIES = (
    ("central_bank", ("fomc", "federal reserve", "emergency rate", "rate decision",
      "rate cut", "rate hike", "quantitative easing", "quantitative tightening",
      "liquidity injection", "ecb", "bank of england", "bank of japan", "pboc", "snb"),
     25, 15, 13, ("macro", "central_bank", "regime", "gold", "forex", "equity", "crypto", "liquidity")),
    ("macro", ("consumer price index", " cpi ", "personal consumption expenditures",
      " pce ", "nonfarm payroll", " nfp ", "unemployment rate", "gross domestic product",
      " gdp ", "purchasing managers", " pmi ", "retail sales", "jolts"),
     24, 15, 10, ("macro", "central_bank", "regime", "gold", "forex", "equity")),
    ("rates_fiscal", ("treasury yield", "bond yield", "yield shock", "yield curve",
      "real yield", "nominal yield", "effective fed funds", "fed target rate", "sofr",
      "sovereign default", "debt crisis", "fiscal crisis", "government shutdown", "debt ceiling"),
     23, 14, 14, ("macro", "central_bank", "regime", "gold", "forex", "equity", "liquidity")),
    ("geopolitical", ("declares war", "war escalation", "military strike", "missile attack",
      "invasion", "major attack", "ceasefire", "sanctions", "export restriction",
      "trade restriction", "tariff"),
     25, 15, 15, ("geopolitical", "macro", "regime", "oil", "energy", "gold", "forex", "equity")),
    ("energy", ("opec", "strategic petroleum reserve", "oil disruption", "oil inventory",
      "gasoline inventory", "distillate inventory", "oil production", "refinery utilization",
      "pipeline shutdown", "refinery outage", "shipping disruption", "strait closed", "production cut"),
     24, 13, 12, ("oil", "energy", "geopolitical", "macro", "regime")),
    ("systemic_financial", ("bank failure", "bank run", "liquidity crisis", "credit event",
      "systemic outage", "exchange outage", "withdrawals suspended", "clearing failure"),
     25, 15, 15, ("macro", "central_bank", "regime", "liquidity", "equity", "crypto", "derivatives")),
    ("crypto_critical", ("protocol exploit", "bridge exploit", "exchange hack", "funds drained",
      "liquidation cascade", "mass liquidation", "stablecoin depeg", "listing", "delisting",
      "will list", "trading begins", "token launch", "token launches", "launches on dex",
      "spot bitcoin etf", "spot ethereum etf",
      "etf approved", "etf rejected"),
     22, 8, 10, ("crypto", "token", "onchain", "derivatives", "liquidity", "pump_risk", "dump_risk")),
    ("crypto_market_anomaly", ("funding spike", "funding extreme", "funding rate",
      "orderbook imbalance", "order book imbalance", "aggressive buy", "aggressive sell",
      "unusual volume", "open interest spike", "whale netflow"),
     20, 8, 6, ("crypto", "token", "derivatives", "liquidity", "pump_risk", "dump_risk")),
    ("positioning", ("positioning zscore", "positioning net", "positioning weekly delta",
      "positioning four week delta", "cftc positioning"),
     16, 6, 4, ("gold", "oil", "forex", "equity", "regime")),
    ("gold_flows", ("gold etf holdings", "gold etf flow", "central bank gold reserves",
      "central bank gold purchases", "official gold reserve"),
     20, 8, 5, ("gold", "macro", "central_bank", "regime")),
    ("corporate", ("earnings", "guidance", "merger", "acquisition", "profit warning", "bankruptcy"),
     23, 7, 4, ("equity", "regulation", "news_novelty")),
)
_UNEXPECTED = ("unexpected", "unexpectedly", "surprise", "emergency", "unscheduled", "shock", "far above", "far below")
_URGENT = ("breaking", "just in", "emergency", "immediate", "halted", "suspended", "live")
_CONFIRMED = ("official statement", "confirmed", "announces", "decision", "release", "published")
_REACTION = ("yield spike", "yield plunge", "limit down", "limit up", "circuit breaker",
             "crashes", "surges", "plunges", "depeg")


def _contains(text: str, phrase: str) -> bool:
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    return re.search(r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)", text) is not None


def _numeric_surprise(story: dict[str, Any]) -> float | None:
    def number(key: str) -> float | None:
        try:
            value = story.get(key)
            return None if value in (None, "") else float(str(value).replace(",", "").replace("%", ""))
        except (TypeError, ValueError):
            return None

    actual = number("actual")
    expected = number("consensus")
    if expected is None:
        expected = number("forecast")
    if actual is not None and expected is not None:
        return abs(actual - expected) / max(abs(expected), 1.0)
    explicit = number("standardized_surprise")
    if explicit is None:
        explicit = number("surprise")
    return abs(explicit) if explicit is not None else None


def route_story(story: dict[str, Any]) -> ImpactRoute:
    title = str(story.get("english_title") or story.get("title") or "")
    summary = str(story.get("english_summary") or story.get("summary") or "")
    text = f" {title} {summary} {story.get('category') or ''} ".lower()
    source_name = str(story.get("source_name") or "").lower()
    source_url = str(story.get("source_url") or story.get("canonical_url") or "")
    host = (urlsplit(source_url).hostname or "").lower()
    official = bool(story.get("official")) or any(
        host == item or host.endswith("." + item) for item in _OFFICIAL_HOSTS
    ) or "official" in source_name
    verified_structured = bool(story.get("_structured_observation")) or urlsplit(source_url).scheme == "market"
    wire = any(name in source_name for name in _WIRE_NAMES)
    source_authority = 20 if official else 18 if verified_structured else 15 if wire else 8 if host else 4
    matched = [row for row in _FAMILIES if any(_contains(text, phrase) for phrase in row[1])]
    if matched:
        primary = max(matched, key=lambda row: (row[2], row[4], row[0]))
        event_type, cross_asset_scope, systemic_risk = primary[2], primary[3], primary[4]
        families = tuple(row[0] for row in matched)
        experts = tuple(dict.fromkeys(expert for row in matched for expert in row[5]))
    else:
        primary = ("general", (), 7, 3, 1, ("news_novelty", "regime"))
        event_type, cross_asset_scope, systemic_risk = 7, 3, 1
        families, experts = ("general",), ("news_novelty", "regime")
    numeric_surprise = _numeric_surprise(story)
    surprising = any(_contains(text, phrase) for phrase in _UNEXPECTED)
    surprise = 10 if surprising or (numeric_surprise is not None and numeric_surprise >= .5) else (
        7 if numeric_surprise is not None and numeric_surprise >= .2 else
        3 if numeric_surprise is not None else 0
    )
    novelty = 5 if surprising else 2 if any(_contains(text, word) for word in ("new", "first", "record")) else 0
    urgency = 5 if any(_contains(text, phrase) for phrase in _URGENT) else (
        3 if primary[0] in {"central_bank", "macro", "geopolitical", "systemic_financial"} else 0
    )
    official_confirmation = 5 if official else 4 if verified_structured else (
        3 if wire and any(_contains(text, phrase) for phrase in _CONFIRMED) else 0
    )
    market_reaction = 5 if any(_contains(text, phrase) for phrase in _REACTION) else 0
    materiality = max(0, min(10, int(story.get("materiality") or 0)))
    materiality_boost = 3 if materiality >= 9 else 2 if materiality >= 7 else 0
    score = min(100, source_authority + event_type + cross_asset_scope + surprise + novelty
                + urgency + official_confirmation + market_reaction + systemic_risk + materiality_boost)
    priority = "P0" if score >= 90 else "P1" if score >= 70 else "P2" if score >= 40 else "P3"
    reasons = (
        f"authority={source_authority}", f"event={event_type}", f"cross_asset={cross_asset_scope}",
        f"surprise={surprise}", f"systemic={systemic_risk}", "families=" + ",".join(families),
    )
    return ImpactRoute(
        score, priority, source_authority, event_type, cross_asset_scope, surprise, novelty,
        urgency, official_confirmation, market_reaction, systemic_risk, families, experts, reasons,
    )


def fanout_budget(priority: str, available_assets: int, available_horizons: int) -> tuple[int, int]:
    if priority == "P0":
        return max(0, available_assets), max(0, available_horizons)
    if priority == "P1":
        return min(5, max(0, available_assets)), min(4, max(0, available_horizons))
    if priority == "P2":
        return min(2, max(0, available_assets)), min(2, max(0, available_horizons))
    return 0, 0
