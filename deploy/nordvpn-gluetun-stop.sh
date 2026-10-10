#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: nordvpn-gluetun-stop <tag>" >&2
  exit 2
fi

docker rm -f "gluetun-$1"
