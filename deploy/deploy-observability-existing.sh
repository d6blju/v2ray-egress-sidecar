#!/usr/bin/env bash
# Upgrade the established legacy-path installation without touching forwarding.
set -euo pipefail
stage="$(cd "$(dirname "$0")/.." && pwd)"
backup="/var/backups/v2ray-observability-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$backup"
units=(v2ray.service v2ray-rust-sidecar.service)
before="$(systemctl show -p MainPID "${units[@]}")"
hashes="$(sha256sum /etc/v2ray-rust-sidecar/config.toml /etc/v2ray/config.json)"
printf '%s\n' "$before" > "$backup/forwarding-pids.txt"
printf '%s\n' "$hashes" > "$backup/config-sha256.txt"
paths=(usr/local/bin/v2ray-sidecar-dashboard-render
       etc/systemd/system/v2ray-sidecar-dashboard-render.service
       etc/systemd/system/v2ray-sidecar-dashboard-render.timer
       var/www/v2ray-sidecar-dashboard/index.html var/www/v2ray-sidecar-dashboard/stats.json)
if [[ -f /usr/local/lib/v2ray-egress-sidecar/sidecar_stats_store.py ]]; then
    paths+=(usr/local/lib/v2ray-egress-sidecar/sidecar_stats_store.py)
fi
tar -czf "$backup/files.tar.gz" -C / "${paths[@]}"
rollback() {
    echo "Observability upgrade failed; restoring $backup" >&2
    systemctl stop v2ray-sidecar-dashboard-render.timer || true
    tar -xzf "$backup/files.tar.gz" -C /
    systemctl daemon-reload
    systemctl start v2ray-sidecar-dashboard-render.timer || true
}
trap rollback ERR
systemctl stop v2ray-sidecar-dashboard-render.timer v2ray-sidecar-dashboard-render.service
for unit in v2ray-cpu-diagnose.service v2ray-cpu-diagnose.timer; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
        systemctl disable --now "$unit"
    fi
done
install -m 0644 "$stage/deploy/sidecar_stats_store.py" /usr/local/lib/v2ray-egress-sidecar/sidecar_stats_store.py
install -m 0755 "$stage/deploy/v2ray-egress-dashboard-render.py" /usr/local/bin/v2ray-sidecar-dashboard-render
install -m 0644 "$stage/systemd/v2ray-sidecar-dashboard-render.service" /etc/systemd/system/
install -m 0644 "$stage/systemd/v2ray-sidecar-dashboard-render.timer" /etc/systemd/system/
python3 -m py_compile /usr/local/lib/v2ray-egress-sidecar/sidecar_stats_store.py /usr/local/bin/v2ray-sidecar-dashboard-render
systemctl daemon-reload
systemctl start v2ray-sidecar-dashboard-render.service
systemctl start v2ray-sidecar-dashboard-render.timer
[[ "$before" == "$(systemctl show -p MainPID "${units[@]}")" ]]
[[ "$hashes" == "$(sha256sum /etc/v2ray-rust-sidecar/config.toml /etc/v2ray/config.json)" ]]
systemctl is-active v2ray.service v2ray-rust-sidecar.service v2ray-sidecar-dashboard-render.timer
trap - ERR
echo "Backup: $backup"
