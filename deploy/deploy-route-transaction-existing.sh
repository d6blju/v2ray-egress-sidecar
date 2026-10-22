#!/usr/bin/env bash
# Upgrade management only on a legacy-path installation. Supply the helper-built checker.
set -euo pipefail
stage="$(cd "$(dirname "$0")/.." && pwd)"
checker="${SIDECAR_CHECK_SOURCE:-$stage/v2ray-egress-config-check}"
"$checker" --check --config /etc/v2ray-rust-sidecar/config.toml
test ! -e /var/lib/v2ray-sidecar-admin/route-transaction.json
before="$(systemctl show v2ray.service v2ray-rust-sidecar.service -p MainPID)"
hashes="$(sha256sum /etc/v2ray-rust-sidecar/config.toml /etc/v2ray/config.json)"
backup="/var/backups/v2ray-route-transaction-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$backup"
printf '%s\n' "$before" > "$backup/forwarding-pids.txt"
printf '%s\n' "$hashes" > "$backup/config-sha256.txt"
paths=(usr/local/bin/v2ray-sidecar-admin-api var/www/v2ray-sidecar-dashboard/control.html)
for path in usr/local/lib/v2ray-egress-sidecar/sidecar_route_transaction.py usr/local/bin/v2ray-egress-config-check; do
    [[ ! -f "/$path" ]] || paths+=("$path")
done
dropin=etc/systemd/system/v2ray-sidecar-admin-api.service.d/route-transactions.conf
had_dropin=0
if [[ -f "/$dropin" ]]; then
    paths+=("$dropin")
    had_dropin=1
fi
tar -czf "$backup/files.tar.gz" -C / "${paths[@]}"
rollback() {
    systemctl stop v2ray-sidecar-admin-api.service || true
    tar -xzf "$backup/files.tar.gz" -C /
    [[ "$had_dropin" != 0 ]] || rm -f "/$dropin"
    systemctl daemon-reload
    systemctl start v2ray-sidecar-admin-api.service || true
    echo "Management upgrade restored from $backup" >&2
}
trap rollback ERR
systemctl stop v2ray-sidecar-admin-api.service
install -m 0755 "$checker" /usr/local/bin/v2ray-egress-config-check
install -m 0644 "$stage/deploy/sidecar_route_transaction.py" /usr/local/lib/v2ray-egress-sidecar/sidecar_route_transaction.py
install -m 0755 "$stage/deploy/v2ray-egress-admin-api.py" /usr/local/bin/v2ray-sidecar-admin-api
install -m 0644 "$stage/web/control.html" /var/www/v2ray-sidecar-dashboard/control.html
install -d /etc/systemd/system/v2ray-sidecar-admin-api.service.d
install -m 0644 "$stage/systemd/v2ray-route-transaction-legacy.conf" "/$dropin"
python3 -m py_compile /usr/local/bin/v2ray-sidecar-admin-api /usr/local/lib/v2ray-egress-sidecar/sidecar_route_transaction.py
systemctl daemon-reload
systemctl start v2ray-sidecar-admin-api.service
ready=0
for attempt in $(seq 1 20); do
    if curl -fsS --max-time 5 http://127.0.0.1:18082/api/state -o "$backup/state-after.json"; then
        ready=1
        break
    fi
    sleep 1
done
[[ "$ready" == 1 ]]
[[ "$before" == "$(systemctl show v2ray.service v2ray-rust-sidecar.service -p MainPID)" ]]
[[ "$hashes" == "$(sha256sum /etc/v2ray-rust-sidecar/config.toml /etc/v2ray/config.json)" ]]
systemctl is-active v2ray.service v2ray-rust-sidecar.service v2ray-sidecar-admin-api.service
trap - ERR
echo "Backup: $backup"
