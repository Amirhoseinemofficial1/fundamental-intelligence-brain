from __future__ import annotations

APP_NAME = "V59 Impact-Routed Fundamental Intelligence"
APP_VERSION = "59.0.0"
SCHEMA_VERSION = "59.0"
DEFAULT_SERVICES = (
    "news_hot",
    "gdelt",
    "google_mesh",
    "x_social",
    "bluesky",
    "social_pollers",
    "news_coverage",
    "sitemap",
    "news_processor",
    "intelligence",
    "market_response",
    "active_signal_monitor",
    "research",
    "news_enrichment",
    "translation",
    "alerts",
    "structured",
    "institutional_connectors",
    "provider_flows",
    "premium_actions",
    "prediction",
    "official_native",
    "altdata_native",
    "filings_native",
    "discovery",
    "market_bybit",
    "market_okx",
    "market_deribit",
    "market_binance",
    "market_mexc",
    "market_dydx",
    "market_coinbase",
    "market_kraken",
    "market_hyperliquid",
    "maintenance",
)
MARKET_SERVICES = tuple(x for x in DEFAULT_SERVICES if x.startswith("market_") and x != "market_response")
NEWS_RAW_PRODUCER_SERVICES = (
    "news_hot", "gdelt", "google_mesh", "x_social", "bluesky", "social_pollers", "news_coverage", "sitemap",
)
STRUCTURED_PRODUCER_SERVICES = (
    "structured", "institutional_connectors", "provider_flows", "premium_actions", "prediction", "official_native", "altdata_native", "filings_native",
)

# Shutdown is dependency-aware. All ingest producers stop first while the durable
# processor and downstream queues remain alive long enough to drain committed work.
SOURCE_PRODUCER_SERVICES = (
    *NEWS_RAW_PRODUCER_SERVICES,
    *STRUCTURED_PRODUCER_SERVICES,
    "discovery",
    *MARKET_SERVICES,
)
PROCESSOR_SERVICES = ("news_processor",)
DOWNSTREAM_SERVICES = ("intelligence", "market_response", "active_signal_monitor", "research", "news_enrichment", "translation", "alerts", "maintenance")
