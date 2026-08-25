#!/usr/bin/env bash
set -euo pipefail

UNIT="V52-fundamental.service"
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is unavailable. For a foreground run, press Ctrl+C in its terminal." >&2
  exit 2
fi
if systemctl is-active --quiet "$UNIT"; then
  sudo systemctl stop "$UNIT"
  echo "$UNIT stopped cleanly"
else
  echo "$UNIT is not active"
fi
