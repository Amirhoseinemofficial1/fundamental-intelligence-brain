# V52 Operations

## Installation

Recommended host: current Debian/Ubuntu, Python 3.11+.

```bash
sudo apt update
sudo apt install -y python3 python3-venv ca-certificates unzip
unzip V52_REALTIME_FUNDAMENTAL_INTELLIGENCE_FULL_COMPLETE_FINAL_20260824.zip
cd V52_REALTIME_FUNDAMENTAL_INTELLIGENCE_FULL_COMPLETE_FINAL_20260824
cp .env.example .env
```

Install dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip check
```

## Preflight

Local/config/storage check:

```bash
./bin/preflight_V52.sh
```

Production target network check:

```bash
PREFLIGHT_STRICT_NETWORK=1 ./bin/preflight_V52.sh
```

Preflight validates imports, exact `.env.example`/schema key parity, reference JSON assets, SQLite WAL, `fdatasync`, service names, required Telegram secrets when enabled, configuration ranges, and optional DNS reachability.

## systemd

```bash
sudo ./bin/install_systemd.sh
sudo nano /etc/v52-fundamental.env
sudo systemctl enable --now v52-fundamental.service
```

Useful commands:

```bash
sudo systemctl status v52-fundamental --no-pager
sudo journalctl -u v52-fundamental -f
sudo systemctl restart v52-fundamental
sudo systemctl stop v52-fundamental
```

Runtime JSON logs and status artifacts are under `/var/lib/v52-fundamental`.

## Mandatory production diagnostic

Run after initial deployment and after material network/config changes:

```bash
cd /opt/v52-fundamental
sudo -u v52fund ./bin/run_V52_20min_diagnostic.sh
```

If your env contains shell-special characters, do **not** use the inline `env $(...)` form; source a protected shell-compatible env or run with systemd environment instead.

The diagnostic checks:

- Supervisor operational success.
- Every service wrote `final.json`.
- No service fatal.
- Zero unexpected restarts.
- Zero forced shutdown kills.
- Zero stale-heartbeat recoveries during a healthy diagnostic.
- Exact durable raw-news accounting for that `run_id`.
- Unique/material news volume.
- Material-news detection-age ratios.
- Translation backlog growth.
- No new dead translation/alert jobs.
- Exact market raw and semantic persistence invariants.
- No unresolved persisted market recovery state.
- CPU PSI resource threshold and parse-clean resource samples.

## Provider failures

V52 does not pretend every public site is permanently healthy. Provider errors are classified (timeout, DNS/connect, 429/rate-limited, blocked/auth, server error, malformed response, etc.) and written to metrics/source health. Backoff and circuit behavior prevents one bad provider from monopolizing the hot path.

## Recovery

### News processor crash
Leased raw rows are not deleted. When the lease expires another processor instance can reclaim them.

### Translation/alert/enrichment crash
Jobs remain durable and are reclaimed after their lease expires.

### Market process crash
Already synced raw journal data and semantic SQLite rows remain. Sequence state is persisted per stream. Supervisor restart is bounded by the restart budget.

### Disk full / writer error
Persistence errors are fatal to the owning service. They are not silently treated as success. The Supervisor then applies recovery/restart policy; repeated failures exhaust the budget and fail the run.

## Diagnostic bundle

A successful diagnostic creates a compressed bundle plus checksum under:

```text
$DATA_DIR/diagnostic_bundles/
```

It includes run artifacts, redacted structured logs, public settings, and a per-file SHA-256 manifest. `.env` is never copied.
