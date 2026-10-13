#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: nordvpn-gluetun-stop <tag-or-container-name>" >&2
  exit 2
fi

CONTAINER="$1"
case "${CONTAINER}" in
  gluetun-*) ;;
  *) CONTAINER="gluetun-${CONTAINER}" ;;
esac
docker rm -f "${CONTAINER}"
