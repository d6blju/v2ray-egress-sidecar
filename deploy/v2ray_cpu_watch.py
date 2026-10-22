#!/usr/bin/env python3
"""Read-only CPU evidence collector; never signals or restarts V2Ray."""
import argparse
import collections
import glob
import json
import os
import sqlite3
import subprocess
import time


def atomic(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def proc_stat(path):
    with open(path) as fh:
        raw = fh.read()
    # comm may contain spaces or parentheses. Fields below start at state (#3).
    fields = raw[raw.rfind(')') + 2:].split()
    return {'ticks': int(fields[11]) + int(fields[12]), 'start': int(fields[19]),
            'rss': int(fields[21]) * os.sysconf('SC_PAGE_SIZE'), 'state': fields[0]}


def sample(pid, proc='/proc'):
    data = proc_stat(os.path.join(proc, str(pid), 'stat'))
    data.update(pid=pid, monotonic=time.monotonic(), timestamp=time.time())
    with open(os.path.join(proc, 'stat')) as fh:
        v = [int(x) for x in fh.readline().split()[1:9]]
    data.update(host_ticks=sum(v), idle_ticks=v[3] + v[4], iowait_ticks=v[4], steal_ticks=v[7])
    data['threads'] = {}
    for path in glob.glob(os.path.join(proc, str(pid), 'task', '*', 'stat')):
        try:
            data['threads'][os.path.basename(os.path.dirname(path))] = proc_stat(path)
        except (OSError, ValueError, IndexError):
            pass
    data['network_bytes'] = 0
    # Listing entries is cheap; no per-descriptor readlink in the sampling loop.
    data['fd_count'] = len(os.listdir(os.path.join(proc, str(pid), 'fd')))
    with open(os.path.join(proc, 'net', 'dev')) as fh:
        for line in fh:
            if ':' in line and line.split(':')[0].strip() != 'lo':
                v = line.split(':')[1].split()
                data['network_bytes'] += int(v[0]) + int(v[8])
    return data


def delta(before, after, hz=None):
    if (before['pid'], before['start']) != (after['pid'], after['start']):
        return None
    seconds = after['monotonic'] - before['monotonic']
    if seconds <= 0 or after['ticks'] < before['ticks']:
        return None
    hz = hz or os.sysconf('SC_CLK_TCK')
    host = max(1, after['host_ticks'] - before['host_ticks'])
    threads = []
    for tid, row in after['threads'].items():
        previous = before['threads'].get(tid)
        if previous and row['start'] == previous['start']:
            threads.append({'tid': tid, 'cpu_percent': round(max(0, row['ticks'] - previous['ticks']) / hz / seconds * 100, 2), 'state': row['state']})
    return {'timestamp': after['timestamp'], 'pid': after['pid'], 'start': after['start'],
            'sample_seconds': round(seconds, 3),
            'cpu_percent': round((after['ticks'] - before['ticks']) / hz / seconds * 100, 2),
            'host_cpu_percent': round(100 * (1 - (after['idle_ticks'] - before['idle_ticks']) / host), 2),
            'iowait_percent': round(100 * (after['iowait_ticks'] - before['iowait_ticks']) / host, 2),
            'steal_percent': round(100 * (after['steal_ticks'] - before['steal_ticks']) / host, 2),
            'host_network_bytes_per_sec': max(0, int((after['network_bytes'] - before['network_bytes']) / seconds)),
            'rss_bytes': after['rss'], 'fd_count': after.get('fd_count', 0),
            'threads': sorted(threads, key=lambda x: x['cpu_percent'], reverse=True)[:20]}


def command(args):
    try:
        # Bound both elapsed time and captured output; never include configuration/secrets.
        with tempfile_output() as output:
            subprocess.run(args, stdout=output, stderr=subprocess.STDOUT, timeout=6)
            output.seek(0)
            return output.read(32768).decode('utf-8', 'replace')
    except (OSError, subprocess.TimeoutExpired) as err:
        return 'unavailable: ' + str(err)


def tempfile_output():
    import tempfile
    return tempfile.TemporaryFile()


def recent_stats(database, now):
    if not os.path.exists(database):
        return {'available': False, 'reason': 'incremental statistics database not ready'}
    db = sqlite3.connect('file:' + database + '?mode=ro', uri=True, timeout=.2)
    try:
        cutoff = int((now - 120) * 1000)
        groups = db.execute('SELECT host,service,outbound,network,COUNT(*),SUM(up+down),SUM(error IS NOT NULL) FROM events WHERE ts>=? GROUP BY 1,2,3,4 ORDER BY SUM(up+down) DESC LIMIT 20', (cutoff,)).fetchall()
        errors = db.execute('SELECT error,COUNT(*) FROM events WHERE ts>=? AND error IS NOT NULL GROUP BY error ORDER BY COUNT(*) DESC LIMIT 10', (cutoff,)).fetchall()
        latest = db.execute('SELECT MAX(ts) FROM events').fetchone()[0]
        return {'available': True, 'latest_event_ms': latest, 'window_seconds': 120,
                'scope': 'completed sidecar sessions only; active TLS domains and inbound user IDs are not observable here',
                'top_completed_sessions': [dict(zip(('host', 'service', 'outbound', 'network', 'connections', 'bytes', 'errors'), r)) for r in groups],
                'errors': [dict(error=r[0], count=r[1]) for r in errors]}
    finally:
        db.close()


def assess(samples, context):
    findings = []
    peak = max(samples, key=lambda s: s['cpu_percent'])
    if peak['host_network_bytes_per_sec'] >= 1024 * 1024:
        findings.append({'type': 'traffic_correlated', 'confidence': 'possible', 'evidence': 'high CPU coincides with >=1 MiB/s host traffic; host traffic includes other processes'})
    elif peak['cpu_percent'] >= 80:
        findings.append({'type': 'cpu_without_large_host_traffic', 'confidence': 'possible', 'evidence': 'high CPU with <1 MiB/s host traffic; busy loop, encryption, GC or small-packet workload require a CPU profile to distinguish'})
    if peak['iowait_percent'] >= 20 or peak['steal_percent'] >= 20:
        findings.append({'type': 'host_contention', 'confidence': 'observed', 'evidence': 'I/O wait or hypervisor steal >=20% during capture'})
    errors = context.get('recent_stats', {}).get('errors', [])
    dns = sum(r['count'] for r in errors if any(w in r['error'].lower() for w in ('lookup', 'resolve', 'dns', 'name or service')))
    if dns:
        findings.append({'type': 'dns_errors', 'confidence': 'observed', 'count': dns, 'evidence': 'completed sidecar sessions report DNS errors; causation of V2Ray CPU is unproven'})
    if (context.get('fd_count') or 0) >= 800:
        findings.append({'type': 'many_descriptors', 'confidence': 'observed', 'evidence': '>=800 open descriptors; a single snapshot does not establish a leak'})
    if len(samples) >= 30 and samples[-1].get('fd_count', 0) - samples[0].get('fd_count', 0) >= 100:
        findings.append({'type': 'descriptor_growth', 'confidence': 'possible', 'evidence': 'open descriptors increased by >=100 within the sample history; growing legitimate traffic can also cause this, so a leak is not confirmed'})
    findings.append({'type': 'profile_needed', 'confidence': 'unknown', 'evidence': 'thread CPU identifies the busy OS thread, not the Go function. No intrusive profiler or process signals were used.'})
    return findings


class Watcher:
    def __init__(self, out, database, threshold=80, cooldown=300):
        self.out, self.database, self.threshold, self.cooldown = out, database, threshold, cooldown
        os.makedirs(out, mode=0o700, exist_ok=True)
        os.chmod(out, 0o700)
        self.history = collections.deque(maxlen=60)
        self.pending = None
        self.last_report = 0
        try:
            with open(os.path.join(out, 'status.json')) as fh:
                self.last_report = json.load(fh).get('last_report_unix', 0)
        except (OSError, ValueError):
            pass

    def observe(self, value):
        if self.history and self.history[-1]['start'] != value['start']:
            self.history.clear()
            self.pending = None
        self.history.append(value)
        if self.pending:
            self.pending.append(value)
            if len(self.pending) >= 6:
                self.capture(list(self.history)[:-6] + self.pending)
                self.pending = None
                self.last_report = value['timestamp']
                return True
        elif value['cpu_percent'] >= self.threshold and value['timestamp'] - self.last_report >= self.cooldown:
            self.pending = [value]
        return False

    def capture(self, samples):
        latest = samples[-1]
        pid = latest['pid']
        try:
            fd_count = len(os.listdir('/proc/%d/fd' % pid))
        except OSError:
            fd_count = None
        context = {'fd_count': fd_count, 'sockets': command(['ss', '-Htanp']),
                   'processes': command(['ps', '-eo', 'pid,ppid,stat,pcpu,rss,comm', '--sort=-pcpu']),
                   'v2ray_journal': command(['journalctl', '-u', 'v2ray.service', '--since', '2 minutes ago', '-n', '80', '--no-pager']),
                   'sidecar_journal': command(['journalctl', '-u', 'v2ray-rust-sidecar.service', '--since', '2 minutes ago', '-n', '80', '--no-pager'])}
        try:
            context['recent_stats'] = recent_stats(self.database, latest['timestamp'])
        except (sqlite3.Error, OSError) as err:
            context['recent_stats'] = {'available': False, 'reason': str(err)}
        report = {'schema_version': 1, 'timestamp': latest['timestamp'], 'pid': pid,
                  'threshold_percent': self.threshold, 'samples': samples, 'context': context,
                  'findings': assess(samples, context), 'automatic_action': 'evidence collection only'}
        name = 'v2ray-%s.json' % time.strftime('%Y%m%d-%H%M%S', time.localtime(latest['timestamp']))
        atomic(os.path.join(self.out, name), report)
        atomic(os.path.join(self.out, 'latest.json'), report)
        text = os.path.join(self.out, 'latest.txt')
        with open(text + '.tmp', 'w', encoding='utf-8') as fh:
            fh.write('V2Ray CPU incident PID %d\n' % pid)
            fh.write('Peak CPU: %.2f%%\n' % max(s['cpu_percent'] for s in samples))
            for finding in report['findings']:
                fh.write('%s [%s]: %s\n' % (finding['type'], finding['confidence'], finding['evidence']))
            fh.write('Full evidence: ' + name + '\n')
        os.chmod(text + '.tmp', 0o600)
        os.replace(text + '.tmp', text)
        for path in sorted(glob.glob(os.path.join(self.out, 'v2ray-*.json')))[:-20]:
            os.unlink(path)
        for path in glob.glob(os.path.join(self.out, 'v2ray-*.json')):
            if time.time() - os.stat(path).st_mtime > 3 * 86400:
                os.unlink(path)

    def status(self, latest, state='watching'):
        atomic(os.path.join(self.out, 'status.json'), {'state': state, 'updated_at': time.time(),
               'last_report_unix': self.last_report, 'latest_sample': latest,
               'threshold_percent': self.threshold, 'sample_interval_seconds': 1})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='/var/log/v2ray-cpu-diagnostics')
    parser.add_argument('--database', default=os.environ.get('SIDECAR_STATS_DATABASE', '/var/lib/v2ray-egress-sidecar/stats.jsonl.sqlite3'))
    parser.add_argument('--threshold', type=float, default=80)
    parser.add_argument('--cooldown', type=int, default=300)
    parser.add_argument('--once', action='store_true', help='One real sample and forced report for isolated deployment verification')
    args = parser.parse_args()
    watcher = Watcher(args.out, args.database, args.threshold, args.cooldown)
    before, last_status, pid, last_pid_check = None, 0, 0, 0
    while True:
        try:
            if time.monotonic() - last_pid_check >= 10 or not os.path.exists('/proc/%d/stat' % pid):
                output = command(['systemctl', 'show', '-p', 'MainPID', 'v2ray.service'])
                pid = int(output.strip().split('=')[-1])
                last_pid_check = time.monotonic()
            after = sample(pid)
            value = delta(before, after) if before else None
            before = after
            if value:
                if args.once:
                    watcher.capture([value])
                    watcher.status(value, 'test capture')
                    return
                captured = watcher.observe(value)
                if captured or time.monotonic() - last_status >= 30:
                    watcher.status(value)
                    last_status = time.monotonic()
        except (OSError, ValueError, IndexError) as err:
            before = None
            watcher.status(None, 'sampling unavailable: ' + str(err))
            if args.once:
                raise
        time.sleep(1)


if __name__ == '__main__':
    main()
