#!/usr/bin/env bash
set -euo pipefail

WEB_ROOT="${WEB_ROOT:-/var/www/v2ray-egress-sidecar-dashboard}"
STATE_DIR="${STATE_DIR:-/var/lib/v2ray-egress-sidecar-admin}"
LIB_DIR="/usr/local/lib/v2ray-egress-sidecar"
CHECK_SOURCE="${SIDECAR_CHECK_SOURCE:-./target/release/v2ray-egress-sidecar}"
if [[ ! -f "$CHECK_SOURCE" ]]; then
    echo 'Build the configuration checker with cargo build --release before installing the UI.' >&2
    exit 1
fi
install -m 0755 "$CHECK_SOURCE" /usr/local/bin/v2ray-egress-config-check

install -d "${WEB_ROOT}"
install -d -m 0700 "${STATE_DIR}"
install -d "${LIB_DIR}"
install -m 0644 ./deploy/sidecar_services.py "${LIB_DIR}/sidecar_services.py"
install -m 0644 ./deploy/sidecar_stats_store.py "${LIB_DIR}/sidecar_stats_store.py"
install -m 0644 ./deploy/sidecar_route_transaction.py "${LIB_DIR}/sidecar_route_transaction.py"
install -m 0644 ./deploy/sidecar_route_preview.py "${LIB_DIR}/sidecar_route_preview.py"
install -m 0644 ./deploy/sidecar_auth.py "${LIB_DIR}/sidecar_auth.py"
install -m 0644 ./deploy/sidecar_nginx_auth.py "${LIB_DIR}/sidecar_nginx_auth.py"
install -m 0644 ./deploy/service-domains.json "${LIB_DIR}/service-domains.json"
install -m 0644 ./deploy/sidecar_exits.py "${LIB_DIR}/sidecar_exits.py"
install -m 0644 ./deploy/sidecar_streaming.py "${LIB_DIR}/sidecar_streaming.py"
install -m 0644 ./deploy/sidecar_unlock.py "${LIB_DIR}/sidecar_unlock.py"
install -m 0644 ./deploy/sidecar_streaming_jobs.py "${LIB_DIR}/sidecar_streaming_jobs.py"

install -m 0644 ./web/control.html "${WEB_ROOT}/control.html"
install -m 0644 ./web/login.html "${WEB_ROOT}/login.html"
install -m 0644 ./web/auth.js "${WEB_ROOT}/auth.js"
install -m 0755 ./deploy/v2ray-egress-dashboard-render.py /usr/local/bin/v2ray-egress-dashboard-render
install -m 0755 ./deploy/v2ray-egress-admin-api.py /usr/local/bin/v2ray-egress-admin-api
install -m 0755 ./deploy/v2ray-sidecar-stats.py /usr/local/bin/v2ray-sidecar-stats

install -m 0755 ./deploy/nordvpn-gluetun-start.sh /usr/local/bin/nordvpn-gluetun-start
install -m 0755 ./deploy/nordvpn-gluetun-status.sh /usr/local/bin/nordvpn-gluetun-status
install -m 0755 ./deploy/nordvpn-gluetun-stop.sh /usr/local/bin/nordvpn-gluetun-stop

install -m 0644 ./systemd/v2ray-egress-sidecar-dashboard-render.service /etc/systemd/system/v2ray-egress-sidecar-dashboard-render.service
install -m 0644 ./systemd/v2ray-egress-sidecar-dashboard-render.timer /etc/systemd/system/v2ray-egress-sidecar-dashboard-render.timer
install -m 0644 ./systemd/v2ray-egress-sidecar-admin-api.service /etc/systemd/system/v2ray-egress-sidecar-admin-api.service

install -m 0644 ./deploy/nginx-sidecar-ui.conf.example /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf.example 2>/dev/null || true

systemctl daemon-reload
systemctl disable --now v2ray-cpu-diagnose.timer 2>/dev/null || true
systemctl disable --now v2ray-cpu-diagnose.service 2>/dev/null || true
systemctl enable --now v2ray-egress-sidecar-dashboard-render.timer
systemctl enable --now v2ray-egress-sidecar-admin-api.service
systemctl start v2ray-egress-sidecar-dashboard-render.service || true

cat <<EOF
UI files installed.

Dashboard root:
  ${WEB_ROOT}

Admin API:
  http://127.0.0.1:18082/api/health

Nginx example:
  /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf.example

Copy the nginx example to an enabled vhost or paste the location blocks into
your existing TLS server. Follow docs/authentication.md to configure credentials,
session cookies and the static session gate before exposing the UI.
EOF
