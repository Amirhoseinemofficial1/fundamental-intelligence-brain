#!/usr/bin/env bash
set -Eeuo pipefail
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files v57-fundamental.service >/dev/null 2>&1; then
  exec systemctl stop v57-fundamental.service
fi
pkill -TERM -f 'python.*v52.runtime.supervisor' 2>/dev/null || true
