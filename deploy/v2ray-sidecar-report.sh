#!/usr/bin/env bash
set -euo pipefail

STATS_FILE="${1:-/var/lib/v2ray-egress-sidecar/stats.jsonl}"

if [[ ! -f "${STATS_FILE}" ]]; then
  echo "stats file not found: ${STATS_FILE}" >&2
  exit 1
fi

printf "%-48s %-18s %12s %14s\n" "domain" "outbound" "connections" "bytes"

awk '
  /"error":null/ {
    host = outbound = ""
    uplink = downlink = 0
    if (match($0, /"target_host":"([^"]*)"/, m)) host = m[1]
    if (match($0, /"outbound":"([^"]*)"/, m)) outbound = m[1]
    if (match($0, /"uplink_bytes":([0-9]+)/, m)) uplink = m[1]
    if (match($0, /"downlink_bytes":([0-9]+)/, m)) downlink = m[1]
    if (host != "" && outbound != "") {
      key = host "\t" outbound
      bytes[key] += uplink + downlink
      conns[key] += 1
    }
  }
  END {
    for (key in bytes) {
      split(key, parts, "\t")
      printf "%-48s %-18s %12d %14d\n", parts[1], parts[2], conns[key], bytes[key]
    }
  }
' "${STATS_FILE}" | sort -k4,4nr
