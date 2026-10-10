#!/usr/bin/env bash
set -euo pipefail

docker ps --filter 'name=gluetun-' --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

for name in $(docker ps --filter 'name=gluetun-' --format '{{.Names}}'); do
  echo "--- ${name} public ip ---"
  docker exec "${name}" wget -qO- --timeout=10 https://ipinfo.io/ip || true
  echo
done
