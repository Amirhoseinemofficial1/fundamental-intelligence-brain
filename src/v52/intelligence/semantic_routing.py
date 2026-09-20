from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SemanticRoute:
    observation_family: str
    event_type: str
    domain: str
    subtype: str
    experts: tuple[str, ...]
    horizons: tuple[int, ...] = (30, 60, 180, 300, 900, 1800, 3600, 14400, 43200, 86400, 172800)


ROUTES: tuple[tuple[tuple[str, ...], SemanticRoute], ...] = (
    (("REAL_YIELD", "REAL YIELD"), SemanticRoute("MACRO", "REAL_YIELD", "MACRO", "RATES", ("macro", "central_bank", "gold"))),
    (("TREASURY_NOMINAL_YIELD", "EFFECTIVE_FED_FUNDS_RATE", "FED_TARGET_RATE", "SOFR", "OBFR", "TGCR", "BGCR"), SemanticRoute("MACRO", "RATES_UPDATE", "MACRO", "RATES", ("macro", "central_bank", "gold", "forex", "equity", "liquidity"))),
    (("YIELD_CURVE", "RATE_DIFFERENTIAL"), SemanticRoute("MACRO", "YIELD_CURVE_UPDATE", "MACRO", "RATES", ("macro", "central_bank", "gold", "forex", "equity", "regime"))),
    (("FED_RATE", "FOMC_RATE", "CENTRAL_BANK_RATE"), SemanticRoute("MACRO", "CENTRAL_BANK_DECISION", "MACRO", "POLICY_RATE", ("macro", "central_bank", "gold", "forex", "equity"))),
    (("CPI", "CONSUMER_PRICE"), SemanticRoute("MACRO", "INFLATION_RELEASE", "MACRO", "CPI", ("macro", "central_bank", "gold", "forex", "equity"))),
    (("PPI", "PRODUCER_PRICE"), SemanticRoute("MACRO", "INFLATION_RELEASE", "MACRO", "PPI", ("macro", "central_bank", "gold", "forex", "equity"))),
    (("NFP", "NONFARM", "PAYROLL"), SemanticRoute("MACRO", "EMPLOYMENT_RELEASE", "MACRO", "NFP", ("macro", "central_bank", "forex", "equity"))),
    (("UNEMPLOYMENT", "JOLTS", "HOURLY_EARNINGS", "WAGES"), SemanticRoute("MACRO", "EMPLOYMENT_RELEASE", "MACRO", "LABOR", ("macro", "central_bank", "forex", "equity"))),
    (("OPEC",), SemanticRoute("ENERGY", "OPEC_DECISION", "ENERGY", "SUPPLY_POLICY", ("oil", "energy", "geopolitical"))),
    (("EIA", "CRUDE_INVENTORY", "OIL_INVENTORY"), SemanticRoute("ENERGY", "OIL_INVENTORY", "ENERGY", "INVENTORY", ("oil", "energy", "macro"))),
    (("OIL_PRODUCTION", "REFINERY_UTILIZATION", "GASOLINE_INVENTORY", "DISTILLATE_INVENTORY"), SemanticRoute("ENERGY", "ENERGY_BALANCE_UPDATE", "ENERGY", "SUPPLY_DEMAND", ("oil", "energy", "macro"))),
    (("POSITIONING_", "CFTC_POSITIONING"), SemanticRoute("DERIVATIVES", "POSITIONING_UPDATE", "COMMODITIES", "CFTC", ("gold", "oil", "forex", "equity", "regime"))),
    (("GOLD_ETF_HOLDINGS", "GOLD_ETF_FLOW"), SemanticRoute("COMMODITY", "GOLD_FLOW_UPDATE", "COMMODITIES", "ETF_FLOW", ("gold", "macro", "regime"))),
    (("CENTRAL_BANK_GOLD_RESERVES", "CENTRAL_BANK_GOLD_PURCHASES"), SemanticRoute("COMMODITY", "OFFICIAL_GOLD_RESERVE_UPDATE", "COMMODITIES", "OFFICIAL_SECTOR", ("gold", "macro", "central_bank", "regime"))),
    (("EARNINGS", "EPS", "QUARTERLY_RESULTS"), SemanticRoute("CORPORATE", "EARNINGS", "EQUITIES", "EARNINGS", ("equity", "news_novelty"))),
    (("SEC_FILING", "FORM_10", "FORM_8", "EDGAR"), SemanticRoute("CORPORATE", "SEC_FILING", "EQUITIES", "FILING", ("equity", "regulation"))),
    (("OPEN_INTEREST",), SemanticRoute("DERIVATIVES", "OPEN_INTEREST", "CRYPTO", "POSITIONING", ("derivatives", "liquidity", "crypto"))),
    (("FUNDING",), SemanticRoute("DERIVATIVES", "FUNDING_RATE", "CRYPTO", "FUNDING", ("derivatives", "liquidity", "crypto"))),
    (("LIQUIDATION",), SemanticRoute("DERIVATIVES", "LIQUIDATION_EVENT", "CRYPTO", "LIQUIDATION", ("derivatives", "liquidity", "pump_risk", "dump_risk"))),
    (("WHALE_TRANSFER", "LARGE_TRANSFER"), SemanticRoute("ONCHAIN", "WHALE_TRANSFER", "CRYPTO", "WHALE", ("onchain", "whale", "crypto", "token"))),
    (("EXCHANGE_FLOW", "EXCHANGE_INFLOW", "EXCHANGE_OUTFLOW", "NETFLOW"), SemanticRoute("ONCHAIN", "EXCHANGE_FLOW", "CRYPTO", "EXCHANGE_FLOW", ("onchain", "whale", "liquidity", "crypto", "token"))),
    (("DEX_LIQUIDITY", "DEX_VOLUME", "LP_CHANGE"), SemanticRoute("ONCHAIN", "DEX_LIQUIDITY", "CRYPTO", "DEX", ("onchain", "liquidity", "token"))),
    (("TOKEN_UNLOCK", "VESTING"), SemanticRoute("TOKEN", "TOKEN_UNLOCK", "CRYPTO", "SUPPLY", ("token", "onchain", "dump_risk"))),
    (("LISTING", "EXCHANGE_LISTING"), SemanticRoute("TOKEN", "EXCHANGE_LISTING", "CRYPTO", "LISTING", ("listing", "token", "pump_risk", "liquidity"))),
    (("EXPLOIT", "HACK"), SemanticRoute("TOKEN", "PROTOCOL_EXPLOIT", "CRYPTO", "SECURITY", ("token", "regulation", "dump_risk", "liquidity"))),
)


def route_observation(provider: str, lane: str, record_type: str, unit: str = "", metadata: dict[str, Any] | None = None) -> SemanticRoute:
    flattened = " ".join((provider, lane, record_type, unit, str(metadata or {}))).upper()
    normalized = re.sub(r"[^A-Z0-9]+", "_", flattened)
    for needles, route in ROUTES:
        if any(re.sub(r"[^A-Z0-9]+", "_", needle.upper()).strip("_") in normalized for needle in needles):
            return route
    return SemanticRoute("GENERAL", record_type.upper() or "GENERAL_EVENT", "GENERAL", "GENERAL", ("news_novelty",), (300, 900, 3600, 14400, 86400))
