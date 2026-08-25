# V52 FULL — Restored Capability Matrix

This release keeps the clean V52 durability/supervision architecture **without dropping the production capabilities that existed in V50/V34–V37**. The following are runtime-active, not merely copied reference files.

## News and realtime social

- Direct priority/hot feeds and broad coverage feeds.
- **GDELT** fundamental query families.
- **Google News full mesh**: 78 locales, top feeds, topic feeds, publisher/site fallbacks.
- **X** Filtered Stream rule synchronization plus recent-search fallback when credentials are configured.
- **Bluesky Jetstream** with persisted cursor/reconnect handling.
- **Reddit**, **Mastodon**, and **NewsAPI** authenticated pollers when credentials are configured.
- **Sitemap + nested sitemap + news-sitemap** discovery/ingestion.
- V37 **365 parser profiles are used by runtime parsing**, not just counted by Preflight.
- Separate discovery plane so cold discovery cannot consume the hot-news execution lane.

## Structured / official / filings / alternative data

- Generic V36 free-provider registry runner with request/auth handling.
- V36 `<PROVIDER>_QUERIES_JSON` contracts for providers whose legal/query-specific endpoint must be configured by the operator.
- API-key, Bearer, Basic, static-header and OAuth client-credentials/password flows.
- Native public collectors including Federal Register/CFTC/BLS/Treasury/ECB/World Bank/FDIC/GLEIF/USGS/CISA/NWS/openFDA/ClinicalTrials and issuer filing surfaces.
- SEC current forms, company/entity maps, fund datasets, Fails-to-Deliver ZIP/row parsing, USAspending, FCA short-position data, OpenSky and configurable EIA route polling.
- **130 institutional connector environment contracts** restored from V34/V35/V36 registries, including authorized REST/WS and node-connector families.

## Smart money / institutional / prediction / whale intelligence

- Nansen smart-money flow adapter and configurable token queries.
- CryptoQuant, CoinGlass and Glassnode/configurable provider-flow contracts.
- Polymarket REST + market WebSocket and Kalshi REST.
- V33 Wall-Street/whale premium-action plane, including configurable Unusual Whales action endpoints and Whale Alert streaming when credentials are configured.
- EVM, Solana and Bitcoin node connector contracts from the institutional registries.

## Market

Independent services preserve/extend the V50 market durability contract for Bybit, OKX, Deribit, Binance Futures, MEXC Futures, dYdX, Coinbase, Kraken and Hyperliquid, plus global market/fear-greed polling in `market_core`.

Raw wire is persisted before semantic interpretation; per-venue semantic stores persist sequence/recovery checkpoints across restart.

## Reference assets preserved for coverage/auditability

- `fundamental_sources.json`
- `SOURCE_UNIVERSE_V36.json`
- `FREE_API_PROVIDER_REGISTRY_V36.json`
- `PROVIDER_CAPABILITY_MATRIX_V36.json`
- `FUNDAMENTAL_TAXONOMY_V36.json`
- `V36_LICENSE_PROVENANCE.json`
- `V37_PROFESSIONAL_NEWSWIRE_MESH.json`
- `V37_SOURCE_PARSER_REGISTRY.json`
- `SOURCE_DISCOVERY_SEEDS_V35.json`
- `V34_18_LAYER_IMPLEMENTATION.json`
- `V35_30_LAYER_IMPLEMENTATION.json`
- `V36_30_FINAL_GAP_CLOSURE_IMPLEMENTATION.json`
- `FREE_API_QUERY_EXAMPLES_V36.json`
- `WALL_STREET_WHALE_INTELLIGENCE_V33.json`

## Credential behavior

Capabilities requiring third-party credentials are **implemented but fail/disable visibly when credentials are absent**; they are not silently treated as successful. Public/no-key collectors continue independently. `.env.example` contains empty credential/query hooks—never real secrets.
