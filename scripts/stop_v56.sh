#!/usr/bin/env bash
set -Eeuo pipefail
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet v56-fundamental.service; then
  exec systemctl stop v56-fundamental.service
fi
echo "v56-fundamental.service is not active; foreground runs stop with SIGTERM/Ctrl-C"

