#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/load_env_v56.sh"
exec "$ROOT/.venv/bin/python" -m v52.diagnostics.preflight "$@"

