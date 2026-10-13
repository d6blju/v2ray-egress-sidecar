#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  nordvpn-gluetun-start <tag> <country> <proxy_port> [udp|tcp]

Examples:
  nordvpn-gluetun-start us "United States" 18888
  nordvpn-gluetun-start jp "Japan" 18889 tcp

Credentials:
  Put NordVPN manual setup credentials in /etc/gluetun/nordvpn.env:
    OPENVPN_USER=...
    OPENVPN_PASSWORD=...

Reuses an existing country exit or creates a localhost-only HTTP CONNECT proxy.
The returned proxy port can differ from the preferred port.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 3 ]]; then
  usage
  exit 0
fi

TAG="$1"
COUNTRY="$2"
PROXY_PORT="$3"
PROTOCOL="${4:-udp}"

case "${TAG}" in
  *[!a-zA-Z0-9_-]*|"")
    echo "invalid tag: ${TAG}" >&2
    exit 2
    ;;
esac

case "${PROTOCOL}" in
  udp|tcp) ;;
  *)
    echo "protocol must be udp or tcp" >&2
    exit 2
    ;;
esac

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HELPER="${SCRIPT_DIR}/sidecar_exits.py"
if [[ ! -f "${HELPER}" ]]; then
  HELPER=/usr/local/lib/v2ray-egress-sidecar/sidecar_exits.py
fi
exec python3 "${HELPER}" "${COUNTRY}" --preferred-port "${PROXY_PORT}" --protocol "${PROTOCOL}"
