from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


def _load_v57_dotenv() -> Path | None:
    """Load the canonical .env without overriding process/systemd values."""
    explicit = os.getenv("V57_ENV_FILE") or os.getenv("V56_ENV_FILE")
    roots = [Path(explicit)] if explicit else [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    for path in roots:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if not name or not name.replace("_", "A").isalnum() or name[0].isdigit():
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            os.environ.setdefault(name, value)
        return path.resolve()
    return None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    value = default if raw is None or raw.strip() == "" else int(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _float(name: str, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    raw = os.getenv(name)
    value = default if raw is None or raw.strip() == "" else float(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser().resolve()


@dataclasses.dataclass(frozen=True, slots=True)
class Settings:
    package_root: Path
    data_dir: Path
    log_level: str
    user_agent: str
    service_status_seconds: float
    service_stale_seconds: float
    shutdown_grace_seconds: float
    shutdown_drain_seconds: float
    sqlite_busy_timeout_ms: int
    sqlite_sync: str

    news_hot_concurrency: int
    news_coverage_concurrency: int
    news_discovery_concurrency: int
    news_hot_min_poll_seconds: float
    news_hot_max_poll_seconds: float
    news_coverage_min_poll_seconds: float
    news_coverage_max_poll_seconds: float
    news_http_total_timeout: float
    news_http_connect_timeout: float
    news_http_read_timeout: float
    news_max_body_bytes: int
    news_raw_backpressure_rows: int
    news_raw_batch: int
    news_raw_lease_seconds: float
    news_materiality_threshold: int
    news_alert_max_age_seconds: int
    news_enrichment_enabled: bool
    news_enrichment_concurrency: int
    news_enrichment_max_age_seconds: int
    news_google_fallback_enabled: bool
    news_google_tier0_poll_seconds: float

    # Restored V50 news/social planes, implemented as isolated V52-native services.
    gdelt_enabled: bool
    gdelt_concurrency: int
    gdelt_min_gap_seconds: float
    gdelt_timeout_seconds: float
    gdelt_max_records: int
    gdelt_timespan: str
    gdelt_poll_seconds: float
    google_mesh_enabled: bool
    google_mesh_concurrency: int
    google_mesh_per_host_limit: int
    google_mesh_min_gap_seconds: float
    google_top_poll_seconds: float
    google_topic_poll_seconds: float
    google_publisher_fallback_enabled: bool
    google_publisher_batch_domains: int
    google_publisher_when: str
    google_publisher_poll_seconds: float
    google_mesh_min_poll_seconds: float
    google_mesh_max_poll_seconds: float
    google_mesh_startup_spread_seconds: float
    sitemap_enabled: bool
    sitemap_concurrency: int
    sitemap_host_gap_seconds: float
    sitemap_timeout_seconds: float
    sitemap_max_body_bytes: int
    sitemap_candidates_per_site: int
    sitemap_max_items_per_file: int
    sitemap_nested_depth: int
    sitemap_nested_max: int
    sitemap_sites_per_cycle: int
    sitemap_cycle_seconds: float
    x_enabled: bool
    x_bearer_token: str
    x_sync_rules: bool
    x_recent_search_enabled: bool
    x_recent_search_poll_seconds: float
    x_min_request_gap_seconds: float
    x_handle_group_size: int
    x_rule_max: int
    bluesky_enabled: bool
    bluesky_jetstream_url: str
    bluesky_max_message_bytes: int
    bluesky_cursor_flush_seconds: float
    reddit_enabled: bool
    reddit_client_id: str
    reddit_client_secret: str
    reddit_poll_seconds: float
    mastodon_enabled: bool
    mastodon_base_url: str
    mastodon_access_token: str
    mastodon_poll_seconds: float
    newsapi_enabled: bool
    newsapi_key: str
    newsapi_poll_seconds: float

    translation_enabled: bool
    libretranslate_url: str
    translation_concurrency: int
    translation_min_gap_seconds: float
    translation_batch: int
    translation_lease_seconds: float
    translation_max_attempts: int
    translation_require_healthy_on_start: bool

    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_api_base: str
    telegram_per_chat_min_gap_seconds: float
    telegram_global_rps: float
    telegram_batch_items: int
    telegram_lease_seconds: float
    telegram_max_attempts: int

    structured_enabled: bool
    structured_concurrency: int
    structured_default_timeout: float
    public_native_enabled: bool
    public_native_concurrency: int

    institutional_enabled: bool
    institutional_concurrency: int
    institutional_min_gap_seconds: float
    institutional_timeout_seconds: float
    institutional_page_watch_enabled: bool
    institutional_page_watch_batch: int
    institutional_page_watch_seconds: float
    sec_current_forms_enabled: bool
    sec_current_forms_poll_seconds: float

    provider_flow_enabled: bool
    provider_flow_concurrency: int
    provider_flow_min_gap_seconds: float
    provider_flow_timeout_seconds: float
    provider_flow_poll_seconds: float
    nansen_api_key: str
    nansen_base: str
    nansen_smart_money_chains: tuple[str, ...]
    nansen_poll_seconds: float
    cryptoquant_token: str
    cryptoquant_base: str
    cryptoquant_exchanges: tuple[str, ...]
    cryptoquant_window: str
    cryptoquant_poll_seconds: float
    coinglass_api_key: str
    coinglass_base: str
    coinglass_symbols: tuple[str, ...]
    coinglass_whale_poll_seconds: float
    coinglass_balance_poll_seconds: float
    glassnode_api_key: str

    prediction_enabled: bool
    polymarket_enabled: bool
    polymarket_gamma_markets_url: str
    polymarket_poll_seconds: float
    polymarket_max_markets: int
    polymarket_ws_enabled: bool
    polymarket_market_ws: str
    polymarket_ws_subscribe_batch: int
    polymarket_ws_max_tokens: int
    polymarket_ws_emit_ms: int
    kalshi_enabled: bool
    kalshi_markets_url: str
    kalshi_poll_seconds: float
    kalshi_max_markets: int
    kalshi_page_limit: int

    discovery_enabled: bool
    discovery_max_sites_per_cycle: int
    discovery_cycle_seconds: float

    market_enabled: bool
    market_raw_queue_size: int
    market_raw_sync_interval_ms: int
    market_raw_sync_bytes: int
    market_semantic_batch: int
    market_crypto_universe_enabled: bool
    market_crypto_universe_refresh_seconds: float
    market_crypto_universe_venues: tuple[str, ...]
    market_crypto_stream_limit: int
    market_crypto_stream_rotate_seconds: float
    market_crypto_anomaly_enabled: bool
    market_crypto_anomaly_change_pct: float
    market_crypto_anomaly_min_quote_volume_usd: float
    market_crypto_anomaly_max_per_cycle: int
    market_symbols: tuple[str, ...]
    okx_instruments: tuple[str, ...]
    deribit_instruments: tuple[str, ...]
    dydx_markets: tuple[str, ...]
    coinbase_products: tuple[str, ...]
    kraken_products: tuple[str, ...]
    hyperliquid_coins: tuple[str, ...]
    mexc_symbols: tuple[str, ...]
    binance_symbols: tuple[str, ...]
    binance_aggtrade_enabled: bool
    dydx_hf_enabled: bool
    liquidity_depth: int
    bybit_orderbook_depth: int

    supervisor_services: tuple[str, ...]
    supervisor_restart_limit: int
    supervisor_restart_window_seconds: int
    diagnostic_min_unique_20m: int
    diagnostic_min_material_unique_20m: int
    diagnostic_min_under_60s_ratio: float
    diagnostic_min_under_15s_ratio: float
    diagnostic_max_translation_backlog_growth: int
    diagnostic_max_cpu_psi_avg10: float
    preflight_strict_network: bool

    @property
    def reference_dir(self) -> Path:
        return self.package_root / "data" / "reference"

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_v57_dotenv()
        package_root = Path(os.getenv("V57_PACKAGE_ROOT", os.getenv("V56_PACKAGE_ROOT", os.getenv("V52_PACKAGE_ROOT", Path(__file__).resolve().parents[2])))).resolve()
        default_data = "/var/lib/v57-fundamental" if os.name != "nt" else str(package_root / "data")
        data_dir = _path("DATA_DIR", default_data)
        services = _csv(
            "SUPERVISOR_SERVICES",
            "news_hot,gdelt,google_mesh,x_social,bluesky,social_pollers,news_coverage,sitemap,news_processor,intelligence,market_response,active_signal_monitor,research,news_enrichment,translation,alerts,"
            "structured,institutional_connectors,provider_flows,premium_actions,prediction,official_native,altdata_native,filings_native,discovery,"
            "market_bybit,market_okx,market_deribit,market_binance,market_mexc,market_dydx,market_coinbase,"
            "market_kraken,market_hyperliquid,maintenance",
        )
        settings = cls(
            package_root=package_root,
            data_dir=data_dir,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            user_agent=os.getenv("HTTP_USER_AGENT", "V59-Fundamental/59.0"),
            service_status_seconds=_float("SERVICE_STATUS_SECONDS", 5.0, 1.0, 60.0),
            service_stale_seconds=_float("SERVICE_STALE_SECONDS", 30.0, 5.0, 300.0),
            shutdown_grace_seconds=_float("SHUTDOWN_GRACE_SECONDS", 45.0, 5.0, 300.0),
            shutdown_drain_seconds=_float("SHUTDOWN_DRAIN_SECONDS", 8.0, 0.0, 120.0),
            sqlite_busy_timeout_ms=_int("SQLITE_BUSY_TIMEOUT_MS", 15000, 1000, 120000),
            sqlite_sync=os.getenv("SQLITE_SYNCHRONOUS", "FULL").upper(),
            news_hot_concurrency=_int("NEWS_HOT_CONCURRENCY", 16, 1, 128),
            news_coverage_concurrency=_int("NEWS_COVERAGE_CONCURRENCY", 12, 1, 128),
            news_discovery_concurrency=_int("NEWS_DISCOVERY_CONCURRENCY", 6, 1, 64),
            news_hot_min_poll_seconds=_float("NEWS_HOT_MIN_POLL_SECONDS", 2.0, 0.5, 60.0),
            news_hot_max_poll_seconds=_float("NEWS_HOT_MAX_POLL_SECONDS", 30.0, 2.0, 300.0),
            news_coverage_min_poll_seconds=_float("NEWS_COVERAGE_MIN_POLL_SECONDS", 20.0, 5.0, 600.0),
            news_coverage_max_poll_seconds=_float("NEWS_COVERAGE_MAX_POLL_SECONDS", 180.0, 10.0, 3600.0),
            news_http_total_timeout=_float("NEWS_HTTP_TOTAL_TIMEOUT", 12.0, 2.0, 120.0),
            news_http_connect_timeout=_float("NEWS_HTTP_CONNECT_TIMEOUT", 3.0, 0.5, 30.0),
            news_http_read_timeout=_float("NEWS_HTTP_READ_TIMEOUT", 8.0, 1.0, 120.0),
            news_max_body_bytes=_int("NEWS_MAX_BODY_BYTES", 4_000_000, 100_000, 50_000_000),
            news_raw_backpressure_rows=_int("NEWS_RAW_BACKPRESSURE_ROWS", 250_000, 1_000, 5_000_000),
            news_raw_batch=_int("NEWS_RAW_BATCH", 100, 1, 2000),
            news_raw_lease_seconds=_float("NEWS_RAW_LEASE_SECONDS", 60.0, 10.0, 600.0),
            news_materiality_threshold=_int("NEWS_MATERIALITY_THRESHOLD", 3, 0, 20),
            news_alert_max_age_seconds=_int("NEWS_ALERT_MAX_AGE_SECONDS", 900, 30, 86_400),
            news_enrichment_enabled=_bool("NEWS_ENRICHMENT_ENABLED", True),
            news_enrichment_concurrency=_int("NEWS_ENRICHMENT_CONCURRENCY", 3, 1, 32),
            news_enrichment_max_age_seconds=_int("NEWS_ENRICHMENT_MAX_AGE_SECONDS", 3600, 60, 86_400),
            news_google_fallback_enabled=_bool("NEWS_GOOGLE_FALLBACK_ENABLED", True),
            news_google_tier0_poll_seconds=_float("NEWS_GOOGLE_TIER0_POLL_SECONDS", 30.0, 10.0, 600.0),
            gdelt_enabled=_bool("GDELT_ENABLED", True),
            gdelt_concurrency=_int("GDELT_CONCURRENCY", 6, 1, 32),
            gdelt_min_gap_seconds=_float("GDELT_MIN_GAP_SECONDS", 0.25, 0.0, 30.0),
            gdelt_timeout_seconds=_float("GDELT_TIMEOUT_SECONDS", 8.0, 2.0, 60.0),
            gdelt_max_records=_int("GDELT_MAX_RECORDS", 250, 10, 250),
            gdelt_timespan=os.getenv("GDELT_TIMESPAN", "15min").strip() or "15min",
            gdelt_poll_seconds=_float("GDELT_POLL_SECONDS", 30.0, 10.0, 600.0),
            google_mesh_enabled=_bool("GOOGLE_MESH_ENABLED", True),
            google_mesh_concurrency=_int("GOOGLE_MESH_CONCURRENCY", 24, 1, 96),
            google_mesh_per_host_limit=_int("GOOGLE_MESH_PER_HOST_LIMIT", 12, 1, 64),
            google_mesh_min_gap_seconds=_float("GOOGLE_MESH_MIN_GAP_SECONDS", 0.04, 0.0, 10.0),
            google_top_poll_seconds=_float("GOOGLE_TOP_POLL_SECONDS", 12.0, 5.0, 600.0),
            google_topic_poll_seconds=_float("GOOGLE_TOPIC_POLL_SECONDS", 24.0, 5.0, 1200.0),
            google_publisher_fallback_enabled=_bool("GOOGLE_PUBLISHER_FALLBACK_ENABLED", True),
            google_publisher_batch_domains=_int("GOOGLE_PUBLISHER_BATCH_DOMAINS", 6, 1, 20),
            google_publisher_when=os.getenv("GOOGLE_PUBLISHER_WHEN", "1h").strip() or "1h",
            google_publisher_poll_seconds=_float("GOOGLE_PUBLISHER_POLL_SECONDS", 30.0, 10.0, 3600.0),
            google_mesh_min_poll_seconds=_float("GOOGLE_MESH_MIN_POLL_SECONDS", 8.0, 2.0, 600.0),
            google_mesh_max_poll_seconds=_float("GOOGLE_MESH_MAX_POLL_SECONDS", 300.0, 30.0, 7200.0),
            google_mesh_startup_spread_seconds=_float("GOOGLE_MESH_STARTUP_SPREAD_SECONDS", 15.0, 0.0, 300.0),
            sitemap_enabled=_bool("SITEMAP_ENABLED", True),
            sitemap_concurrency=_int("SITEMAP_CONCURRENCY", 8, 1, 64),
            sitemap_host_gap_seconds=_float("SITEMAP_HOST_GAP_SECONDS", 0.25, 0.0, 30.0),
            sitemap_timeout_seconds=_float("SITEMAP_TIMEOUT_SECONDS", 12.0, 2.0, 120.0),
            sitemap_max_body_bytes=_int("SITEMAP_MAX_BODY_BYTES", 12_000_000, 100_000, 100_000_000),
            sitemap_candidates_per_site=_int("SITEMAP_CANDIDATES_PER_SITE", 6, 1, 30),
            sitemap_max_items_per_file=_int("SITEMAP_MAX_ITEMS_PER_FILE", 500, 10, 10_000),
            sitemap_nested_depth=_int("SITEMAP_NESTED_DEPTH", 2, 0, 5),
            sitemap_nested_max=_int("SITEMAP_NESTED_MAX", 20, 1, 500),
            sitemap_sites_per_cycle=_int("SITEMAP_SITES_PER_CYCLE", 300, 1, 5000),
            sitemap_cycle_seconds=_float("SITEMAP_CYCLE_SECONDS", 1800.0, 60.0, 86_400.0),
            x_enabled=_bool("X_ENABLED", True),
            x_bearer_token=os.getenv("X_BEARER_TOKEN", "").strip(),
            x_sync_rules=_bool("X_SYNC_RULES", True),
            x_recent_search_enabled=_bool("X_RECENT_SEARCH_ENABLED", True),
            x_recent_search_poll_seconds=_float("X_RECENT_SEARCH_POLL_SECONDS", 30.0, 10.0, 900.0),
            x_min_request_gap_seconds=_float("X_MIN_REQUEST_GAP_SECONDS", 1.0, 0.0, 60.0),
            x_handle_group_size=_int("X_HANDLE_GROUP_SIZE", 8, 1, 20),
            x_rule_max=_int("X_RULE_MAX", 25, 1, 100),
            bluesky_enabled=_bool("BLUESKY_ENABLED", True),
            bluesky_jetstream_url=os.getenv("BLUESKY_JETSTREAM_URL", "wss://jetstream.us-east.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents").strip(),
            bluesky_max_message_bytes=_int("BLUESKY_MAX_MESSAGE_BYTES", 4_000_000, 100_000, 50_000_000),
            bluesky_cursor_flush_seconds=_float("BLUESKY_CURSOR_FLUSH_SECONDS", 2.0, 0.2, 60.0),
            reddit_enabled=_bool("REDDIT_ENABLED", True),
            reddit_client_id=os.getenv("REDDIT_CLIENT_ID", "").strip(),
            reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", "").strip(),
            reddit_poll_seconds=_float("REDDIT_POLL_SECONDS", 60.0, 30.0, 3600.0),
            mastodon_enabled=_bool("MASTODON_ENABLED", False),
            mastodon_base_url=os.getenv("MASTODON_BASE_URL", "https://mastodon.social").rstrip("/"),
            mastodon_access_token=os.getenv("MASTODON_ACCESS_TOKEN", "").strip(),
            mastodon_poll_seconds=_float("MASTODON_POLL_SECONDS", 60.0, 15.0, 3600.0),
            newsapi_enabled=_bool("NEWSAPI_ENABLED", True),
            newsapi_key=os.getenv("NEWSAPI_KEY", "").strip(),
            newsapi_poll_seconds=_float("NEWSAPI_POLL_SECONDS", 120.0, 30.0, 3600.0),
            translation_enabled=_bool("TRANSLATION_ENABLED", True),
            libretranslate_url=os.getenv("LIBRETRANSLATE_URL", "http://127.0.0.1:5000").rstrip("/"),
            translation_concurrency=_int("TRANSLATION_CONCURRENCY", 2, 1, 16),
            translation_min_gap_seconds=_float("TRANSLATION_MIN_GAP_SECONDS", 0.25, 0.0, 10.0),
            translation_batch=_int("TRANSLATION_BATCH", 16, 1, 200),
            translation_lease_seconds=_float("TRANSLATION_LEASE_SECONDS", 90.0, 10.0, 900.0),
            translation_max_attempts=_int("TRANSLATION_MAX_ATTEMPTS", 20, 1, 100),
            translation_require_healthy_on_start=_bool("TRANSLATION_REQUIRE_HEALTHY_ON_START", False),
            telegram_enabled=_bool("TELEGRAM_ENABLED", True),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            telegram_api_base=os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
            telegram_per_chat_min_gap_seconds=_float("TELEGRAM_PER_CHAT_MIN_GAP_SECONDS", 1.05, 0.03, 60.0),
            telegram_global_rps=_float("TELEGRAM_GLOBAL_RPS", 20.0, 0.1, 30.0),
            # V52 intelligence messages are atomic decision records. One signal per
            # Telegram request prevents truncation and ambiguous delivery receipts.
            telegram_batch_items=_int("TELEGRAM_BATCH_ITEMS", 1, 1, 1),
            telegram_lease_seconds=_float("TELEGRAM_LEASE_SECONDS", 120.0, 10.0, 900.0),
            telegram_max_attempts=_int("TELEGRAM_MAX_ATTEMPTS", 20, 1, 100),
            structured_enabled=_bool("STRUCTURED_ENABLED", True),
            structured_concurrency=_int("STRUCTURED_CONCURRENCY", 6, 1, 64),
            structured_default_timeout=_float("STRUCTURED_DEFAULT_TIMEOUT", 15.0, 2.0, 120.0),
            public_native_enabled=_bool("PUBLIC_NATIVE_ENABLED", True),
            public_native_concurrency=_int("PUBLIC_NATIVE_CONCURRENCY", 8, 1, 64),
            institutional_enabled=_bool("INSTITUTIONAL_ENABLED", True),
            institutional_concurrency=_int("INSTITUTIONAL_CONCURRENCY", 16, 1, 96),
            institutional_min_gap_seconds=_float("INSTITUTIONAL_MIN_GAP_SECONDS", 0.05, 0.0, 30.0),
            institutional_timeout_seconds=_float("INSTITUTIONAL_TIMEOUT_SECONDS", 20.0, 2.0, 180.0),
            institutional_page_watch_enabled=_bool("INSTITUTIONAL_PAGE_WATCH_ENABLED", True),
            institutional_page_watch_batch=_int("INSTITUTIONAL_PAGE_WATCH_BATCH", 20, 1, 500),
            institutional_page_watch_seconds=_float("INSTITUTIONAL_PAGE_WATCH_SECONDS", 900.0, 60.0, 86_400.0),
            sec_current_forms_enabled=_bool("SEC_CURRENT_FORMS_ENABLED", True),
            sec_current_forms_poll_seconds=_float("SEC_CURRENT_FORMS_POLL_SECONDS", 120.0, 30.0, 3600.0),
            provider_flow_enabled=_bool("PROVIDER_FLOW_ENABLED", True),
            provider_flow_concurrency=_int("PROVIDER_FLOW_CONCURRENCY", 12, 1, 64),
            provider_flow_min_gap_seconds=_float("PROVIDER_FLOW_MIN_GAP_SECONDS", 0.05, 0.0, 10.0),
            provider_flow_timeout_seconds=_float("PROVIDER_FLOW_TIMEOUT_SECONDS", 20.0, 2.0, 120.0),
            provider_flow_poll_seconds=_float("PROVIDER_FLOW_POLL_SECONDS", 30.0, 1.0, 3600.0),
            nansen_api_key=os.getenv("NANSEN_API_KEY", "").strip(),
            nansen_base=os.getenv("NANSEN_BASE", "https://api.nansen.ai/api/v1").rstrip("/"),
            nansen_smart_money_chains=_csv("NANSEN_SMART_MONEY_CHAINS", "ethereum,solana,base,arbitrum,bnb"),
            nansen_poll_seconds=_float("NANSEN_POLL_SECONDS", 30.0, 5.0, 3600.0),
            cryptoquant_token=os.getenv("CRYPTOQUANT_TOKEN", "").strip(),
            cryptoquant_base=os.getenv("CRYPTOQUANT_BASE", "https://api.cryptoquant.com/v1").rstrip("/"),
            cryptoquant_exchanges=_csv("CRYPTOQUANT_EXCHANGES", "all_exchange"),
            cryptoquant_window=os.getenv("CRYPTOQUANT_WINDOW", "block").strip() or "block",
            cryptoquant_poll_seconds=_float("CRYPTOQUANT_POLL_SECONDS", 15.0, 5.0, 3600.0),
            coinglass_api_key=os.getenv("COINGLASS_API_KEY", "").strip(),
            coinglass_base=os.getenv("COINGLASS_BASE", "https://open-api-v4.coinglass.com").rstrip("/"),
            coinglass_symbols=_csv("COINGLASS_SYMBOLS", "BTC,ETH,SOL,SUI"),
            coinglass_whale_poll_seconds=_float("COINGLASS_WHALE_POLL_SECONDS", 5.0, 2.0, 3600.0),
            coinglass_balance_poll_seconds=_float("COINGLASS_BALANCE_POLL_SECONDS", 3600.0, 60.0, 86_400.0),
            glassnode_api_key=os.getenv("GLASSNODE_API_KEY", "").strip(),
            prediction_enabled=_bool("PREDICTION_ENABLED", True),
            polymarket_enabled=_bool("POLYMARKET_ENABLED", True),
            polymarket_gamma_markets_url=os.getenv("POLYMARKET_GAMMA_MARKETS_URL", "https://gamma-api.polymarket.com/markets").strip(),
            polymarket_poll_seconds=_float("POLYMARKET_POLL_SECONDS", 20.0, 5.0, 3600.0),
            polymarket_max_markets=_int("POLYMARKET_MAX_MARKETS", 1000, 20, 5000),
            polymarket_ws_enabled=_bool("POLYMARKET_WS_ENABLED", True),
            polymarket_market_ws=os.getenv("POLYMARKET_MARKET_WS", "wss://ws-subscriptions-clob.polymarket.com/ws/market").strip(),
            polymarket_ws_subscribe_batch=_int("POLYMARKET_WS_SUBSCRIBE_BATCH", 500, 20, 2000),
            polymarket_ws_max_tokens=_int("POLYMARKET_WS_MAX_TOKENS", 0, 0, 100_000),
            polymarket_ws_emit_ms=_int("POLYMARKET_WS_EMIT_MS", 250, 50, 60_000),
            kalshi_enabled=_bool("KALSHI_ENABLED", True),
            kalshi_markets_url=os.getenv("KALSHI_MARKETS_URL", "https://external-api.kalshi.com/trade-api/v2/markets").strip(),
            kalshi_poll_seconds=_float("KALSHI_POLL_SECONDS", 20.0, 5.0, 3600.0),
            kalshi_max_markets=_int("KALSHI_MAX_MARKETS", 3000, 100, 10_000),
            kalshi_page_limit=_int("KALSHI_PAGE_LIMIT", 1000, 100, 1000),
            discovery_enabled=_bool("DISCOVERY_ENABLED", True),
            discovery_max_sites_per_cycle=_int("DISCOVERY_MAX_SITES_PER_CYCLE", 250, 1, 5000),
            discovery_cycle_seconds=_float("DISCOVERY_CYCLE_SECONDS", 3600.0, 60.0, 86_400.0),
            market_enabled=_bool("MARKET_ENABLED", True),
            market_raw_queue_size=_int("MARKET_RAW_QUEUE_SIZE", 5_000, 1000, 2_000_000),
            market_raw_sync_interval_ms=_int("MARKET_RAW_SYNC_INTERVAL_MS", 250, 10, 10_000),
            market_raw_sync_bytes=_int("MARKET_RAW_SYNC_BYTES", 1_048_576, 4096, 128_000_000),
            market_semantic_batch=_int("MARKET_SEMANTIC_BATCH", 500, 1, 10_000),
            market_crypto_universe_enabled=_bool("MARKET_CRYPTO_UNIVERSE_ENABLED", True),
            market_crypto_universe_refresh_seconds=_float("MARKET_CRYPTO_UNIVERSE_REFRESH_SECONDS", 900.0, 60.0, 86_400.0),
            market_crypto_universe_venues=_csv("MARKET_CRYPTO_UNIVERSE_VENUES", "binance,binance_spot,bybit,bybit_spot,okx,okx_spot,mexc,mexc_spot,dydx,coinbase,kraken,kraken_spot,hyperliquid"),
            market_crypto_stream_limit=_int("MARKET_CRYPTO_STREAM_LIMIT", 12, 0, 5000),
            market_crypto_stream_rotate_seconds=_float("MARKET_CRYPTO_STREAM_ROTATE_SECONDS", 3600.0, 0.0, 86_400.0),
            market_crypto_anomaly_enabled=_bool("MARKET_CRYPTO_ANOMALY_ENABLED", True),
            market_crypto_anomaly_change_pct=_float("MARKET_CRYPTO_ANOMALY_CHANGE_PCT", 12.0, 1.0, 500.0),
            market_crypto_anomaly_min_quote_volume_usd=_float("MARKET_CRYPTO_ANOMALY_MIN_QUOTE_VOLUME_USD", 1_000_000.0, 0.0, 1_000_000_000_000.0),
            market_crypto_anomaly_max_per_cycle=_int("MARKET_CRYPTO_ANOMALY_MAX_PER_CYCLE", 25, 0, 500),
            market_symbols=_csv("MARKET_SYMBOLS", "BTCUSDT,ETHUSDT,SUIUSDT"),
            okx_instruments=_csv("OKX_INSTRUMENTS", "BTC-USDT-SWAP,ETH-USDT-SWAP,SUI-USDT-SWAP"),
            deribit_instruments=_csv("DERIBIT_INSTRUMENTS", "BTC-PERPETUAL,ETH-PERPETUAL"),
            dydx_markets=_csv("DYDX_MARKETS", "BTC-USD,ETH-USD,SUI-USD"),
            coinbase_products=_csv("COINBASE_PRODUCTS", "BTC-USD,ETH-USD,SUI-USD"),
            kraken_products=_csv("KRAKEN_PRODUCTS", "PI_XBTUSD,PI_ETHUSD"),
            hyperliquid_coins=_csv("HYPERLIQUID_COINS", "BTC,ETH,SUI"),
            mexc_symbols=_csv("MEXC_SYMBOLS", "BTC_USDT,ETH_USDT,SUI_USDT"),
            binance_symbols=_csv("BINANCE_SYMBOLS", "BTCUSDT,ETHUSDT,SUIUSDT"),
            binance_aggtrade_enabled=_bool("BINANCE_AGGTRADE_ENABLED", False),
            dydx_hf_enabled=_bool("DYDX_HIGH_FREQUENCY_STREAMS", False),
            liquidity_depth=_int("LIQUIDITY_DEPTH", 20, 5, 50),
            bybit_orderbook_depth=_int("BYBIT_ORDERBOOK_DEPTH", 50, 1, 1000),
            supervisor_services=services,
            supervisor_restart_limit=_int("SUPERVISOR_RESTART_LIMIT", 5, 0, 100),
            supervisor_restart_window_seconds=_int("SUPERVISOR_RESTART_WINDOW_SECONDS", 600, 30, 86_400),
            diagnostic_min_unique_20m=_int("DIAGNOSTIC_MIN_UNIQUE_20M", 300, 1, 100_000),
            diagnostic_min_material_unique_20m=_int("DIAGNOSTIC_MIN_MATERIAL_UNIQUE_20M", 100, 1, 100_000),
            diagnostic_min_under_60s_ratio=_float("DIAGNOSTIC_MIN_UNDER_60S_RATIO", 0.35, 0.0, 1.0),
            diagnostic_min_under_15s_ratio=_float("DIAGNOSTIC_MIN_UNDER_15S_RATIO", 0.10, 0.0, 1.0),
            diagnostic_max_translation_backlog_growth=_int("DIAGNOSTIC_MAX_TRANSLATION_BACKLOG_GROWTH", 50, 0, 1_000_000),
            diagnostic_max_cpu_psi_avg10=_float("DIAGNOSTIC_MAX_CPU_PSI_AVG10", 40.0, 0.0, 100.0),
            preflight_strict_network=_bool("PREFLIGHT_STRICT_NETWORK", False),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        errors: list[str] = []
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            errors.append(f"LOG_LEVEL unsupported: {self.log_level}")
        if self.sqlite_sync not in {"FULL", "EXTRA"}:
            errors.append("SQLITE_SYNCHRONOUS must be FULL or EXTRA for production durability")
        if self.news_hot_min_poll_seconds > self.news_hot_max_poll_seconds:
            errors.append("NEWS_HOT_MIN_POLL_SECONDS must be <= NEWS_HOT_MAX_POLL_SECONDS")
        if self.news_coverage_min_poll_seconds > self.news_coverage_max_poll_seconds:
            errors.append("NEWS_COVERAGE_MIN_POLL_SECONDS must be <= NEWS_COVERAGE_MAX_POLL_SECONDS")
        if self.service_stale_seconds <= self.service_status_seconds:
            errors.append("SERVICE_STALE_SECONDS must be greater than SERVICE_STATUS_SECONDS")
        if not self.supervisor_services:
            errors.append("SUPERVISOR_SERVICES cannot be empty")
        if self.bybit_orderbook_depth not in {1, 50, 200, 1000}:
            errors.append("BYBIT_ORDERBOOK_DEPTH must be one of 1,50,200,1000")
        endpoint_defaults = {
            "BYBIT_WS": "wss://stream.bybit.com/v5/public/linear",
            "OKX_PUBLIC_WS": "wss://ws.okx.com:8443/ws/v5/public",
            "DERIBIT_WS": "wss://www.deribit.com/ws/api/v2",
            "BINANCE_FUTURES_PUBLIC_WS": "wss://fstream.binance.com/public/stream",
            "BINANCE_FUTURES_MARKET_WS": "wss://fstream.binance.com/market/stream",
            "BINANCE_FUTURES_REST": "https://fapi.binance.com",
            "MEXC_FUTURES_WS": "wss://contract.mexc.com/edge",
            "DYDX_WS": "wss://indexer.dydx.trade/v4/ws",
            "COINBASE_WS": "wss://advanced-trade-ws.coinbase.com",
            "KRAKEN_FUTURES_WS": "wss://futures.kraken.com/ws/v1",
            "HYPERLIQUID_WS": "wss://api.hyperliquid.xyz/ws",
        }
        for name, default in endpoint_defaults.items():
            value = os.getenv(name, default).strip()
            parsed = urlparse(value)
            allowed = {"wss"} if name.endswith("_WS") or name in {"BYBIT_WS", "OKX_PUBLIC_WS", "DERIBIT_WS", "DYDX_WS", "COINBASE_WS", "HYPERLIQUID_WS"} else {"https"}
            if parsed.scheme not in allowed or not parsed.hostname:
                errors.append(f"{name} must use {sorted(allowed)} with a hostname")
        speed = os.getenv("BINANCE_DEPTH_SPEED", "500ms")
        if speed not in {"100ms", "500ms"}:
            errors.append("BINANCE_DEPTH_SPEED must be 100ms or 500ms")
        interval = os.getenv("DERIBIT_TICK_INTERVAL", "100ms")
        if interval not in {"raw", "100ms", "agg2"}:
            errors.append("DERIBIT_TICK_INTERVAL must be raw, 100ms, or agg2")
        if errors:
            raise ValueError("; ".join(errors))

    def ensure_directories(self) -> None:
        for p in (self.data_dir, self.run_dir, self.state_dir, self.log_dir):
            p.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["package_root"] = str(self.package_root)
        data["data_dir"] = str(self.data_dir)
        for key in (
            "telegram_bot_token", "x_bearer_token", "reddit_client_secret", "mastodon_access_token",
            "newsapi_key", "nansen_api_key", "cryptoquant_token", "coinglass_api_key", "glassnode_api_key",
        ):
            data[key] = "***" if data.get(key) else ""
        return data


def load_schema(package_root: Path) -> dict[str, Any]:
    path = package_root / "config" / "settings.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))
