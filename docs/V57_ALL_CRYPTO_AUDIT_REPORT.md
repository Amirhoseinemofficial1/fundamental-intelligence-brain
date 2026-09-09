# V57 All-Crypto Audit and Runtime Upgrade

## Scope

This package merges the external `patch_v57_runtime_final.py` behavior into the V57 source tree and adds dynamic all-crypto coverage without replacing the existing intelligence, learning, qualification, durability, or anti-leakage architecture.

## What was preserved

- 35-service V57 supervisor/runtime architecture.
- Observation -> event -> feature -> specialists -> meta -> policy -> signal pipeline.
- Point-in-time timestamp contract and non-leaking outcome construction.
- Specialist/Meta Challenger -> Shadow -> Champion governance.
- 24h Observation + 24h Shadow qualification gate and fail-closed LIVE eligibility.
- Existing market durability, sequence recovery, raw journals, semantic SQLite writers, news/structured collectors, alert queues, and model registry.
- No broker/order execution was added. This remains an intelligence/signal system.

## Runtime hotfix now merged into the package

The release no longer depends on a separate external runtime patch for these fixes:

- Restart-safe structured cursor advancement.
- Structured high-frequency observations do not invoke OpenAI in the ingest hot path.
- GENERAL structured rows default to feature/learning flow rather than event fan-out.
- OpenAI failure cooldown.
- Stop-aware staged research loop and delayed expensive research stages.
- Telegram trade-lifecycle output is subject to the final LIVE gate.

## All-crypto universe

The hard-coded BTC/ETH/SUI market universe is no longer the authoritative crypto universe. Static symbols remain only as startup/failure fallbacks.

Low-frequency public directory discovery now covers these market directories:

- Binance USD-M futures/perpetual
- Binance Spot
- Bybit linear perpetual/futures
- Bybit Spot
- OKX SWAP
- OKX Spot
- MEXC futures
- MEXC Spot
- dYdX perpetuals
- Coinbase Spot
- Kraken Futures
- Kraken Spot
- Hyperliquid perpetuals

Every active token discovered across those directories is registered into the Brain asset universe. Provider-specific symbols are kept separately so spot and derivative symbols cannot be accidentally sent to the wrong venue protocol.

## Every-token screening vs deep streaming

Every discovered active token is screened on each universe refresh. The compact screen computes:

- number of venues
- number of markets
- primary venue/symbol
- available reference price
- maximum observed 24h quote volume
- average 24h change
- maximum absolute 24h change
- cross-venue price dispersion
- attention score
- HOT/WARM tier

The complete screen is persisted in `data/market/crypto_universe.json`.

Deep WebSocket order-book/trade/funding/liquidation streaming is intentionally bounded to a liquidity-ranked HOT tier. This is not a coverage restriction: WARM assets remain registered and are immediately eligible for full Brain analysis when selected by news, fundamental events, market anomalies, the active watchlist, or outcome evaluation. This avoids thousands of simultaneous order books and uncontrolled RAM/network usage.

Default HOT limit: `MARKET_CRYPTO_STREAM_LIMIT=200` per supported deep-stream venue. Set `0` only if the host and venue limits have been deliberately provisioned for an unbounded deep stream.

## Dynamic HOT refresh

Deep-stream sessions rotate in a controlled way every 3600 seconds by default (`MARKET_CRYPTO_STREAM_ROTATE_SECONDS=3600`). This refreshes liquidity-ranked symbols and lets newly listed/high-activity tokens enter the HOT tier without waiting for an accidental WebSocket disconnect. Set `0` to disable planned rotation.

Large subscription sets are batched/paced to reduce subscription-payload and request-rate failures.

## Market anomalies now reach the Brain

The old market alert path stored market data but suppressed intelligence handoff. It now creates durable internal Brain stories for significant market anomalies such as:

- large liquidation
- extreme funding
- configured large-trade anomalies
- all-universe 24h momentum anomalies

These internal events do **not** bypass LIVE qualification or directly force Telegram output.

## Dynamic token activation and reference prices

For any registered CEX token selected for full analysis, V57 can obtain public reference prices on demand without continuously polling every token. Spot reference data is preferred when available, then derivative sources are used as fallback. Supported on-demand spot paths include Binance Spot, Bybit Spot, OKX Spot, MEXC Spot, Coinbase and Kraken Spot; futures paths remain available for Binance/Bybit/OKX.

This gives long-tail tokens a usable price/outcome path while keeping background request volume bounded.

## Asset discovery scalability fix

The original dynamic asset resolver could regex-scan the entire asset table for each story. With thousands of registered crypto symbols that becomes expensive. CEX token matching now uses exact indexed SQL lookups for cashtags and uppercase token symbols in crypto-relevant context, while canonical equity/contract resolution keeps the existing deterministic path.

## Feature changes

Feature version is now `features-57.1.0-crypto-universe` for the upgraded path. Point-in-time-safe universe context includes:

- `asset_universe_venue_count`
- `asset_universe_quote_volume_24h_log`
- `asset_universe_change_24h`

If the current asset metadata was updated after a historical cutoff, these features fall back to zero rather than leaking future universe state.

## Taxonomy additions

The deterministic event taxonomy now recognizes:

- `FUNDING_EXTREME`
- `MARKET_MOMENTUM_ANOMALY`

so market-plane anomalies can route to CRYPTO specialists instead of remaining generic text.

## Test status

Final post-change validation was executed in isolated groups to avoid the environment's command timeout:

- Core V52->V57, learning, market-response, closure, release and all-crypto tests: **115 passed**
- Import/config/control/market protocol/durability/pipeline/preflight/quality/relevance capability tests: **30 passed**
- Runtime/security/shutdown/structured/supervisor/brain artifact/control/daily/expert/writer tests: **55 passed**
- Telegram format, true-system E2E and performance-stress tests: **5 passed**

Total collected and validated: **205 passed**.

The former legacy Telegram test was updated to the final fail-closed contract: EXIT/INVALIDATION rendering is tested with `live_gate=True`, and a separate test proves those trade-lifecycle messages are suppressed before LIVE.

## Environment limitation during audit

The analysis container blocked direct outbound connections to exchange REST endpoints, so a live network smoke scan could not be completed from this environment. Public endpoint shapes and current limits were checked against current official exchange documentation, and spot directory parsers were covered with deterministic mocked response-shape tests. Production preflight/network checks should still be run on the VPS.

## Important residual limits

1. **“All crypto” means all active markets discoverable from the configured supported CEX directories, plus DEX/contract tokens discovered through the existing event/contract resolver.** It does not attempt to enumerate every spam/minted token on every blockchain. That would be both unbounded and low-value.
2. A CEX symbol alone does not always prove chain/contract identity. If two unrelated assets share a ticker, V57 keeps deterministic/contract-aware resolution conservative rather than inventing an identity.
3. Premium per-symbol providers are not blindly queried for thousands of tokens. Their existing credentials/limits remain respected; the public universe + on-demand market path provides broad coverage.
4. Broad coverage is not the same as predictive edge. New tokens still require real outcomes, Shadow evidence, calibration, and Champion governance before V57 should trust learned behavior.
5. V57 still does not place live exchange orders.

## Recommended production defaults

```env
MARKET_CRYPTO_UNIVERSE_ENABLED='1'
MARKET_CRYPTO_UNIVERSE_REFRESH_SECONDS='900'
MARKET_CRYPTO_STREAM_LIMIT='200'
MARKET_CRYPTO_STREAM_ROTATE_SECONDS='3600'
MARKET_CRYPTO_ANOMALY_ENABLED='1'
MARKET_CRYPTO_ANOMALY_CHANGE_PCT='12'
MARKET_CRYPTO_ANOMALY_MIN_QUOTE_VOLUME_USD='1000000'
MARKET_CRYPTO_ANOMALY_MAX_PER_CYCLE='25'
```

These defaults make the entire active token universe visible to the Brain while keeping the most expensive microstructure feed bounded and refreshable.
