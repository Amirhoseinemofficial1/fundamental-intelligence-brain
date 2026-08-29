#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=load_env_v56.sh
source "$ROOT/scripts/load_env_v56.sh"
if [[ "${1:-}" == "--check-env" ]]; then
  name="${2:?environment variable name required}"
  printenv "$name"
  exit 0
fi
args=()
if [[ -n "${V56_RUN_SECONDS:-}" ]]; then args+=(--run-seconds "$V56_RUN_SECONDS"); fi
exec "$ROOT/.venv/bin/python" -m v52.runtime.supervisor "${args[@]}" "$@"

