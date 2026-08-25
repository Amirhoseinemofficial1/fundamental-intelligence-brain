#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "$0")" && pwd)/_common.sh"
"$PY" -m v52.diagnostics.preflight
exec "$PY" -m v52.runtime.supervisor "$@"
