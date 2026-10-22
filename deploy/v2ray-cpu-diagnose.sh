#!/usr/bin/env bash
# Manual capture; continuous observation runs in v2ray-cpu-diagnose.service.
set -euo pipefail
exec /usr/bin/python3 /usr/local/lib/v2ray-egress-sidecar/v2ray_cpu_watch.py \
    --once --out "${V2RAY_CPU_DIAG_DIR:-/var/log/v2ray-cpu-diagnostics}" "$@"
