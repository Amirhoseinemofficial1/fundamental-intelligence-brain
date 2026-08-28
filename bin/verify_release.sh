#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f MANIFEST.sha256 ]]; then
  echo "MANIFEST.sha256 missing" >&2
  exit 2
fi
sha256sum -c MANIFEST.sha256

if [[ -n "${PYTHON:-}" ]] && "$PYTHON" -c 'import sys' >/dev/null 2>&1; then
  PY="$PYTHON"
elif command -v python3 >/dev/null 2>&1 && "$(command -v python3)" -c 'import sys' >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1 && "$(command -v python)" -c 'import sys' >/dev/null 2>&1; then
  PY="$(command -v python)"
else
  echo "an executable Python interpreter was not found" >&2
  exit 127
fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
"$PY" -m compileall -q src tests
for f in bin/*.sh; do bash -n "$f"; done

if "$PY" -c 'import pytest' >/dev/null 2>&1; then
  "$PY" -m pytest -q tests
else
  echo "pytest unavailable: install requirements-dev.txt to execute the test portion" >&2
  exit 3
fi

TMP_DATA="$(mktemp -d -t v54-release-verify-XXXXXX)"
trap 'rm -rf "$TMP_DATA"' EXIT
DATA_DIR="$TMP_DATA" \
TELEGRAM_ENABLED=0 TRANSLATION_ENABLED=0 MARKET_ENABLED=0 \
STRUCTURED_ENABLED=0 PUBLIC_NATIVE_ENABLED=0 DISCOVERY_ENABLED=0 \
NEWS_ENRICHMENT_ENABLED=0 PREFLIGHT_STRICT_NETWORK=0 \
"$PY" -m v52.diagnostics.preflight >/dev/null
DATA_DIR="$TMP_DATA/clean-exit" V52_PACKAGE_ROOT="$ROOT" TELEGRAM_ENABLED=0 MARKET_ENABLED=0 "$PY" -m v52.runtime.service active_signal_monitor --run-seconds 0.25 >/dev/null

echo "V55 release verification PASS"
