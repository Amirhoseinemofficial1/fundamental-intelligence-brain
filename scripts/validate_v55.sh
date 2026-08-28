#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export V52_PACKAGE_ROOT="${V52_PACKAGE_ROOT:-$ROOT}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
"$ROOT/.venv/bin/python" -m compileall -q "$ROOT/src"
"$ROOT/.venv/bin/python" -m v52.diagnostics.preflight
"$ROOT/.venv/bin/python" -m pytest -q "$ROOT/tests"
"$ROOT/.venv/bin/python" -m v52.cli brain active-signals --limit 20
