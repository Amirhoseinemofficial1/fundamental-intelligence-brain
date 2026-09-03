#!/usr/bin/env bash
set -Eeuo pipefail
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files v57-fundamental.service >/dev/null 2>&1; then
  systemctl --no-pager --full status v57-fundamental.service || true
else
  pgrep -af 'python.*v52.runtime.supervisor' || true
fi
