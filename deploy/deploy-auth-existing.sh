#!/usr/bin/env bash
# Management-only upgrade for the existing legacy-path installation.
set -euo pipefail
stage="$(cd "$(dirname "$0")/.." && pwd)"
service=v2ray-sidecar-admin-api.service
lib=/usr/local/lib/v2ray-egress-sidecar
web=/var/www/v2ray-sidecar-dashboard
dropin=/etc/systemd/system/$service.d/auth.conf
credentials=/etc/nginx/.htpasswd-v2ray-sidecar
test -s "$credentials"
test ! -e /var/lib/v2ray-sidecar-admin/route-transaction.json
nginx -V 2>&1 | grep -q -- --with-http_auth_request_module
nginx -t
before="$(systemctl show v2ray.service v2ray-rust-sidecar.service nginx.service -p MainPID)"
hashes="$(sha256sum /etc/v2ray-rust-sidecar/config.toml /etc/v2ray/config.json "$credentials")"
backup="/var/backups/sidecar-auth-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$backup"
printf '%s\n' "$before" > "$backup/pids"
printf '%s\n' "$hashes" > "$backup/hashes"
paths=(etc/nginx/nginx.conf usr/local/bin/v2ray-sidecar-admin-api usr/local/bin/v2ray-sidecar-dashboard-render var/www/v2ray-sidecar-dashboard/control.html)
optional=("$dropin" "$lib/sidecar_route_preview.py" "$lib/sidecar_auth.py" "$lib/sidecar_nginx_auth.py" "$web/auth.js" "$web/login.html" "$web/index.html")
for path in "${optional[@]}"; do
    if [[ -f "$path" ]]; then
        paths+=("${path#/}")
    else
        printf '%s\n' "$path" >> "$backup/new-files"
    fi
done
tar -czf "$backup/files.tar.gz" -C / "${paths[@]}"
rollback() {
    trap - ERR
    systemctl stop "$service" || true
    tar -xzf "$backup/files.tar.gz" -C /
    if [[ -f "$backup/new-files" ]]; then
        while IFS= read -r path; do rm -f -- "$path"; done < "$backup/new-files"
    fi
    systemctl daemon-reload
    systemctl start "$service" || true
    if nginx -t; then systemctl reload nginx; fi
    echo "Management upgrade rolled back; backup: $backup" >&2
    exit 1
}
trap rollback ERR
systemctl stop "$service"
install -m 0644 "$stage/deploy/sidecar_auth.py" "$lib/sidecar_auth.py"
install -m 0644 "$stage/deploy/sidecar_route_preview.py" "$lib/sidecar_route_preview.py"
install -m 0644 "$stage/deploy/sidecar_nginx_auth.py" "$lib/sidecar_nginx_auth.py"
install -m 0755 "$stage/deploy/v2ray-egress-admin-api.py" /usr/local/bin/v2ray-sidecar-admin-api
install -m 0755 "$stage/deploy/v2ray-egress-dashboard-render.py" /usr/local/bin/v2ray-sidecar-dashboard-render
for name in control.html auth.js login.html; do install -m 0644 "$stage/web/$name" "$web/$name"; done
install -d "$(dirname "$dropin")"
printf '%s\n' '[Service]' "Environment=SIDECAR_HTPASSWD_FILE=$credentials" 'Environment=SIDECAR_COOKIE_SECURE=1' 'Environment=SIDECAR_COOKIE_PATH=/sidecar-stats/' > "$dropin"
python3 -m py_compile "$lib/sidecar_route_preview.py" "$lib/sidecar_auth.py" /usr/local/bin/v2ray-sidecar-admin-api
PYTHONPATH="$lib" python3 "$lib/sidecar_nginx_auth.py" /etc/nginx/nginx.conf
nginx -t
systemctl daemon-reload
systemctl start "$service"
ready=0
for attempt in $(seq 1 20); do
    if curl -fsS --max-time 5 http://127.0.0.1:18082/api/health -o "$backup/health.json"; then ready=1; break; fi
    sleep 1
done
[[ "$ready" == 1 ]]
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["auth_configured"]' "$backup/health.json"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18082/api/state)" == 401 ]]
systemctl reload nginx
# The timer uses the upgraded renderer. Add its auth script immediately without
# scanning any statistics or starting an overlapping render on the production host.
python3 - "$web/index.html" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/usr/local/lib/v2ray-egress-sidecar')
from sidecar_route_transaction import atomic
path = Path(sys.argv[1])
if path.exists():
    text = path.read_text()
    if '<script src="auth.js"></script>' not in text:
        text = text.replace('</head>', '<script src="auth.js"></script>\n</head>', 1)
    if 'await SidecarAuth.ready();' not in text:
        text = text.replace('async function load(){', 'async function load(){\n await SidecarAuth.ready();', 1)
    atomic(str(path), text, mode=0o644)
PY
[[ "$before" == "$(systemctl show v2ray.service v2ray-rust-sidecar.service nginx.service -p MainPID)" ]]
[[ "$hashes" == "$(sha256sum /etc/v2ray-rust-sidecar/config.toml /etc/v2ray/config.json "$credentials")" ]]
systemctl is-active v2ray.service v2ray-rust-sidecar.service "$service" nginx.service
trap - ERR
echo "Backup: $backup"
