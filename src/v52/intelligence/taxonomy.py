from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .types import EventUnderstanding


@dataclass(frozen=True, slots=True)
class Taxon:
    event_type: str
    domain: str
    patterns: tuple[str, ...]
    importance: float
    priority: str
    horizons: tuple[int, ...]


TAXONOMY: tuple[Taxon, ...] = (
    Taxon("CENTRAL_BANK_DECISION", "MACRO", ("interest rate decision", "rate hike", "rate cut", "cuts interest rate", "raises interest rate", "federal reserve", "ecb decision", "bank of england", "boj decision"), .95, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400, 172800)),
    Taxon("INFLATION_RELEASE", "MACRO", ("consumer price index", " cpi ", "producer price index", " ppi ", "inflation rate"), .90, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400)),
    Taxon("EMPLOYMENT_RELEASE", "MACRO", ("nonfarm payroll", "payrolls", "unemployment rate", "jobless claims", "employment report"), .86, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400)),
    Taxon("GDP_RELEASE", "MACRO", ("gross domestic product", " gdp ", "economic growth"), .78, "P1", (60, 300, 900, 3600, 14400, 86400)),
    Taxon("PMI_RELEASE", "MACRO", ("purchasing managers index", " pmi ", "ism manufacturing", "ism services"), .72, "P1", (60, 300, 900, 3600, 14400)),
    Taxon("LIQUIDITY_POLICY", "MACRO", ("quantitative easing", "quantitative tightening", "balance sheet", "liquidity injection", "reserve requirement"), .85, "P1", (60, 300, 900, 3600, 14400, 86400, 172800)),
    Taxon("FX_INTERVENTION", "FOREX", ("currency intervention", "fx intervention", "yen intervention", "foreign exchange intervention"), .92, "P0", (30, 60, 180, 300, 900, 3600, 14400)),
    Taxon("WAR_ESCALATION", "GEOPOLITICAL", ("declares war", "military strike", "missile attack", "invasion", "troops enter", "air strike"), .98, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400, 172800)),
    Taxon("CEASEFIRE", "GEOPOLITICAL", ("ceasefire", "peace agreement", "truce agreement"), .90, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400)),
    Taxon("SANCTIONS", "GEOPOLITICAL", ("new sanctions", "imposes sanctions", "ofac designates", "sanctions package"), .90, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400)),
    Taxon("TARIFF_TRADE_RESTRICTION", "GEOPOLITICAL", ("new tariff", "raises tariffs", "export restriction", "export control", "trade restriction"), .84, "P1", (60, 300, 900, 3600, 14400, 86400)),
    Taxon("OPEC_DECISION", "ENERGY", ("opec", "production cut", "production increase", "output quota"), .94, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400)),
    Taxon("OIL_INVENTORY", "ENERGY", ("crude inventories", "oil inventory", "eia inventory", "api inventory"), .78, "P1", (60, 300, 900, 3600, 14400)),
    Taxon("ENERGY_SUPPLY_DISRUPTION", "ENERGY", ("pipeline shutdown", "refinery outage", "shipping disruption", "strait closed", "oil supply disruption"), .92, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400)),
    Taxon("EARNINGS", "EQUITIES", ("earnings report", "quarterly results", "beats estimates", "misses estimates", "earnings per share"), .80, "P1", (60, 300, 900, 3600, 14400, 86400)),
    Taxon("GUIDANCE", "EQUITIES", ("raises guidance", "cuts guidance", "withdraws guidance", "revenue outlook", "profit warning"), .86, "P1", (60, 300, 900, 3600, 14400, 86400)),
    Taxon("MERGER_ACQUISITION", "EQUITIES", ("acquire", "acquisition", "merger agreement", "takeover offer"), .88, "P1", (60, 300, 900, 3600, 14400, 86400)),
    Taxon("BUYBACK_DIVIDEND", "EQUITIES", ("share buyback", "stock repurchase", "dividend increase", "special dividend"), .70, "P2", (300, 900, 3600, 14400, 86400)),
    Taxon("REGULATORY_ACTION", "REGULATION", ("regulator fines", "sec charges", "antitrust lawsuit", "regulatory approval", "license revoked"), .86, "P1", (60, 300, 900, 3600, 14400, 86400)),
    Taxon("EXCHANGE_LISTING", "CRYPTO", ("will list", "spot listing", "listed on", "trading begins", "new listing"), .88, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400)),
    Taxon("EXCHANGE_DELISTING", "CRYPTO", ("will delist", "delisting", "remove trading pairs"), .92, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400)),
    Taxon("PROTOCOL_EXPLOIT", "CRYPTO", ("protocol exploit", "smart contract exploit", "bridge exploit", "funds drained", "security exploit"), .98, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400)),
    Taxon("EXCHANGE_HACK", "CRYPTO", ("exchange hacked", "exchange hack", "hot wallet compromised", "withdrawals suspended"), .98, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400)),
    Taxon("ETF_DECISION", "CRYPTO", ("etf approved", "etf rejected", "spot bitcoin etf", "spot ethereum etf", "etf filing"), .95, "P0", (30, 60, 180, 300, 900, 3600, 14400, 86400)),
    Taxon("TOKEN_UNLOCK", "CRYPTO", ("token unlock", "vesting unlock", "tokens unlocked"), .78, "P1", (300, 900, 3600, 14400, 86400, 172800)),
    Taxon("TOKEN_SUPPLY_CHANGE", "CRYPTO", ("token burn", "tokens burned", "token mint", "minted tokens"), .74, "P1", (60, 300, 900, 3600, 14400, 86400)),
    Taxon("WHALE_TRANSFER", "CRYPTO", ("whale transfer", "large transfer", "moved to exchange", "moved from exchange"), .70, "P1", (30, 60, 180, 300, 900, 3600, 14400)),
    Taxon("LIQUIDATION_EVENT", "CRYPTO", ("liquidations", "liquidated", "short squeeze", "long squeeze"), .82, "P0", (30, 60, 180, 300, 900, 3600)),
    Taxon("FUNDING_EXTREME", "CRYPTO", ("extreme funding", "funding extreme"), .74, "P1", (60, 180, 300, 900, 3600, 14400)),
    Taxon("MARKET_MOMENTUM_ANOMALY", "CRYPTO", ("crypto market anomaly", "market momentum anomaly"), .66, "P2", (60, 300, 900, 3600, 14400)),
    Taxon("PROTOCOL_UPGRADE", "CRYPTO", ("mainnet launch", "network upgrade", "hard fork", "governance proposal passed"), .70, "P2", (300, 900, 3600, 14400, 86400)),
)


ASSETS: dict[str, dict[str, str]] = {
    "BTC": {"symbol": "BTC", "name": "Bitcoin", "asset_class": "CRYPTO", "market_symbol": "BTCUSDT"},
    "ETH": {"symbol": "ETH", "name": "Ethereum", "asset_class": "CRYPTO", "market_symbol": "ETHUSDT"},
    "SOL": {"symbol": "SOL", "name": "Solana", "asset_class": "CRYPTO", "market_symbol": "SOLUSDT"},
    "XAUUSD": {"symbol": "XAUUSD", "name": "Gold", "asset_class": "COMMODITY", "market_symbol": "XAUUSD"},
    "WTI": {"symbol": "WTI", "name": "WTI Crude Oil", "asset_class": "COMMODITY", "market_symbol": "CL"},
    "BRENT": {"symbol": "BRENT", "name": "Brent Crude Oil", "asset_class": "COMMODITY", "market_symbol": "BRN"},
    "DXY": {"symbol": "DXY", "name": "US Dollar Index", "asset_class": "INDEX", "market_symbol": "DXY"},
    "EURUSD": {"symbol": "EURUSD", "name": "EUR/USD", "asset_class": "FOREX", "market_symbol": "EURUSD"},
    "USDJPY": {"symbol": "USDJPY", "name": "USD/JPY", "asset_class": "FOREX", "market_symbol": "USDJPY"},
    "SPX": {"symbol": "SPX", "name": "S&P 500", "asset_class": "INDEX", "market_symbol": "SPX"},
    "NDX": {"symbol": "NDX", "name": "Nasdaq 100", "asset_class": "INDEX", "market_symbol": "NDX"},
}


ASSET_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", " btc ", "$btc"),
    "ETH": ("ethereum", " ether ", " eth ", "$eth"),
    "SOL": ("solana", " sol ", "$sol"),
    "XAUUSD": ("gold", "bullion", "xau"),
    "WTI": ("wti", "crude oil", "oil prices"),
    "BRENT": ("brent",),
    "DXY": ("dollar index", "dxy", "us dollar"),
    "EURUSD": ("eur/usd", "euro dollar", "euro"),
    "USDJPY": ("usd/jpy", "yen", "japanese yen"),
    "SPX": ("s&p 500", "sp500", "wall street"),
    "NDX": ("nasdaq 100", "nasdaq", "technology stocks"),
}


ENTITY_ALIASES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "FED": ("Federal Reserve", "CENTRAL_BANK", ("federal reserve", " fed ", "fomc")),
    "ECB": ("European Central Bank", "CENTRAL_BANK", ("european central bank", " ecb ")),
    "BOE": ("Bank of England", "CENTRAL_BANK", ("bank of england", " boe ")),
    "BOJ": ("Bank of Japan", "CENTRAL_BANK", ("bank of japan", " boj ")),
    "OPEC": ("OPEC", "ORGANIZATION", ("opec", "opec+")),
    "SEC": ("US Securities and Exchange Commission", "REGULATOR", ("securities and exchange commission", " sec ")),
    "CFTC": ("US Commodity Futures Trading Commission", "REGULATOR", ("commodity futures trading commission", " cftc ")),
}


EVENT_ASSET_DEFAULTS: dict[str, tuple[tuple[str, str, float, int], ...]] = {
    "CENTRAL_BANK_DECISION": (("DXY", "MIXED", .82, 1), ("XAUUSD", "MIXED", .82, 1), ("SPX", "MIXED", .78, 1), ("NDX", "MIXED", .78, 1), ("EURUSD", "MIXED", .76, 1), ("USDJPY", "MIXED", .72, 2), ("WTI", "MIXED", .58, 2), ("BTC", "MIXED", .68, 2), ("ETH", "MIXED", .58, 3)),
    "INFLATION_RELEASE": (("DXY", "MIXED", .82, 1), ("XAUUSD", "MIXED", .80, 1), ("SPX", "MIXED", .72, 1), ("NDX", "MIXED", .76, 1), ("EURUSD", "MIXED", .70, 2), ("BTC", "MIXED", .62, 2), ("WTI", "MIXED", .54, 3)),
    "EMPLOYMENT_RELEASE": (("DXY", "MIXED", .78, 1), ("XAUUSD", "MIXED", .72, 1), ("SPX", "MIXED", .70, 1), ("NDX", "MIXED", .66, 2), ("EURUSD", "MIXED", .62, 2), ("BTC", "MIXED", .52, 3)),
    "LIQUIDITY_POLICY": (("SPX", "MIXED", .82, 1), ("NDX", "MIXED", .84, 1), ("DXY", "MIXED", .76, 1), ("BTC", "MIXED", .78, 1), ("ETH", "MIXED", .68, 2), ("XAUUSD", "MIXED", .70, 2), ("WTI", "MIXED", .52, 3)),
    "FX_INTERVENTION": (("USDJPY", "MIXED", .90, 1), ("DXY", "MIXED", .60, 2)),
    "WAR_ESCALATION": (("XAUUSD", "UP", .80, 1), ("WTI", "UP", .75, 1), ("BRENT", "UP", .75, 1), ("SPX", "DOWN", .68, 1), ("BTC", "MIXED", .52, 2)),
    "CEASEFIRE": (("XAUUSD", "DOWN", .65, 1), ("WTI", "DOWN", .62, 1), ("SPX", "UP", .64, 1)),
    "SANCTIONS": (("WTI", "UP", .62, 1), ("BRENT", "UP", .64, 1), ("XAUUSD", "UP", .58, 2), ("SPX", "DOWN", .56, 2)),
    "OPEC_DECISION": (("WTI", "MIXED", .90, 1), ("BRENT", "MIXED", .90, 1), ("SPX", "MIXED", .48, 2)),
    "OIL_INVENTORY": (("WTI", "MIXED", .86, 1), ("BRENT", "MIXED", .78, 1)),
    "ENERGY_SUPPLY_DISRUPTION": (("WTI", "UP", .82, 1), ("BRENT", "UP", .84, 1), ("SPX", "DOWN", .50, 2)),
    "ETF_DECISION": (("BTC", "MIXED", .92, 1), ("ETH", "MIXED", .78, 1)),
    "EXCHANGE_HACK": (("BTC", "DOWN", .66, 2), ("ETH", "DOWN", .60, 2)),
    "LIQUIDATION_EVENT": (("BTC", "MIXED", .78, 1), ("ETH", "MIXED", .72, 1)),
}


def _phrase_match(text: str, phrase: str) -> bool:
    phrase = phrase.strip()
    if not phrase:
        return False
    pattern = r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _matches_any(text: str, patterns: Iterable[str]) -> bool:
    return any(_phrase_match(text, p) for p in patterns)


def _normalized_tokens(text: str) -> list[str]:
    stop = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "says", "said", "report", "reports", "update", "breaking"}
    return [x for x in re.findall(r"[a-z0-9]{2,}", text.lower()) if x not in stop]


def _state(text: str) -> str:
    if _matches_any(text, ("resolved", "resolution", "reopened", "restored")):
        return "RESOLUTION"
    if _matches_any(text, ("escalation", "escalates", "intensifies", "expands attack")):
        return "ESCALATION"
    if _matches_any(text, ("correction", "corrected", "retracts", "retraction")):
        return "CORRECTION"
    if _matches_any(text, ("confirms", "confirmed", "official statement", "according to official")):
        return "CONFIRMATION"
    if _matches_any(text, ("escalates", "expands", "additional", "raises to", "increases to")):
        return "UPDATE"
    if _matches_any(text, ("follow up", "later said", "subsequently")):
        return "FOLLOW_UP"
    return "INITIAL"


def _central_bank_scope(text: str) -> str | None:
    scopes = {
        "FED": ("federal reserve", " fed ", "fomc"),
        "ECB": ("european central bank", " ecb "),
        "BOE": ("bank of england", " boe "),
        "BOJ": ("bank of japan", " boj "),
    }
    for scope, aliases in scopes.items():
        if _matches_any(text, aliases):
            return scope
    return None


def _direction_from_text(event_type: str, text: str, default: str, asset_id: str = "") -> str:
    if event_type == "CENTRAL_BANK_DECISION":
        scope = _central_bank_scope(text)
        hike = _matches_any(text, ("rate hike", "raises interest rate", "raised interest rate"))
        cut = _matches_any(text, ("rate cut", "cuts interest rate", "cut interest rate"))
        if scope == "FED" and (hike or cut):
            hike_up = {"DXY", "USDJPY"}
            return "UP" if (asset_id in hike_up) == hike else "DOWN"
        if scope == "ECB" and asset_id == "EURUSD" and (hike or cut):
            return "UP" if hike else "DOWN"
        if scope == "BOJ" and asset_id == "USDJPY" and (hike or cut):
            return "DOWN" if hike else "UP"
        return default
    positive = ("approved", "rate cut", "production cut", "ceasefire", "beats estimates", "raises guidance", "inflows")
    negative = ("rejected", "rate hike", "production increase", "hack", "exploit", "misses estimates", "cuts guidance", "outflows", "delist")
    pos = sum(_phrase_match(text, x) for x in positive)
    neg = sum(_phrase_match(text, x) for x in negative)
    if event_type in {"OPEC_DECISION", "OIL_INVENTORY", "INFLATION_RELEASE", "ETF_DECISION", "LIQUIDATION_EVENT"}:
        if pos > neg:
            return "UP"
        if neg > pos:
            return "DOWN"
    return default


def _surprise(text: str) -> float | None:
    match = re.search(r"(?:actual|rose|fell|at)\s+(-?\d+(?:\.\d+)?)%?.{0,80}?(?:expected|forecast|consensus)\s+(-?\d+(?:\.\d+)?)%?", text, flags=re.IGNORECASE)
    if not match:
        return None
    actual, expected = float(match.group(1)), float(match.group(2))
    scale = max(abs(expected), 1.0)
    return max(-3.0, min(3.0, (actual - expected) / scale))


def classify_story(story: dict[str, Any], historical_similarity: float = 0.0) -> EventUnderstanding:
    title = str(story.get("english_title") or story.get("title") or "").strip()
    summary = str(story.get("english_summary") or story.get("summary") or "").strip()
    text = f" {title} {summary} ".lower()
    matches = [t for t in TAXONOMY if _matches_any(text, t.patterns)]
    taxon = max(matches, key=lambda x: x.importance) if matches else Taxon(
        f"{str(story.get('category') or 'GENERAL').upper()}_EVENT", "GENERAL", (), .35, "P3", (300, 900, 3600, 14400, 86400)
    )
    entities: list[dict[str, str]] = []
    for entity_id, (name, kind, aliases) in ENTITY_ALIASES.items():
        if _matches_any(text, aliases):
            entities.append({"entity_id": entity_id, "name": name, "kind": kind})
    explicit_assets = {asset_id for asset_id, aliases in ASSET_ALIASES.items() if _matches_any(text, aliases)}
    defaults = EVENT_ASSET_DEFAULTS.get(taxon.event_type, ())
    if taxon.event_type == "CENTRAL_BANK_DECISION":
        scope = _central_bank_scope(text)
        if scope == "ECB": defaults = (("EURUSD", "MIXED", .84, 1),)
        elif scope == "BOJ": defaults = (("USDJPY", "MIXED", .84, 1),)
        elif scope == "BOE": defaults = ()
        elif scope != "FED": defaults = ()
    asset_rows: dict[str, dict[str, Any]] = {}
    for asset_id, direction, confidence, order in defaults:
        asset_rows[asset_id] = {
            **ASSETS[asset_id], "asset_id": asset_id, "impact_order": order,
            "directional_bias": _direction_from_text(taxon.event_type, text, direction, asset_id),
            "mapping_confidence": confidence, "mapping_reason": f"taxonomy:{taxon.event_type}",
        }
    for asset_id in explicit_assets:
        current = asset_rows.get(asset_id, {})
        asset_rows[asset_id] = {
            **ASSETS[asset_id], **current, "asset_id": asset_id,
            "impact_order": min(int(current.get("impact_order", 1)), 1),
            "directional_bias": current.get("directional_bias", "MIXED"),
            "mapping_confidence": max(float(current.get("mapping_confidence", 0.0)), .88),
            "mapping_reason": "explicit_mention",
        }
    if taxon.domain == "CRYPTO" and not asset_rows:
        asset_rows["BTC"] = {**ASSETS["BTC"], "asset_id": "BTC", "impact_order": 2, "directional_bias": "MIXED", "mapping_confidence": .45, "mapping_reason": "crypto_market_proxy"}
    source = str(story.get("source_name") or "").lower()
    url = str(story.get("source_url") or story.get("canonical_url") or "").lower()
    official = any(x in url for x in (".gov", "sec.gov", "federalreserve.gov", "ecb.europa.eu", "opec.org")) or "official" in source
    credibility = .92 if official else (.78 if any(x in source for x in ("reuters", "associated press", "bloomberg", "dow jones")) else .55)
    tokens = _normalized_tokens(title)
    semantic = " ".join(sorted(set(tokens))[:24]) or title.lower()
    # Six-hour bucket clusters cross-source reports while allowing later developments.
    observed = str(story.get("first_observed_at") or story.get("published_at") or "")
    bucket = observed[:10] + ":" + (observed[11:13] if len(observed) > 12 else "00")
    bucket_hour = 6 * (int(bucket[-2:]) // 6) if bucket[-2:].isdigit() else 0
    cluster_basis = f"{taxon.event_type}|{','.join(sorted(asset_rows))}|{semantic}|{bucket[:10]}:{bucket_hour:02d}"
    update_basis = f"{story.get('fingerprint')}|{title}|{summary[:500]}"
    novelty = max(.05, min(1.0, 1.0 - historical_similarity))
    importance = max(.0, min(1.0, taxon.importance * .75 + float(story.get("materiality") or 0) / 40.0))
    priority = "P0" if importance >= .88 else ("P1" if importance >= .68 else ("P2" if importance >= .45 else "P3"))
    relations: list[dict[str, str]] = []
    for entity in entities:
        for asset_id in asset_rows:
            relations.append({"source_id": entity["entity_id"], "relation": "AFFECTS", "target_id": asset_id})
    if "DXY" in asset_rows and "XAUUSD" in asset_rows:
        relations.append({"source_id": "DXY", "relation": "INVERSE_DRIVER", "target_id": "XAUUSD"})
    rationale = [f"taxonomy={taxon.event_type}", f"source_credibility={credibility:.2f}", f"novelty={novelty:.2f}"]
    if explicit_assets:
        rationale.append("explicit_assets=" + ",".join(sorted(explicit_assets)))
    return EventUnderstanding(
        event_type=taxon.event_type,
        domain=taxon.domain,
        event_state=_state(text),
        cluster_key=hashlib.sha256(cluster_basis.encode()).hexdigest(),
        update_key=hashlib.sha256(update_basis.encode()).hexdigest(),
        title=title,
        summary=summary,
        occurred_at=str(story.get("published_at") or story.get("first_observed_at") or ""),
        observed_at=str(story.get("first_observed_at") or ""),
        entities=tuple(entities),
        assets=tuple(asset_rows.values()),
        relations=tuple(relations),
        importance=importance,
        novelty=novelty,
        surprise=_surprise(text),
        source_credibility=credibility,
        priority=priority,
        expected_horizons=taxon.horizons,
        rationale=tuple(rationale),
        subtype=taxon.event_type,
        country=str(story.get('country') or story.get('region') or 'GLOBAL'),
        sector=str(story.get('sector') or ('ENERGY' if taxon.domain=='ENERGY' else 'TECHNOLOGY' if taxon.domain=='CRYPTO' else 'GENERAL')),
        priced_in_score=max(0.0,min(1.0,historical_similarity*.7 + (1.0-novelty)*.3)),
    )


def sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, value))))
