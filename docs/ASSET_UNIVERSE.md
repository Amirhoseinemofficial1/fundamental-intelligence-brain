# Asset Universe

`DynamicAssetUniverse` persists metals, energy, FX, global indices, equities, crypto and tokens in `assets`. It resolves symbols, names and aliases, supports controlled discovery from ticker/exchange/chain/contract metadata, and preserves market symbols separately from canonical IDs. PLTR and SUI have behavioral mapping tests. Every event asset activates `active_asset_watchlist`; `MarketResponseService` polls it independently of collection and persists reference prices.

Unknown free text is never promoted blindly. Discovery requires an existing alias/symbol or a structured ticker matching the controlled syntax, with class inferred from exchange/chain/contract metadata.
