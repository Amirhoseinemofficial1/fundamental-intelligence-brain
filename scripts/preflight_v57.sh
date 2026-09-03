#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/load_env_v57.sh"
PY="${V57_PYTHON:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || { echo "missing venv python: $PY" >&2; exit 2; }
exec "$PY" -m v52.diagnostics.preflight
