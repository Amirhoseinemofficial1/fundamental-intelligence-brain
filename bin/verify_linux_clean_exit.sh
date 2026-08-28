#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="${PYTHONPATH:-src}"
V55_CLEAN_EXIT_TMP="$(mktemp -d)"
trap 'rm -rf -- "$V55_CLEAN_EXIT_TMP"' EXIT
export DATA_DIR="$V55_CLEAN_EXIT_TMP/data"
export V52_PACKAGE_ROOT="$(pwd)"
export TELEGRAM_ENABLED=0
export MARKET_ENABLED=0
python -m v52.runtime.service active_signal_monitor --run-seconds 0.25
python - <<'PY'
import threading
leaked=[thread.name for thread in threading.enumerate() if thread is not threading.main_thread() and not thread.daemon]
if leaked:
    raise SystemExit(f"leaked non-daemon threads: {leaked}")
print("LINUX_CLEAN_EXIT=PASS")
PY
