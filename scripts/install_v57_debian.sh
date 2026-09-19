#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "$EUID" -ne 0 ]]; then echo "run as root: sudo bash scripts/install_v57_debian.sh" >&2; exit 2; fi
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${V57_SERVICE_USER:-v57fund}"
SERVICE_GROUP="${V57_SERVICE_GROUP:-v57fund}"
APP_DIR="${V57_APP_DIR:-/opt/v57-fundamental}"
DATA_DIR="${V57_DATA_DIR:-/var/lib/v57-fundamental}"
ENV_FILE="${V57_ENV_FILE:-/etc/v57-fundamental.env}"
UNIT_FILE="/etc/systemd/system/v57-fundamental.service"

# Fresh minimal Debian must have everything needed to create a venv and install wheels.
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates curl build-essential pkg-config libgomp1 procps util-linux
else
  command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 3; }
fi
python3 -m venv --help >/dev/null
getent group "$SERVICE_GROUP" >/dev/null || groupadd --system "$SERVICE_GROUP"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --gid "$SERVICE_GROUP" --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$APP_DIR" "$DATA_DIR" "$DATA_DIR/logs" "$DATA_DIR/runs" "$DATA_DIR/state" "$DATA_DIR/brain" "$DATA_DIR/db"
if [[ "$(readlink -f "$SOURCE_ROOT")" != "$(readlink -f "$APP_DIR")" ]]; then
  find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf -- {} +
  cp -a "$SOURCE_ROOT/." "$APP_DIR/"
fi
rm -rf "$APP_DIR/.venv" "$APP_DIR/.pytest_cache"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
# The temporal torch stack is optional and disabled by the bounded production
# profile. Installing only tabular ML/boosting avoids several gigabytes of
# resident/deployment weight while retaining governed challenger families.
"$APP_DIR/.venv/bin/python" -m pip install "$APP_DIR[ml,boosting]"
"$APP_DIR/.venv/bin/python" -m pip check
# Wheel builds leave a duplicate source tree and packaging metadata beside the
# immutable release payload.  They are not runtime inputs; remove them so the
# active application tree remains byte-verifiable against MANIFEST.sha256.
if [[ -d "$APP_DIR/build" ]]; then
  find "$APP_DIR/build" -mindepth 1 -delete
  rmdir "$APP_DIR/build"
fi
find "$APP_DIR/src" -mindepth 1 -maxdepth 1 -type d -name '*.egg-info' -exec rm -rf -- {} +
if [[ ! -e "$ENV_FILE" ]]; then install -m 0640 -o root -g "$SERVICE_GROUP" "$APP_DIR/.env.example" "$ENV_FILE"; fi
install -m 0640 -o root -g "$SERVICE_GROUP" "$APP_DIR/config/resource-profile.env" /etc/v57-resource-profile.env
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR" "$DATA_DIR"
chmod 0750 "$APP_DIR" "$APP_DIR/.venv" "$DATA_DIR"
find "$APP_DIR/scripts" "$APP_DIR/bin" -maxdepth 1 -type f -name '*.sh' -exec chmod 0750 {} +
install -m 0644 "$APP_DIR/systemd/v57-fundamental.service" "$UNIT_FILE"
install -m 0750 "$APP_DIR/scripts/v57_disk_guard.sh" /usr/local/sbin/v57-disk-guard.sh
install -m 0644 "$APP_DIR/systemd/v57-disk-guard.service" /etc/systemd/system/v57-disk-guard.service
install -m 0644 "$APP_DIR/systemd/v57-disk-guard.timer" /etc/systemd/system/v57-disk-guard.timer
systemctl daemon-reload
systemctl enable v57-fundamental.service
systemctl enable --now v57-disk-guard.timer
getent passwd "$SERVICE_USER" >/dev/null
getent group "$SERVICE_GROUP" >/dev/null
test -x "$APP_DIR/.venv/bin/python"
test -r "$ENV_FILE"
runuser -u "$SERVICE_USER" -- test -w "$DATA_DIR"
# Do not fake credential validation before the operator fills the env file.
runuser -u "$SERVICE_USER" -- bash -c 'set -a; source "$1"; shift; exec "$@"' bash \
  /etc/v57-resource-profile.env env V57_ENV_FILE="$ENV_FILE" V57_PACKAGE_ROOT="$APP_DIR" \
  V52_PACKAGE_ROOT="$APP_DIR" DATA_DIR="$DATA_DIR" PYTHONPATH="$APP_DIR/src" \
  "$APP_DIR/.venv/bin/python" -m v52.runtime.bootstrap
echo "Installed. Configure $ENV_FILE, run: sudo -u $SERVICE_USER V57_ENV_FILE=$ENV_FILE $APP_DIR/scripts/preflight_v57.sh ; then systemctl start v57-fundamental"
