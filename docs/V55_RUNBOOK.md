# V55 Run-Ready Operations

## Clean Debian/Ubuntu install

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip unzip
unzip V55_TRUE_AUTONOMOUS_MULTI_ASSET_FUNDAMENTAL_INTELLIGENCE_BRAIN_RUN_READY_FINAL.zip
cd V55_TRUE_AUTONOMOUS_MULTI_ASSET_FUNDAMENTAL_INTELLIGENCE_BRAIN_RUN_READY_FINAL
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-full.txt
.venv/bin/pip check
cp .env.example .env
```

Set `HTTP_USER_AGENT`, provider credentials that are actually licensed, and the
Telegram credentials if Telegram is enabled. Then run:

```bash
bash scripts/preflight_v55.sh
bash scripts/smoke_v55.sh
bash scripts/status_v55.sh
bash scripts/run_v55.sh
```

The first 24 accumulated process-runtime hours are `OBSERVATION`; hours 24–48
are `SHADOW`. Downtime and replay do not advance these clocks. `LIVE` remains
disabled after 48 hours unless Meta and required specialist champions, unseen
shadow evidence, outcome minimums, calibration, drift, and data-health gates all
pass. Bootstrap artifacts never satisfy that gate.

## Service management

```bash
sudo bash scripts/install_systemd_v55.sh
sudo systemctl start v55-fundamental.service
sudo systemctl status v55-fundamental.service
sudo journalctl -u v55-fundamental.service -f
sudo systemctl stop v55-fundamental.service
```

Installation enables the unit but does not start it. The unit uses
`/opt/v55-fundamental`, `/etc/v55-fundamental.env`, and writable state under
`/var/lib/v55-fundamental`.

## Diagnostics

```bash
.venv/bin/python -m v52.cli brain brain-status
.venv/bin/python -m v52.cli brain provider-status
.venv/bin/python -m v52.cli brain model-status
.venv/bin/python -m v52.cli brain shadow-status
.venv/bin/python -m v52.cli brain active-signals --limit 50
bash scripts/diagnostic_20m_v55.sh
bash scripts/validate_v55.sh
```

## Recovery

The supervisor restarts each failed child independently under a bounded restart
budget. Durable SQLite queues and lifecycle records are restart-safe. An
`EXIT_PENDING` signal is consumed by `active_signal_monitor`; a completed partial
exit persists `position_fraction`, records both `PARTIAL_EXIT` and the return to
`ACTIVE`, and remains continuously monitored. Use `active-signals` to inspect the
latest model artifact, action, remaining EV, price path, and position fraction.

