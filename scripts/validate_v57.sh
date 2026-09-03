#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/load_env_v57.sh"
PY="${V57_PYTHON:-$ROOT/.venv/bin/python}"
"$PY" -m pip check
"$PY" -m compileall -q "$ROOT/src" "$ROOT/tests"
"$PY" -m pytest -q "$ROOT/tests/test_v57_final_closure.py"
"$PY" "$ROOT/scripts/build_manifest.py" --verify
"$PY" -m v52.diagnostics.preflight
