#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${EUID}" -ne 0 ]]; then echo "run as root" >&2; exit 2; fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -m 0644 "$ROOT/systemd/v55-fundamental.service" /etc/systemd/system/v55-fundamental.service
systemctl daemon-reload
systemctl enable v55-fundamental.service
echo "Installed. Start explicitly with: systemctl start v55-fundamental.service"
