#!/usr/bin/env bash
set -uo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "$0")" && pwd)/_common.sh"
set -e
"$PY" -m v52.diagnostics.preflight
RUN_ID="${V52_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-diag-$$}"
export V52_RUN_ID="$RUN_ID"
set +e
"$PY" -m v52.runtime.supervisor --run-seconds 1200
SUP_RC=$?
"$PY" -m v52.diagnostics.quality "$RUN_ID" --mode full
QUALITY_RC=$?
"$PY" -m v52.diagnostics.bundle "$RUN_ID"
BUNDLE_RC=$?
set -e
echo "V52_RUN_ID=$RUN_ID"
if (( SUP_RC != 0 || QUALITY_RC != 0 || BUNDLE_RC != 0 )); then
  echo "V52 diagnostic FAILED supervisor=$SUP_RC quality=$QUALITY_RC bundle=$BUNDLE_RC" >&2
  exit 1
fi
echo "V52 diagnostic PASS run_id=$RUN_ID"
