#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export V52_PACKAGE_ROOT="${V52_PACKAGE_ROOT:-$ROOT}" PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
"$ROOT/.venv/bin/python" -m v52.cli brain brain-status
"$ROOT/.venv/bin/python" -m v52.cli brain provider-status
"$ROOT/.venv/bin/python" -m v52.cli brain active-signals --limit 50
