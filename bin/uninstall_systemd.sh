#!/usr/bin/env bash
set -euo pipefail
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then echo "Run with sudo" >&2; exit 1; fi
systemctl disable --now v52-fundamental.service 2>/dev/null || true
rm -f /etc/systemd/system/v52-fundamental.service
systemctl daemon-reload
echo "Service removed. Data and /opt release were intentionally preserved."
