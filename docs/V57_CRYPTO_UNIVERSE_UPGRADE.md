# V57.1 Dynamic Crypto Universe Upgrade

This upgrade integrates the runtime hotfix and removes the three-symbol crypto coverage assumption. It adds multi-venue directory discovery, dynamic hot-tier WebSocket symbol selection, CEX aggregate asset registration, on-demand quote/history collection for activated long-tail assets, and market-anomaly handoff into the normal Brain learning path.

The design intentionally separates **coverage** from **deep streaming**: the full active directory is discoverable/analyzable, while the default deep stream tier is bounded to 200 symbols per venue to avoid runaway sockets, RAM, raw journals, and SQLite growth. Set `MARKET_CRYPTO_STREAM_LIMIT=0` only on hardware sized for every venue subscription.

No real-order execution was added. Telegram remains behind the existing LIVE qualification gate.


## All-token analysis contract

Every active token returned by the configured public venue directories is evaluated on every universe refresh. The compact screen measures cross-venue coverage, liquidity, 24h momentum, and cross-venue price dispersion and is written into `data/market/crypto_universe.json`. The top liquidity tier receives continuous microstructure WebSocket depth/trade/funding coverage; every other discovered token remains registered and can be promoted immediately to full Brain analysis by news, a fundamental event, a market anomaly, or on-demand outcome evaluation. This avoids pretending that thousands of simultaneous order books are free.

The deep-stream subscription code batches/paces large dynamic topic sets to reduce exchange rate-limit and payload failures. `MARKET_CRYPTO_STREAM_LIMIT=0` is supported as an explicit high-resource override, but is not the production default.


## Spot-only coverage

The directory plane also enumerates Binance Spot, Bybit Spot, OKX Spot, MEXC Spot and Kraken Spot in addition to perpetual/futures venues and Coinbase Spot. These directory providers are separate from derivative WebSocket collectors, so a spot-only symbol cannot accidentally be subscribed on a futures socket. On-demand reference pricing prefers spot when available.


## Hot-tier refresh

Dynamic exchange collectors perform a controlled stream rotation every 3600 seconds by default. This refreshes the liquidity-ranked HOT symbols while keeping connection churn far below exchange connection limits.
