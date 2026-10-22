#!/usr/bin/env python3
import argparse
import json
import os
import sys
import tempfile
import time
from collections import defaultdict


sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "v2ray-egress-sidecar"))
from sidecar_services import service_name
from sidecar_stats_store import StatsStore, pressure


def tail_lines(path, max_lines, max_bytes):
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        size = min(end, max_bytes)
        fh.seek(end - size)
        data = fh.read(size)
    lines = data.decode("utf-8", "ignore").splitlines()
    return lines[-max_lines:]


def event_ms(row):
    for key in ("ended_at_unix_ms", "started_at_unix_ms"):
        if key in row:
            try:
                return int(float(row[key]))
            except (TypeError, ValueError):
                pass
    if "ts" in row:
        try:
            return int(float(row["ts"]) * 1000)
        except (TypeError, ValueError):
            pass
    return 0


def host_from_row(row):
    return (row.get("target_host") or row.get("domain") or "").lower().strip(".")


def udp_class(row):
    port = int(row.get("target_port") or row.get("port") or 0)
    host = host_from_row(row)
    if port == 443:
        return "quic-or-http3"
    if port in (3478, 5349, 19302, 19305):
        return "stun-turn"
    if port == 123:
        return "ntp"
    if port == 53:
        return "dns"
    if port in (500, 4500):
        return "ipsec-ike"
    if port >= 1024:
        return "udp-high-port"
    return "udp-port-%d" % port if port else host or "udp-unknown"


def bytes_human(value):
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return "%d B" % value if unit == "B" else "%.2f %s" % (value, unit)
        value /= 1024.0


def duration_human(value_ms):
    value_ms = int(round(float(value_ms or 0)))
    if value_ms < 1000:
        return "%d ms" % value_ms
    seconds = value_ms // 1000
    if seconds < 60:
        if seconds < 10 and value_ms % 1000:
            return "%.1fs" % (value_ms / 1000.0)
        return "%ds" % seconds
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return "%dm %ds" % (minutes, seconds) if seconds else "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh %dm" % (hours, minutes) if minutes else "%dh" % hours
    days, hours = divmod(hours, 24)
    return "%dd %dh" % (days, hours) if hours else "%dd" % days


def is_error(row):
    return row.get("error") is not None or row.get("status") not in (None, "ok")


def add_group(groups, key, row):
    item = groups[key]
    up = int(row.get("uplink_bytes") or 0)
    down = int(row.get("downlink_bytes") or 0)
    elapsed = int(row.get("elapsed_ms") or 0)
    item["conns"] += 1
    item["ok"] += 0 if is_error(row) else 1
    item["errors"] += 1 if is_error(row) else 0
    item["uplink"] += up
    item["downlink"] += down
    item["total"] += up + down
    item["elapsed"] += elapsed


def table(groups, limit=20):
    rows = []
    for key, item in groups.items():
        avg_ms = int(item["elapsed"] / item["conns"]) if item["conns"] else 0
        rows.append({
            "name": key[0],
            "outbound": key[1] if len(key) > 1 else "",
            "conns": item["conns"],
            "ok": item["ok"],
            "errors": item["errors"],
            "uplink": item["uplink"],
            "downlink": item["downlink"],
            "total": item["total"],
            "total_human": bytes_human(item["total"]),
            "avg_ms": avg_ms,
            "avg_lifetime": duration_human(avg_ms),
        })
    rows.sort(key=lambda r: (r["total"], r["conns"]), reverse=True)
    return rows[:limit]


def summarize(rows, since_ms, label):
    now_ms = int(time.time() * 1000)
    selected = [r for r in rows if event_ms(r) >= now_ms - since_ms]
    groups_domain = defaultdict(lambda: defaultdict(int))
    groups_service = defaultdict(lambda: defaultdict(int))
    groups_outbound = defaultdict(lambda: defaultdict(int))
    groups_udp = defaultdict(lambda: defaultdict(int))
    groups_error = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(int)

    for row in selected:
        host = host_from_row(row)
        outbound = row.get("outbound") or "(none)"
        network = row.get("network") or "unknown"
        up = int(row.get("uplink_bytes") or 0)
        down = int(row.get("downlink_bytes") or 0)
        totals["conns"] += 1
        totals["ok"] += 0 if is_error(row) else 1
        totals["errors"] += 1 if is_error(row) else 0
        totals["uplink"] += up
        totals["downlink"] += down
        totals["total"] += up + down
        totals["tcp"] += 1 if network == "tcp" else 0
        totals["udp"] += 1 if network == "udp" else 0
        totals["unknown"] += 1 if network == "unknown" else 0
        add_group(groups_domain, (host or "(unknown)", outbound), row)
        add_group(groups_service, (service_name(host), outbound), row)
        add_group(groups_outbound, (outbound,), row)
        if network == "udp":
            add_group(groups_udp, (udp_class(row), outbound), row)
        if is_error(row):
            add_group(groups_error, (host or "(unknown)", outbound), row)

    totals["uplink_human"] = bytes_human(totals["uplink"])
    totals["downlink_human"] = bytes_human(totals["downlink"])
    totals["total_human"] = bytes_human(totals["total"])
    return {
        "label": label,
        "totals": dict(totals),
        "domains": table(groups_domain),
        "services": table(groups_service),
        "outbounds": table(groups_outbound),
        "udp": table(groups_udp),
        "errors": table(groups_error, 10),
    }


def write_atomic(path, text):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def render_html():
    return r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V2Ray Egress Sidecar Stats</title>
<style>
:root{color-scheme:light;--bg:#f7f8f4;--panel:#fff;--text:#1d252c;--muted:#68717a;--line:#d9ded6;--accent:#0f766e;--warn:#a16207;--bad:#b42318}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}
header{border-bottom:1px solid var(--line);background:#ffffffd9;position:sticky;top:0;backdrop-filter:saturate(140%) blur(8px);z-index:2}
.wrap{max-width:1180px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0}h2{font-size:15px;margin:24px 0 10px}.sub{color:var(--muted);font-size:12px;margin-top:4px}
.tabs{display:flex;gap:6px;margin-top:12px;flex-wrap:wrap}.tabs button,.tabs a{border:1px solid var(--line);background:#fff;color:var(--text);padding:7px 10px;border-radius:6px;cursor:pointer;text-decoration:none}.tabs button.active{border-color:var(--accent);color:#fff;background:var(--accent)}
.kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-top:14px}.kpi{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;min-height:72px}.kpi .label{color:var(--muted);font-size:12px}.kpi .value{font-size:19px;font-weight:650;margin-top:4px;overflow-wrap:anywhere}
.grid{display:grid;grid-template-columns:1fr;gap:16px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:auto}
table{width:100%;min-width:860px;border-collapse:collapse;table-layout:auto}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left;white-space:normal;overflow-wrap:anywhere;min-width:260px}th{color:var(--muted);font-weight:600;font-size:12px;background:#fbfcfa}tr:last-child td{border-bottom:0}.err{color:var(--bad);font-weight:650}.ok{color:var(--accent);font-weight:650}.empty{color:var(--muted);padding:12px}
@media (max-width:900px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.grid{grid-template-columns:1fr}.wrap{padding:12px}th:nth-child(4),td:nth-child(4),th:nth-child(5),td:nth-child(5){display:none}}
</style>
</head>
<body>
<header><div class="wrap"><h1>V2Ray Egress Sidecar Stats</h1><div id="meta" class="sub">Loading...</div><div class="tabs" id="tabs"></div></div></header>
<main class="wrap">
<section class="kpis" id="kpis"></section>
<section><h2>V2Ray CPU Diagnostics</h2><div id="diagnostics"></div></section>
<section class="grid">
<div><h2>Top Domains</h2><div class="panel" id="domains"></div></div>
<div><h2>Top Services</h2><div class="panel" id="services"></div></div>
<div><h2>Outbounds</h2><div class="panel" id="outbounds"></div></div>
<div><h2>UDP Classes</h2><div class="panel" id="udp"></div></div>
<div><h2>Errors</h2><div class="panel" id="errors"></div></div>
</section>
</main>
<script>
let data=null, active='5m';
const fmt=n=>Number(n||0).toLocaleString();
function esc(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function cell(v, cls=''){const s=esc(v);return `<td class="${cls}" title="${s}">${s}</td>`}
function duration(ms){
 ms=Number(ms||0);
 if(ms<1000)return `${Math.round(ms)} ms`;
 let s=Math.floor(ms/1000);
 if(s<60)return (s<10&&ms%1000)?`${(ms/1000).toFixed(1)}s`:`${s}s`;
 let m=Math.floor(s/60);s=s%60;
 if(m<60)return s?`${m}m ${s}s`:`${m}m`;
 let h=Math.floor(m/60);m=m%60;
 if(h<24)return m?`${h}h ${m}m`:`${h}h`;
 let d=Math.floor(h/24);h=h%24;
 return h?`${d}d ${h}h`:`${d}d`;
}
function table(rows){
 if(!rows||!rows.length)return '<div class="empty">No rows</div>';
 return `<table><thead><tr><th>Name</th><th>Out</th><th>Conns</th><th>OK</th><th>Err</th><th>Total</th><th title="Average connection/session lifetime">Avg Lifetime</th></tr></thead><tbody>`+
 rows.map(r=>`<tr>${cell(r.name)}${cell(r.outbound)}${cell(fmt(r.conns))}${cell(fmt(r.ok),'ok')}${cell(fmt(r.errors),r.errors?'err':'')}${cell(r.total_human)}${cell(r.avg_lifetime||duration(r.avg_ms))}</tr>`).join('')+
 '</tbody></table>';
}
function render(){
 const w=data.windows[active]||Object.values(data.windows)[0], t=w.totals;
 document.getElementById('meta').textContent=`Updated ${data.generated_at_text} | ${fmt(data.loaded_rows)} records | log ${data.stats_size_human} | ${data.collection_status||'ok'} | backlog ${fmt(data.backlog_bytes)} B`;
 document.getElementById('tabs').innerHTML=Object.keys(data.windows).map(k=>`<button class="${k===active?'active':''}" onclick="active='${k}';render()">${data.windows[k].label}</button>`).join('')+'<a href="control.html">Routing</a>';
 document.getElementById('kpis').innerHTML=[
  ['Connections',fmt(t.conns)],['Traffic',t.total_human||'0 B'],['Up',t.uplink_human||'0 B'],['Down',t.downlink_human||'0 B'],['TCP / UDP',`${fmt(t.tcp)} / ${fmt(t.udp)}`],['Errors',fmt(t.errors)]
 ].map(x=>`<div class="kpi"><div class="label">${x[0]}</div><div class="value">${x[1]}</div></div>`).join('');
 for(const id of ['domains','services','outbounds','udp','errors'])document.getElementById(id).innerHTML=table(w[id]);
 const d=data.diagnostics||{}, s=d.status||{}, v=s.latest_sample||{}, incident=d.incident;
 document.getElementById('diagnostics').textContent=`监测: ${d.state||'unknown'} | 当前采样 CPU: ${v.cpu_percent??'--'}% | PID: ${v.pid||'--'} | 最近事件: `+
 (incident?`${new Date(incident.timestamp*1000).toLocaleString()}，峰值 ${incident.peak_cpu_percent}%\n`+incident.findings.map(f=>f.evidence).join('\n'):'尚无超阈值事件');
 document.getElementById('diagnostics').style.whiteSpace='pre-wrap';
}
async function load(){
 const res=await fetch('stats.json?ts='+Date.now(),{cache:'no-store'});
 data=await res.json(); render();
}
load().catch(e=>{document.getElementById('meta').textContent='Load failed: '+e});
setInterval(load,15000);
</script>
</body>
</html>
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stats", default="/var/lib/v2ray-egress-sidecar/stats.jsonl")
    parser.add_argument("--out", default="/var/www/v2ray-egress-sidecar-dashboard")
    parser.add_argument('--database', help='Persistent incremental SQLite database (outside web root)')
    parser.add_argument('--ignore-pressure', action='store_true', help='For isolated tests only')
    parser.add_argument('--rotate-bytes', type=int, default=64 * 1024 * 1024)
    parser.add_argument('--retention-days', type=int, default=3)
    parser.add_argument('--diagnostics', default='/var/log/v2ray-cpu-diagnostics')
    parser.add_argument("--max-lines", type=int, default=30000)
    parser.add_argument("--max-bytes", type=int, default=4 * 1024 * 1024)
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    now = time.time()
    reason = None if args.ignore_pressure else pressure()
    if reason:
        # Preserve last good statistics and expose why collection is delayed.
        previous = os.path.join(args.out, 'stats.json')
        if os.path.exists(previous):
            with open(previous, encoding='utf-8') as fh:
                old = json.load(fh)
            old['collection_status'] = 'paused: ' + reason
            write_atomic(previous, json.dumps(old, separators=(',', ':')))
        print('paused: ' + reason)
        return
    store = StatsStore(args.database or args.stats + '.sqlite3')
    try:
        progress = store.ingest(args.stats, args.max_bytes, now)
        store.rotate(args.stats, args.rotate_bytes, args.retention_days, now)
        windows = {key: store.summary(ms, label, now) for key, ms, label in
                   [('5m', 300000, '5 min'), ('1h', 3600000, '1 hour'), ('24h', 86400000, '24 hours')]}
        loaded_rows = store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    finally:
        store.close()
    for window in windows.values():
        for key in ('uplink', 'downlink', 'total'):
            window['totals'][key + '_human'] = bytes_human(window['totals'][key])
        for key in ('domains', 'services', 'outbounds', 'udp', 'errors'):
            for row in window[key]:
                row['total_human'] = bytes_human(row['total'])
                row['avg_lifetime'] = duration_human(row['avg_ms'])
    diagnostics = {'state': 'unavailable'}
    try:
        with open(os.path.join(args.diagnostics, 'status.json'), encoding='utf-8') as fh:
            diagnostics['status'] = json.loads(fh.read(262144))
        status = diagnostics['status']
        diagnostics['state'] = status.get('state') if now - status.get('updated_at', 0) < 90 else 'stale heartbeat'
        incident_file = os.path.join(args.diagnostics, 'latest.json')
        if os.path.exists(incident_file):
            with open(incident_file, encoding='utf-8') as fh:
                incident = json.loads(fh.read(2 * 1024 * 1024))
            diagnostics['incident'] = {'timestamp': incident['timestamp'], 'pid': incident['pid'],
                                       'peak_cpu_percent': max(s['cpu_percent'] for s in incident['samples']),
                                       'findings': incident['findings']}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    data = {
        "generated_at": int(now),
        "generated_at_text": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(now)),
        "loaded_rows": loaded_rows,
        "collection_status": 'catching up' if progress['backlog_bytes'] else 'ok',
        "diagnostics": diagnostics,
        **progress,
        "stats_size": os.path.getsize(args.stats) if os.path.exists(args.stats) else 0,
        "stats_size_human": bytes_human(os.path.getsize(args.stats)) if os.path.exists(args.stats) else "0 B",
        "windows": windows,
    }
    write_atomic(os.path.join(args.out, "stats.json"), json.dumps(data, separators=(",", ":")))
    write_atomic(os.path.join(args.out, "index.html"), render_html())


if __name__ == "__main__":
    main()
