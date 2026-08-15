#!/usr/bin/env bash
# Safe teardown for mrmpanel fresh installs
set -euo pipefail

INSTALL_ROOT="/opt/mrmpanel"
DATA_ROOT="/var/lib/mrmpanel"
COMPOSE_DIR="${INSTALL_ROOT}/compose"

[[ $(id -u) -eq 0 ]] || { echo "Run as root"; exit 1; }

read -r -p "Stop mrmpanel and remove containers? [y/N] " ans
[[ "${ans,,}" == "y" ]] || exit 0

systemctl stop mrmpanel 2>/dev/null || true
systemctl disable mrmpanel 2>/dev/null || true

if [[ -d "$COMPOSE_DIR" ]]; then
  cd "$COMPOSE_DIR"
  docker compose --profile web --profile mail --profile mariadb --profile postgres down -v || true
fi

# Stop site containers labeled by mrmpanel
docker ps -aq --filter "label=mrmpanel.site" | xargs -r docker rm -f || true

read -r -p "Also delete ${DATA_ROOT}, ${INSTALL_ROOT}, and unit files? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
  rm -f /etc/systemd/system/mrmpanel.service
  systemctl daemon-reload || true
  rm -rf "$DATA_ROOT" "$INSTALL_ROOT" /etc/mrmpanel
  rm -f /usr/local/bin/mrmpanel-jail-shell
  echo "Removed."
else
  echo "Services stopped; data left in place."
fi
