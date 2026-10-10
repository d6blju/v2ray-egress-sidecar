#!/usr/bin/env bash
set -euo pipefail

WEB_ROOT="${WEB_ROOT:-/var/www/v2ray-egress-sidecar-dashboard}"
STATE_DIR="${STATE_DIR:-/var/lib/v2ray-egress-sidecar-admin}"

install -d "${WEB_ROOT}"
install -d -m 0700 "${STATE_DIR}"

install -m 0644 ./web/control.html "${WEB_ROOT}/control.html"
install -m 0755 ./deploy/v2ray-egress-dashboard-render.py /usr/local/bin/v2ray-egress-dashboard-render
install -m 0755 ./deploy/v2ray-egress-admin-api.py /usr/local/bin/v2ray-egress-admin-api

install -m 0755 ./deploy/nordvpn-gluetun-start.sh /usr/local/bin/nordvpn-gluetun-start
install -m 0755 ./deploy/nordvpn-gluetun-status.sh /usr/local/bin/nordvpn-gluetun-status
install -m 0755 ./deploy/nordvpn-gluetun-stop.sh /usr/local/bin/nordvpn-gluetun-stop

install -m 0644 ./systemd/v2ray-egress-sidecar-dashboard-render.service /etc/systemd/system/v2ray-egress-sidecar-dashboard-render.service
install -m 0644 ./systemd/v2ray-egress-sidecar-dashboard-render.timer /etc/systemd/system/v2ray-egress-sidecar-dashboard-render.timer
install -m 0644 ./systemd/v2ray-egress-sidecar-admin-api.service /etc/systemd/system/v2ray-egress-sidecar-admin-api.service

install -m 0644 ./deploy/nginx-sidecar-ui.conf.example /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf.example 2>/dev/null || true

systemctl daemon-reload
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
