#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/load_env_v56.sh"
"$ROOT/.venv/bin/python" -m v52.cli brain brain-status
"$ROOT/.venv/bin/python" -m v52.cli brain provider-status
"$ROOT/.venv/bin/python" -m v52.cli brain active-signals --limit 50

