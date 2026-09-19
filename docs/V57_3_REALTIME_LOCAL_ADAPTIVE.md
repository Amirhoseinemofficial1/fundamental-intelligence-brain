# V57.3 realtime local adaptive release

This revision responds to measured production growth. The host investigation found
that V57 stopped cleanly under its disk guard, while an unrelated Order Book research
job produced about 94 GB under `/root/rot_bot/csv_archives`. V57 itself reached about
2.4 GB in roughly nine hours, so this release also closes its internal amplification
paths instead of relying only on the host cleanup.

## Data admission and latency

- The production profile polls GDELT and top Google feeds every 10 seconds.
- A shared SQLite admission transaction limits the pending queue to 5,000 rows and
  each source to 100 new rows per five minutes.
- Processing leases newest high-priority rows first.
- News below materiality 4, untimed news below 7, and timed news older than one hour
  are released from transport without entering the story or learning ledgers.
- The cold sitemap plane is disabled in production. Its implementation remains
  available for controlled backfills and now applies the same fundamental filter.

## Local text learning

Paid OpenAI semantics are hard-disabled in code and in the production profile. A
stale API key or environment override cannot turn the paid path back on. Material
news receives a deterministic taxonomy analysis and a normalized 64-dimensional
multilingual text vector built from title/summary tokens, field-aware n-grams,
negation scopes, character fragments, event type and mapped assets. These vectors
feed the existing 76-input, 32-hidden-unit adaptive bandit. Delayed market/barrier
outcomes update the model under the existing observation, shadow, calibration,
champion/challenger and rollback gates.

This is an online neural learner, not a local general-purpose LLM. Its output remains
probabilistic and is prevented from becoming live solely because time elapsed.

## Storage bounds

- Mutable structured entities are written only on real A-to-B state transitions;
  repeated unchanged polls create no row. A-to-B-to-A is preserved.
- Feature JSON omits numeric zeroes and caps duplicate provenance at 128 entries.
- Observation mode creates one representative horizon per accepted event and no
  response targets for news below the prediction importance floor.
- Completed raw news is retained for 30 minutes, stale pending transport for 15
  minutes, control stories and structured snapshots for six hours, market semantic
  rows for 12 hours, and reference prices for three days.
- Structured JSON logs rotate at 8 MiB and resource samples are written every 30
  seconds while heartbeat checks continue every five seconds.
- The hard V57 data guard stops the service above 4 GiB and still protects minimum
  filesystem free space.

## Clean deployment

The deploy script removes only the audited V57 application and data paths, installs
the immutable V57.3 payload, creates a fresh environment, runs preflight, and starts
`v57-fundamental.service`. Data belonging to other bots is outside that deletion
scope and must be reviewed separately.
