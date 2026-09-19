#!/usr/bin/env bash
set -Eeuo pipefail

DATA=/var/lib/v57-fundamental
MIN_FREE_BYTES=${V57_MIN_FREE_BYTES:-16106127360}
MAX_DATA_BYTES=${V57_MAX_DATA_BYTES:-4294967296}
free=$(df -B1 --output=avail / | tail -1 | tr -d ' ')
used=$(du -sb "$DATA" 2>/dev/null | awk '{print $1}')
used=${used:-0}

if (( free < MIN_FREE_BYTES || used > MAX_DATA_BYTES )); then
  logger -t v57-disk-guard "HARD_GUARD free=${free} data=${used}; stopping v57-fundamental before filesystem exhaustion"
  systemctl stop v57-fundamental.service || true
  # Raw websocket journals are explicitly disposable transport evidence.  The
  # bounded production profile disables them; remove leftovers before restart.
  find "$DATA/market" -type f \( -name 'wire-*.jsonl' -o -name 'wire-*.jsonl.gz' \) -delete 2>/dev/null || true
  exit 0
fi

logger -t v57-disk-guard "OK free=${free} data=${used}"
