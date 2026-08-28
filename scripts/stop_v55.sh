#!/usr/bin/env bash
set -Eeuo pipefail
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet v55-fundamental.service; then
  exec systemctl stop v55-fundamental.service
fi
echo "v55-fundamental.service is not active; foreground runs stop with SIGTERM/Ctrl-C"
