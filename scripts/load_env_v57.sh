#!/usr/bin/env bash
set -Eeuo pipefail
V57_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
V57_ENV_FILE="${V57_ENV_FILE:-$V57_ROOT/.env}"
if [[ -f "$V57_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$V57_ENV_FILE"
  set +a
fi
export V57_PACKAGE_ROOT="${V57_PACKAGE_ROOT:-$V57_ROOT}"
export V56_PACKAGE_ROOT="${V56_PACKAGE_ROOT:-$V57_PACKAGE_ROOT}"
export V52_PACKAGE_ROOT="${V52_PACKAGE_ROOT:-$V57_PACKAGE_ROOT}"
export PYTHONPATH="$V57_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
