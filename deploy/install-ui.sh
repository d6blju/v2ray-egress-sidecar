#!/usr/bin/env bash
set -euo pipefail

WEB_ROOT="${WEB_ROOT:-/var/www/v2ray-egress-sidecar-dashboard}"
STATE_DIR="${STATE_DIR:-/var/lib/v2ray-egress-sidecar-admin}"
LIB_DIR="/usr/local/lib/v2ray-egress-sidecar"

install -d "${WEB_ROOT}"
install -d -m 0700 "${STATE_DIR}"
install -d "${LIB_DIR}"
install -m 0644 ./deploy/sidecar_services.py "${LIB_DIR}/sidecar_services.py"
install -m 0644 ./deploy/sidecar_stats_store.py "${LIB_DIR}/sidecar_stats_store.py"
install -m 0644 ./deploy/v2ray_cpu_watch.py "${LIB_DIR}/v2ray_cpu_watch.py"
install -m 0644 ./deploy/service-domains.json "${LIB_DIR}/service-domains.json"
install -m 0644 ./deploy/sidecar_exits.py "${LIB_DIR}/sidecar_exits.py"
install -m 0644 ./deploy/sidecar_streaming.py "${LIB_DIR}/sidecar_streaming.py"
install -m 0644 ./deploy/sidecar_unlock.py "${LIB_DIR}/sidecar_unlock.py"
install -m 0644 ./deploy/sidecar_streaming_jobs.py "${LIB_DIR}/sidecar_streaming_jobs.py"

install -m 0644 ./web/control.html "${WEB_ROOT}/control.html"
install -m 0755 ./deploy/v2ray-egress-dashboard-render.py /usr/local/bin/v2ray-egress-dashboard-render
install -m 0755 ./deploy/v2ray-egress-admin-api.py /usr/local/bin/v2ray-egress-admin-api
install -m 0755 ./deploy/v2ray-sidecar-stats.py /usr/local/bin/v2ray-sidecar-stats
install -m 0755 ./deploy/v2ray-cpu-diagnose.sh /usr/local/bin/v2ray-cpu-diagnose

install -m 0755 ./deploy/nordvpn-gluetun-start.sh /usr/local/bin/nordvpn-gluetun-start
install -m 0755 ./deploy/nordvpn-gluetun-status.sh /usr/local/bin/nordvpn-gluetun-status
install -m 0755 ./deploy/nordvpn-gluetun-stop.sh /usr/local/bin/nordvpn-gluetun-stop

install -m 0644 ./systemd/v2ray-egress-sidecar-dashboard-render.service /etc/systemd/system/v2ray-egress-sidecar-dashboard-render.service
install -m 0644 ./systemd/v2ray-egress-sidecar-dashboard-render.timer /etc/systemd/system/v2ray-egress-sidecar-dashboard-render.timer
install -m 0644 ./systemd/v2ray-egress-sidecar-admin-api.service /etc/systemd/system/v2ray-egress-sidecar-admin-api.service
install -m 0644 ./systemd/v2ray-cpu-diagnose.service /etc/systemd/system/v2ray-cpu-diagnose.service
install -d -m 0700 /var/log/v2ray-cpu-diagnostics

install -m 0644 ./deploy/nginx-sidecar-ui.conf.example /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf.example 2>/dev/null || true

systemctl daemon-reload
systemctl disable --now v2ray-cpu-diagnose.timer 2>/dev/null || true
systemctl enable --now v2ray-cpu-diagnose.service
systemctl enable --now v2ray-egress-sidecar-dashboard-render.timer
systemctl enable --now v2ray-egress-sidecar-admin-api.service
systemctl start v2ray-egress-sidecar-dashboard-render.service || true

cat <<EOF
UI files installed.

Dashboard root:
  ${WEB_ROOT}

Admin API:
  http://127.0.0.1:18082/api/state

Nginx example:
  /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf.example

Copy the nginx example to an enabled vhost or paste the location blocks into
your existing TLS server, protect it with Basic Auth, then reload nginx.
EOF
