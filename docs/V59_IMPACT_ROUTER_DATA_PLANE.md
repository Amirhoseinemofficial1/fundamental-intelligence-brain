# V59 impact router and structured data plane

## Runtime contract

Collection coverage is retained. Every story revision is durably assigned a
deterministic global-impact score before heavyweight semantic, analogue or
model work. The score combines source authority, event family, cross-market
scope, surprise, novelty, urgency, confirmation, observed market reaction,
systemic risk and materiality.

| Score | Lane | Prediction fan-out |
|---:|---|---|
| 90-100 | P0 Fast-Lane | all deterministically relevant assets and horizons |
| 70-89 | P1 Fast-Lane | at most 5 assets and 4 horizons |
| 40-69 | P2 normal | at most 2 assets and 2 horizons |
| below 40 | P3 archive | no prediction fan-out |

P0 and P1 are selected independently of the normal `story_change_feed` FIFO.
A Fast-Lane receipt is committed only after processing succeeds. When the FIFO
later reaches that change, it can perform one deferred semantic/analogue
refinement but cannot repeat prediction fan-out. The FIFO cursor never jumps,
so older P2/P3 rows are not lost. An activation boundary prevents an in-place
upgrade from treating the entire historical database as newly arrived urgent
news.

Fast-Lane understanding is deterministic-first: taxonomy, source, surprise,
event clustering, affected assets and relevant-expert routing execute before
optional historical/semantic enrichment. Expert routing wakes only the event
families supported by evidence. Existing champion/challenger, point-in-time,
anti-leakage, replay, warmup and LIVE qualification gates remain in force.

## Structured observations

- BLS series IDs become typed CPI, unemployment, payroll, wages, PPI and JOLTS
  observations with current value, previous value and change.
- Treasury nominal and TIPS curves become tenor-specific yields plus 2s10s and
  5s30s spreads. NY Fed data provides EFFR, SOFR, OBFR, TGCR, BGCR and the Fed
  target midpoint when present.
- CFTC report families are transformed into asset- and participant-specific
  net position, weekly delta, four-week delta, percentile and z-score records.
  Raw generic reports stay in the structured source database and do not pollute
  the canonical learning stream.
- Five built-in EIA petroleum routes cover crude, gasoline and distillate
  inventories, crude production and refinery utilization. `EIA_ROUTES_JSON`
  can override the defaults; `EIA_API_KEY` remains required.
- SPDR GLD provides the primary no-key dated gold-holdings series from its
  official XLSX archive; iShares IAU is a secondary source because some
  datacenter networks receive an upstream 403. The World Gold Council workbook
  connector is authenticated and fail-closed when
  `WGC_AUTH_COOKIE` is absent; lack of that credential is never reported as
  live coverage.

## Validation

`tests/test_v59_impact_data_plane.py` includes the explicit 999-normal-story
backlog followed by a P0 FOMC emergency story. It proves direct P0 selection,
an unchanged normal cursor, persistent receipts and no duplicate Fast-Lane
eligibility. The same module verifies fan-out limits and every normalization
contract using deterministic fixtures.

Run the live read-only source check with:

```bash
PYTHONPATH=src .venv/bin/python scripts/validate_v59_data_plane.py
```

The command fails for broken required public contracts and reports EIA/WGC as
credential-skipped when their secrets are absent. It does not print secrets.

No implementation or short smoke test establishes profitability, sub-minute
upstream publication latency, or medical/financial certainty. Signals remain
probabilistic and governed by observation, shadow, OOS, calibration and
rollback controls.
