#!/usr/bin/env bash
set -euo pipefail
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run with sudo: sudo ./bin/install_systemd.sh" >&2
  exit 1
fi
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/v52-fundamental
STAGE=/opt/v52-fundamental.release-new
PREV=/opt/v52-fundamental.release-previous
ENVFILE=/etc/v52-fundamental.env
DATA=/var/lib/v52-fundamental
SERVICE=v52-fundamental.service

if ! id v52fund >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA" --create-home --shell /usr/sbin/nologin v52fund
fi
install -d -o v52fund -g v52fund -m 0750 "$DATA" "$DATA/db" "$DATA/state" "$DATA/logs" "$DATA/runs" "$DATA/market" "$DATA/diagnostic_bundles"

# Build a clean staged release so stale files from an older installation can never survive an upgrade.
rm -rf "$STAGE"
install -d -o root -g root -m 0755 "$STAGE"
tar -C "$SRC" \
  --exclude='./.venv' --exclude='./.env' --exclude='./.pytest_cache' \
  --exclude='*/__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
  -cf - . | tar -C "$STAGE" -xf -
python3 -m venv "$STAGE/.venv"
"$STAGE/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"$STAGE/.venv/bin/pip" install --disable-pip-version-check --requirement "$STAGE/requirements.txt"
"$STAGE/.venv/bin/pip" check

if [[ -f "$SRC/.env" ]]; then
  install -o root -g v52fund -m 0640 "$SRC/.env" "$ENVFILE"
elif [[ ! -f "$ENVFILE" ]]; then
  install -o root -g v52fund -m 0640 "$STAGE/.env.example" "$ENVFILE"
fi

# Stop only after the replacement has been fully built and dependency-checked.
systemctl stop "$SERVICE" 2>/dev/null || true
rm -rf "$PREV"
if [[ -d "$APP" ]]; then mv "$APP" "$PREV"; fi
if ! mv "$STAGE" "$APP"; then
  echo "Release activation failed; restoring previous release" >&2
  [[ -d "$PREV" ]] && mv "$PREV" "$APP"
  exit 1
fi
chown -R root:root "$APP"
chmod -R go-w "$APP"
install -o root -g root -m 0644 "$APP/systemd/v52-fundamental.service" /etc/systemd/system/$SERVICE
systemctl daemon-reload

echo "V52 release installed cleanly at $APP"
echo "Edit $ENVFILE (Telegram/API credentials and operator User-Agent), then run:"
echo "  sudo systemctl enable --now $SERVICE"
echo "Preflight is enforced by systemd ExecStartPre on every start."
echo "Previous release, if any, is retained at $PREV for explicit rollback."
