#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "$EUID" -ne 0 ]]; then echo "run as root: sudo bash scripts/install_v56_debian.sh" >&2; exit 2; fi
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${V56_SERVICE_USER:-v56fund}"
SERVICE_GROUP="${V56_SERVICE_GROUP:-v56fund}"
APP_DIR="${V56_APP_DIR:-/opt/v56-fundamental}"
DATA_DIR="${V56_DATA_DIR:-/var/lib/v56-fundamental}"
ENV_FILE="${V56_ENV_FILE:-/etc/v56-fundamental.env}"
UNIT_FILE="/etc/systemd/system/v56-fundamental.service"

getent group "$SERVICE_GROUP" >/dev/null || groupadd --system "$SERVICE_GROUP"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --gid "$SERVICE_GROUP" --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$APP_DIR" "$DATA_DIR" "$DATA_DIR/log" "$DATA_DIR/run"
if [[ "$(readlink -f "$SOURCE_ROOT")" != "$(readlink -f "$APP_DIR")" ]]; then
  find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf -- {} +
  cp -a "$SOURCE_ROOT/." "$APP_DIR/"
fi
rm -rf "$APP_DIR/.venv" "$APP_DIR/.pytest_cache"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$APP_DIR/.venv/bin/python" -m pip install "$APP_DIR[full]"
if [[ ! -e "$ENV_FILE" ]]; then
  install -m 0640 -o root -g "$SERVICE_GROUP" "$APP_DIR/.env.example" "$ENV_FILE"
fi
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR" "$DATA_DIR"
chmod 0750 "$APP_DIR" "$APP_DIR/.venv" "$DATA_DIR"
install -m 0644 "$APP_DIR/systemd/v56-fundamental.service" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable v56-fundamental.service
getent passwd "$SERVICE_USER" >/dev/null
getent group "$SERVICE_GROUP" >/dev/null
test -x "$APP_DIR/.venv/bin/python"
test -r "$ENV_FILE"
runuser -u "$SERVICE_USER" -- test -w "$DATA_DIR"
"$APP_DIR/.venv/bin/python" -m v52.diagnostics.preflight
echo "Installed. Configure $ENV_FILE, then: systemctl start v56-fundamental"
