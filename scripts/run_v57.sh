#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/load_env_v57.sh"
PY="${V57_PYTHON:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || { echo "missing venv python: $PY" >&2; exit 2; }
if [[ "${1:-}" == "--check-env" ]]; then key="${2:?environment variable name required}"; "$PY" -c 'import os,sys;print(os.environ.get(sys.argv[1],""))' "$key"; exit; fi
args=()
if [[ -n "${V57_RUN_SECONDS:-}" ]]; then args+=(--run-seconds "$V57_RUN_SECONDS"); fi
exec "$PY" -m v52.runtime.supervisor "${args[@]}"
