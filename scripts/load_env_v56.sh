#!/usr/bin/env bash
# Source this file from every V56 operational entrypoint.
set -Eeuo pipefail
V56_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
V56_ENV_FILE="${V56_ENV_FILE:-$V56_ROOT/.env}"
if [[ -f "$V56_ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$V56_ENV_FILE"
  set +a
fi
export V56_PACKAGE_ROOT="${V56_PACKAGE_ROOT:-$V56_ROOT}"
export V52_PACKAGE_ROOT="${V52_PACKAGE_ROOT:-$V56_PACKAGE_ROOT}"
export PYTHONPATH="$V56_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

