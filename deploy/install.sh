#!/usr/bin/env bash
set -euo pipefail

APP_NAME="v2ray-egress-sidecar"
BIN_SRC="${1:-./target/release/${APP_NAME}}"
CONFIG_SRC="${2:-./config.example.toml}"

if [[ ! -f "${BIN_SRC}" ]]; then
  echo "binary not found: ${BIN_SRC}" >&2
  exit 1
fi

install -d /etc/v2ray-egress-sidecar
install -d /var/lib/v2ray-egress-sidecar
install -m 0755 "${BIN_SRC}" /usr/local/bin/${APP_NAME}

if [[ ! -f /etc/v2ray-egress-sidecar/config.toml ]]; then
  install -m 0644 "${CONFIG_SRC}" /etc/v2ray-egress-sidecar/config.toml
fi

install -m 0644 ./systemd/v2ray-egress-sidecar.service /etc/systemd/system/v2ray-egress-sidecar.service
systemctl daemon-reload
systemctl enable --now v2ray-egress-sidecar.service
systemctl status --no-pager v2ray-egress-sidecar.service
