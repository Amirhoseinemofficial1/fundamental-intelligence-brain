#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export V52_PACKAGE_ROOT="${V52_PACKAGE_ROOT:-$ROOT}" PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$ROOT/.venv/bin/python" -m v52.runtime.supervisor --run-seconds "${V55_SMOKE_SECONDS:-30}"
