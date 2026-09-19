#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$EUID" -ne 0 ]]; then
  echo "run as root" >&2
  exit 2
fi

ARCHIVE=/root/V57_3_REALTIME_LOCAL_ADAPTIVE_FINAL.rar
STAGING=/opt/v57-fundamental-staging-573
APP=/opt/v57-fundamental
DATA=/var/lib/v57-fundamental
ENV_FILE=/etc/v57-fundamental.env

for expected in "$ARCHIVE" "$STAGING/src/v52" "$STAGING/systemd/v57-fundamental.service" "$ENV_FILE"; do
  test -e "$expected" || { echo "missing required deployment input: $expected" >&2; exit 3; }
done
test "$(readlink -f "$APP")" = /opt/v57-fundamental
test "$(readlink -f "$DATA")" = /var/lib/v57-fundamental
test "$(readlink -f "$STAGING")" = /opt/v57-fundamental-staging-573

systemctl stop v57-fundamental.service || true

# The operator explicitly requested a clean learning start. These are the exact
# V57 application/data/release-backup paths audited before deployment.
if [[ -d "$DATA" ]]; then find "$DATA" -mindepth 1 -delete; fi
if [[ -d "$APP" ]]; then find "$APP" -mindepth 1 -delete; fi
install -d -m 0750 -o v57fund -g v57fund "$APP" "$DATA"
cp -a "$STAGING/." "$APP/"

rm -f /root/V57_TRUE_AUTONOMOUS_MULTI_ASSET_FUNDAMENTAL_INTELLIGENCE_BRAIN_ABSOLUTE_FINAL.rar
rm -f /root/V57_TRUE_AUTONOMOUS_MULTI_ASSET_FUNDAMENTAL_INTELLIGENCE_BRAIN_ABSOLUTE_FINAL.zip
rm -f /root/v57-prepatch-diagnostics.tar.gz
rm -f /root/patch_v57_runtime_final.py
if [[ -d /root/v57-learning-fix-20260906-024601 ]]; then
  find /root/v57-learning-fix-20260906-024601 -mindepth 1 -delete
  rmdir /root/v57-learning-fix-20260906-024601
fi

bash "$APP/scripts/install_v57_debian.sh"
chown -R v57fund:v57fund "$APP" "$DATA"
systemctl daemon-reload

runuser -u v57fund -- bash -c 'set -a; source "$1"; shift; exec "$@"' bash \
  /etc/v57-resource-profile.env env V57_ENV_FILE="$ENV_FILE" V57_PACKAGE_ROOT="$APP" \
  V52_PACKAGE_ROOT="$APP" DATA_DIR="$DATA" PYTHONPATH="$APP/src" \
  "$APP/.venv/bin/python" -m v52.diagnostics.preflight

systemctl enable --now v57-fundamental.service
echo "fresh V57.3 deployment started"
