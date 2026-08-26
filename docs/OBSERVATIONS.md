# Canonical Observations and Routing

Every structured record is normalized to source/provider, event/available/ingestion time, assets/entities, market/family/type, value/unit/direction, quality/freshness and provenance. `event_time <= available_time <= ingestion_time` is enforced at construction and `available_time <= prediction_time` is enforced by the feature store.

`semantic_routing.py` uses provider, lane, record type, unit and metadata. The routing matrix behavior-tests CPI, real yields, rates, NFP, OPEC/EIA, earnings/SEC, open interest/funding/liquidations, whale/exchange/DEX flows, unlocks, listings and exploits against their specialist sets.
