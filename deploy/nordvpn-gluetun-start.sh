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

This starts a Gluetun container that exposes a localhost-only HTTP CONNECT proxy.
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
ENV_FILE="/etc/gluetun/nordvpn.env"
CONTAINER="gluetun-${TAG}"

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

if [[ ! -f "${ENV_FILE}" ]]; then
  install -d -m 0700 /etc/gluetun
  cat > "${ENV_FILE}" <<'EOF'
OPENVPN_USER=
OPENVPN_PASSWORD=
EOF
  chmod 0600 "${ENV_FILE}"
  echo "created ${ENV_FILE}; fill in NordVPN manual setup credentials first" >&2
  exit 1
fi

if ! grep -q '^OPENVPN_USER=.' "${ENV_FILE}" || ! grep -q '^OPENVPN_PASSWORD=.' "${ENV_FILE}"; then
  echo "fill OPENVPN_USER and OPENVPN_PASSWORD in ${ENV_FILE} first" >&2
  exit 1
fi

docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
docker run -d \
  --name "${CONTAINER}" \
  --cap-add=NET_ADMIN \
  --device /dev/net/tun:/dev/net/tun \
  --restart unless-stopped \
  -p "127.0.0.1:${PROXY_PORT}:8888/tcp" \
  --env-file "${ENV_FILE}" \
  -e VPN_SERVICE_PROVIDER=nordvpn \
  -e VPN_TYPE=openvpn \
  -e OPENVPN_PROTOCOL="${PROTOCOL}" \
  -e SERVER_COUNTRIES="${COUNTRY}" \
  -e HTTPPROXY=on \
  -e HTTPPROXY_LISTENING_ADDRESS=:8888 \
  qmcgaw/gluetun:latest

echo "started ${CONTAINER}: country=${COUNTRY} proxy=127.0.0.1:${PROXY_PORT}"
echo "logs: docker logs -f ${CONTAINER}"
