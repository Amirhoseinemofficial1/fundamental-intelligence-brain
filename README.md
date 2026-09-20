# V59 impact-routed structured-data brain

V59 preserves the complete collector and multi-asset capability set while
putting a deterministic global-impact router in front of expensive inference.
P0/P1 changes use a receipt-backed Fast-Lane independent of the durable FIFO
cursor; P2 is bounded to 2 assets by 2 horizons; P3 remains available for
archive, confirmation and learning without prediction fan-out. Official BLS,
CFTC, Treasury, New York Fed, EIA and gold data now enter the brain as typed,
asset-specific observations. See `docs/V59_IMPACT_ROUTER_DATA_PLANE.md`.

The stable Debian service and data paths remain `v57-fundamental` and
`/var/lib/v57-fundamental` so an in-place upgrade preserves operational state.

# V57.4 news quality and latency controls

V57.4 adds a single event and freshness admission rule across direct feeds,
Google News and GDELT, prevents aggregator-only or stale news from sending
time-sensitive trading signals, and provides a read-only production latency
report. See `docs/V57_4_NEWS_QUALITY_AND_LATENCY.md` for the measured baseline,
runtime acceptance targets, and the limits of a sub-minute target.

# V57.3 realtime local adaptive intelligence

V57.3 prevents unchanged structured-state duplication, rejects stale and low-value
news before durable learning, bounds every source at admission, and runs text
semantics locally with no paid OpenAI request path. It also stores sparse feature
vectors, limits observation targets to the prediction path, and shortens transport
retention so the server cannot repeat the previous growth pattern.

V57.2 fixed the production SQLite startup lock, prevented duplicate/high-volume
storage growth, and adds evidence embeddings to the delayed-feedback neural
network. Raw websocket journals are disabled in the bounded server profile;
market state is sampled and upserted in five-second buckets; structured and
news payloads are deduplicated, hashed and size-bounded; the shared news queue
has a transactional 20,000-row ceiling; retention and the systemd disk guard
cap unattended growth.

The first 24 running hours are observation and the next 24 are shadow learning.
After those 48 running hours, signals still remain closed until champion,
sample, calibration and drift qualification pass. Semantic OpenAI output is a
validated feature proposal; the local 76-input, 32-hidden-unit, seven-head
network learns only from delayed candle/barrier outcomes. Read
`docs/V57_3_REALTIME_LOCAL_ADAPTIVE.md` for the exact data and deployment
contract.

# V57.1 resource-bounded adaptive evaluation

This revision adds seven symmetric 1:1 paper-evaluation levels (0.1, 0.3, 0.5, 1, 3, 5, 10%), persisted OHLC evidence, ambiguity/gap rejection, delayed neural contextual-bandit feedback, and complete Telegram HTML reports with all source links. It bounds SQLite connections, training rows, model creation, native threads, streams and systemd resources.

Read `docs/V57_1_UPGRADE.md` for measurement conventions, validation, limitations and deployment. The neural bandit remains in SHADOW; improvement and profitability are not guaranteed. The existing observation/shadow/qualification gates remain active. Historical V57 closure claims below refer to that older release, not independent validation of this revision.

# V57 Final-Closure Fundamental Intelligence Brain

V57 is the repaired release built directly from the audited V56 artifact. It preserves the V56 active multi-asset learning brain while closing the remaining source-level defects found during independent review: structured batch expansion, point-in-time semantic context, real drift-remediation consumption, canonical equity identity, knowledge-wide BH-FDR, unit-safe Surprise history, stronger deterministic asset discovery, and a release verifier that executes real tests and hashes the actual final ZIP.

Raw news and raw market data remain internal. User-facing trading signals remain fail-closed during the accumulated 24-hour observation phase and the following 24-hour shadow phase, and they still require the configured champion/data/calibration/drift qualification gate after 48 actual running hours. Downtime, replay and backtests do not advance that live-runtime gate.

## Recommended deployment: fresh Debian VPS

Upload and extract:

```bash
unzip V57_TRUE_AUTONOMOUS_MULTI_ASSET_FUNDAMENTAL_INTELLIGENCE_BRAIN_ABSOLUTE_FINAL.zip
cd V57_TRUE_AUTONOMOUS_MULTI_ASSET_FUNDAMENTAL_INTELLIGENCE_BRAIN_FINAL_CLOSURE
```

Install the full production stack and systemd service:

```bash
sudo bash scripts/install_v57_debian.sh
```

The installer is idempotent and, on apt-based Debian/Ubuntu hosts, installs the required OS prerequisites (`python3`, `python3-venv`, `python3-pip`, CA certificates, compiler/runtime prerequisites), creates the `v57fund` service account, `/opt/v57-fundamental`, `/var/lib/v57-fundamental`, the production venv, full ML dependencies, `/etc/v57-fundamental.env`, permissions, and the systemd unit.

Edit only configuration/secrets — no source-code edits are required:

```bash
sudo nano /etc/v57-fundamental.env
```

At minimum, review every `replace-me`/empty credential in that file and configure the providers you intend to use, Telegram settings, OpenAI semantics if enabled, and a real operator contact in `HTTP_USER_AGENT`.

Run preflight as the service account:

```bash
sudo -u v57fund env \
  V57_ENV_FILE=/etc/v57-fundamental.env \
  V57_PACKAGE_ROOT=/opt/v57-fundamental \
  DATA_DIR=/var/lib/v57-fundamental \
  PYTHONPATH=/opt/v57-fundamental/src \
  /opt/v57-fundamental/scripts/preflight_v57.sh
```

Start and inspect:

```bash
sudo systemctl start v57-fundamental
sudo systemctl status v57-fundamental --no-pager -l
sudo journalctl -u v57-fundamental -f
```

Restart / stop:

```bash
sudo systemctl restart v57-fundamental
sudo systemctl stop v57-fundamental
```

The production data directory is `/var/lib/v57-fundamental`. The production environment file is `/etc/v57-fundamental.env`. The application directory is `/opt/v57-fundamental`.

## Local/offline validation

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements-full.txt
# Equivalent project extra: .venv/bin/python -m pip install '.[full]'
.venv/bin/python -m pip check
cp .env.example .env
# edit .env if desired
bash scripts/preflight_v57.sh
bash scripts/smoke_v57.sh
bash scripts/status_v57.sh
bash scripts/stop_v57.sh
```

All normal V57 shell entrypoints load the configured `.env` through `scripts/load_env_v57.sh`. Existing exported process variables take precedence where the Python configuration layer specifies that behavior.

To prove the shell environment path:

```bash
echo 'TEST_V57_ENV_VALUE=known-value' >> .env
bash scripts/run_v57.sh --check-env TEST_V57_ENV_VALUE
```

Expected output:

```text
known-value
```

## Release verification

Fast structural gate from an extracted release:

```bash
python scripts/build_manifest.py --verify
PYTHONPATH=src python scripts/orphan_audit.py
PYTHONPATH=src python scripts/verify_v57_completion.py
```

The **final** release gate intentionally does not trust a stored test-summary JSON. It executes compileall, the complete pytest suite, V57 closure tests, preflight and orphan audit, and hashes the actual ZIP supplied with `--artifact`:

```bash
PYTHONPATH=src python scripts/verify_v57_completion.py \
  --full --final \
  --artifact /path/to/V57_TRUE_AUTONOMOUS_MULTI_ASSET_FUNDAMENTAL_INTELLIGENCE_BRAIN_ABSOLUTE_FINAL.zip
```

The required terminal result is:

```text
LEVEL_A_FAILED=0
LEVEL_A_PENDING=0
LEVEL_A_IN_PROGRESS=0
V57_COMPLETION_GATE=PASS
```

Native Debian/systemd execution with the user's real credentials and real external providers, timed 20m/2h/6h/24h/48h validation, and 30-day real-market specialization remain live-environment validations; they are not fabricated by the offline release build.

## Release evidence

- `docs/REQUIREMENT_MATRIX.md` — historical 0–200 requirement matrix.
- `docs/V57_EXECUTION_LEDGER.md` — unified machine-generated reconciliation.
- `docs/V57_FINAL_REQUIREMENT_MATRIX.md` — final unified matrix.
- `artifacts/V57_EXECUTION_LEDGER.json` — machine-readable ledger.
- `scripts/verify_v57_completion.py` — executable final gate.
- `RELEASE_IDENTITY.json` and `MANIFEST.json` — artifact identity and payload integrity.

### Model-governance note
`MODEL_BOOSTING_MIN_ROWS=200` is the default evidence threshold before optional XGBoost/LightGBM/CatBoost challengers are evaluated. Core linear/Bayesian and scikit-learn candidates remain available earlier; this avoids pathological warm-up latency while preserving the full model family once enough resolved outcomes exist.
