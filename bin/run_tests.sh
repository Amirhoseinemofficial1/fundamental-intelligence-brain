#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "$0")" && pwd)/_common.sh"
if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
  echo "pytest is required for tests. Install: $PY -m pip install -r $ROOT/requirements-dev.txt" >&2
  exit 2
fi
exec "$PY" -m pytest -q "$ROOT/tests"
