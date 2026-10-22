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
paths=(usr/local/bin/v2ray-sidecar-dashboard-render usr/local/bin/v2ray-cpu-diagnose
       etc/systemd/system/v2ray-sidecar-dashboard-render.service
       etc/systemd/system/v2ray-sidecar-dashboard-render.timer
       etc/systemd/system/v2ray-cpu-diagnose.service etc/systemd/system/v2ray-cpu-diagnose.timer
       var/www/v2ray-sidecar-dashboard/index.html var/www/v2ray-sidecar-dashboard/stats.json)
for path in usr/local/lib/v2ray-egress-sidecar/sidecar_stats_store.py usr/local/lib/v2ray-egress-sidecar/v2ray_cpu_watch.py; do
    [[ ! -f "/$path" ]] || paths+=("$path")
done
dropin=etc/systemd/system/v2ray-cpu-diagnose.service.d/observability.conf
had_dropin=0
if [[ -f "/$dropin" ]]; then
    paths+=("$dropin")
    had_dropin=1
fi
tar -czf "$backup/files.tar.gz" -C / "${paths[@]}"
rollback() {
    echo "Observability upgrade failed; restoring $backup" >&2
    systemctl stop v2ray-cpu-diagnose.service v2ray-sidecar-dashboard-render.timer || true
    systemctl disable v2ray-cpu-diagnose.service || true
    if [[ "$had_dropin" == 0 ]]; then
        rm -f "/$dropin"
    fi
    tar -xzf "$backup/files.tar.gz" -C /
    systemctl daemon-reload
    systemctl start v2ray-sidecar-dashboard-render.timer v2ray-cpu-diagnose.timer || true
}
trap rollback ERR
systemctl stop v2ray-sidecar-dashboard-render.timer v2ray-sidecar-dashboard-render.service
systemctl disable --now v2ray-cpu-diagnose.timer
systemctl stop v2ray-cpu-diagnose.service
install -m 0644 "$stage/deploy/sidecar_stats_store.py" /usr/local/lib/v2ray-egress-sidecar/sidecar_stats_store.py
install -m 0644 "$stage/deploy/v2ray_cpu_watch.py" /usr/local/lib/v2ray-egress-sidecar/v2ray_cpu_watch.py
install -m 0755 "$stage/deploy/v2ray-egress-dashboard-render.py" /usr/local/bin/v2ray-sidecar-dashboard-render
install -m 0755 "$stage/deploy/v2ray-cpu-diagnose.sh" /usr/local/bin/v2ray-cpu-diagnose
install -m 0644 "$stage/systemd/v2ray-sidecar-dashboard-render.service" /etc/systemd/system/
install -m 0644 "$stage/systemd/v2ray-sidecar-dashboard-render.timer" /etc/systemd/system/
install -m 0644 "$stage/systemd/v2ray-cpu-diagnose.service" /etc/systemd/system/
install -d /etc/systemd/system/v2ray-cpu-diagnose.service.d
install -m 0644 "$stage/systemd/v2ray-cpu-diagnose-legacy.conf" /etc/systemd/system/v2ray-cpu-diagnose.service.d/observability.conf
install -d -m 0700 /var/log/v2ray-cpu-diagnostics
python3 -m py_compile /usr/local/lib/v2ray-egress-sidecar/sidecar_stats_store.py /usr/local/lib/v2ray-egress-sidecar/v2ray_cpu_watch.py /usr/local/bin/v2ray-sidecar-dashboard-render
systemctl daemon-reload
systemctl enable --now v2ray-cpu-diagnose.service
systemctl start v2ray-sidecar-dashboard-render.service
systemctl start v2ray-sidecar-dashboard-render.timer
[[ "$before" == "$(systemctl show -p MainPID "${units[@]}")" ]]
[[ "$hashes" == "$(sha256sum /etc/v2ray-rust-sidecar/config.toml /etc/v2ray/config.json)" ]]
systemctl is-active v2ray.service v2ray-rust-sidecar.service v2ray-cpu-diagnose.service v2ray-sidecar-dashboard-render.timer
trap - ERR
echo "Backup: $backup"
