"""Bounded, transactional JSONL ingestion. No hooks into the forwarding process."""
import glob
import json
import os
import sqlite3
import time

from sidecar_services import service_name


def pressure(proc='/proc', cpu_limit=80, connection_limit=2048):
    def cpu():
        with open(os.path.join(proc, 'stat')) as fh:
            v = [int(x) for x in fh.readline().split()[1:9]]
        return sum(v), v[3] + v[4]
    try:
        a, idle_a = cpu()
        time.sleep(0.1)
        b, idle_b = cpu()
        usage = 100 * (1 - (idle_b - idle_a) / max(1, b - a))
        if usage >= cpu_limit:
            return 'cpu pressure: %.1f%%' % usage
        with open(os.path.join(proc, 'meminfo')) as fh:
            mem = {line.split(':')[0]: int(line.split()[1]) for line in fh}
        if mem.get('MemAvailable', mem['MemFree']) < mem['MemTotal'] * .2:
            return 'available memory below 20%'
        connections = 0
        for name in ('tcp', 'tcp6'):
            with open(os.path.join(proc, 'net', name)) as fh:
                connections += sum(1 for line in fh if len(line.split()) > 3 and line.split()[3] == '01')
        if connections >= connection_limit:
            return 'established socket pressure: %d' % connections
    except (OSError, ValueError, KeyError):
        if os.path.isdir(proc):
            return 'resource counters unavailable'
    return None


class StatsStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=2)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sources (
                identity TEXT PRIMARY KEY, offset INTEGER NOT NULL,
                updated REAL NOT NULL, anchor TEXT NOT NULL DEFAULT '', discarding INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
                ts INTEGER, host TEXT, service TEXT, outbound TEXT, network TEXT,
                udp TEXT, error TEXT, up INTEGER, down INTEGER, elapsed INTEGER);
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        ''')

    def close(self):
        self.db.close()

    def ingest(self, path, max_bytes=4 * 1024 * 1024, now=None):
        now = time.time() if now is None else now
        count, invalid, consumed = 0, 0, 0
        files = sorted(glob.glob(path + '.archive-*')) + ([path] if os.path.exists(path) else [])
        for source in files:
            if consumed >= max_bytes:
                break
            with open(source, 'rb') as fh:
                self.db.execute('BEGIN IMMEDIATE')
                st = os.fstat(fh.fileno())
                identity = '%s:%s' % (st.st_dev, st.st_ino)
                cursor = self.db.execute('SELECT offset,anchor,discarding FROM sources WHERE identity=?', (identity,)).fetchone()
                offset = cursor[0] if cursor else 0
                discarding = cursor[2] if cursor else 0
                # Detect copytruncate/replacement even when a new file has regrown.
                if offset:
                    fh.seek(max(0, offset - 64))
                    anchor = fh.read(min(64, offset)).hex()
                    if st.st_size < offset or anchor != cursor[1]:
                        offset = 0
                        discarding = 0
                fh.seek(offset)
                batch = []
                while consumed < max_bytes:
                    start = fh.tell()
                    allowance = min(65536, max_bytes - consumed)
                    line = fh.readline(allowance)
                    if not line:
                        break
                    if discarding:
                        consumed += len(line)
                        offset = fh.tell()
                        if line.endswith(b'\n'):
                            discarding = 0
                        continue
                    if not line.endswith(b'\n'):
                        if len(line) == 65536:
                            # Persist discard state, so an oversized malformed record
                            # cannot block collection or consume unbounded memory.
                            discarding = 1
                            invalid += 1
                            consumed += len(line)
                            offset = fh.tell()
                            continue
                        fh.seek(start)  # Retry incomplete records next time.
                        break
                    consumed += len(line)
                    offset = fh.tell()
                    try:
                        row = json.loads(line.decode('utf-8'))
                        ts = int(row.get('ended_at_unix_ms') or row.get('started_at_unix_ms') or float(row.get('ts', 0)) * 1000)
                        if ts < (now - 86400) * 1000 or ts > (now + 300) * 1000:
                            continue
                        host = str(row.get('target_host') or row.get('domain') or '(unknown)').lower().strip('.')[:253]
                        network = str(row.get('network') or 'unknown')[:16]
                        port = int(row.get('target_port') or row.get('port') or 0)
                        udp = ('quic-or-http3' if port == 443 else 'stun-turn' if port in (3478, 5349, 19302, 19305)
                               else 'ntp' if port == 123 else 'dns' if port == 53 else 'ipsec-ike' if port in (500, 4500)
                               else 'udp-high-port' if port >= 1024 else 'udp-port-%d' % port)
                        error = row.get('error')
                        if error is None and row.get('status') not in (None, 'ok'):
                            error = row['status']
                        batch.append((ts, host, service_name(host), str(row.get('outbound') or '(none)')[:128], network,
                                      udp, str(error)[:512] if error is not None else None,
                                      max(0, int(row.get('uplink_bytes') or 0)), max(0, int(row.get('downlink_bytes') or 0)),
                                      max(0, int(row.get('elapsed_ms') or 0))))
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        invalid += 1
                fh.seek(max(0, offset - 64))
                anchor = fh.read(min(64, offset)).hex()
                # Events and read cursor commit together; a crash cannot duplicate a batch.
                with self.db:
                    self.db.executemany('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)', batch)
                    self.db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?)', (identity, offset, now, anchor, discarding))
                count += len(batch)
        with self.db:
            self.db.execute('DELETE FROM events WHERE ts < ?', (int((now - 86400) * 1000),))
        return {'new_rows': count, 'invalid_rows': invalid, 'read_bytes': consumed,
                'backlog_bytes': self.backlog(path)}

    def backlog(self, path):
        result = 0
        for source in glob.glob(path + '.archive-*') + ([path] if os.path.exists(path) else []):
            st = os.stat(source)
            row = self.db.execute('SELECT offset FROM sources WHERE identity=?', ('%s:%s' % (st.st_dev, st.st_ino),)).fetchone()
            result += max(0, st.st_size - (row[0] if row else 0))
        return result

    def rotate(self, path, max_bytes=64 * 1024 * 1024, retention_days=3, now=None):
        now = time.time() if now is None else now
        if os.path.exists(path) and os.stat(path).st_size >= max_bytes:
            st = os.stat(path)
            cursor = self.db.execute('SELECT offset FROM sources WHERE identity=?', ('%s:%s' % (st.st_dev, st.st_ino),)).fetchone()
            if cursor and cursor[0] == st.st_size:
                # Rust opens the pathname on each append. Rename retains late writes;
                # the next ingestion scans this inode again before the new active file.
                os.rename(path, path + '.archive-%d-%d' % (int(now * 1000000), st.st_ino))
        for source in glob.glob(path + '.archive-*'):
            st = os.stat(source)
            cursor = self.db.execute('SELECT offset,updated FROM sources WHERE identity=?', ('%s:%s' % (st.st_dev, st.st_ino),)).fetchone()
            if cursor and cursor[0] == st.st_size and now - st.st_mtime > retention_days * 86400:
                os.unlink(source)

    def summary(self, since_ms, label, now=None):
        now = time.time() if now is None else now
        cutoff = int(now * 1000 - since_ms)
        fields = 'COUNT(*),SUM(error IS NULL),SUM(error IS NOT NULL),SUM(up),SUM(down),SUM(up+down),SUM(elapsed)'
        totals_row = self.db.execute('SELECT ' + fields + ",SUM(network='tcp'),SUM(network='udp'),SUM(network='unknown') FROM events WHERE ts>=?", (cutoff,)).fetchone()
        totals = dict(zip(('conns', 'ok', 'errors', 'uplink', 'downlink', 'total', 'elapsed', 'tcp', 'udp', 'unknown'), [v or 0 for v in totals_row]))
        result = {'label': label, 'totals': totals}
        for name, column, extra, limit in [('domains', 'host', '', 20), ('services', 'service', '', 20),
                                            ('outbounds', 'outbound', '', 20), ('udp', 'udp', " AND network='udp'", 20),
                                            ('errors', 'host', ' AND error IS NOT NULL', 10)]:
            outbound = "''" if name == 'outbounds' else 'outbound'
            sql = ('SELECT %s,%s,%s FROM events WHERE ts>=?%s GROUP BY 1,2 ORDER BY SUM(up+down) DESC,COUNT(*) DESC LIMIT %d'
                   % (column, outbound, fields, extra, limit))
            result[name] = []
            for r in self.db.execute(sql, (cutoff,)):
                item = dict(zip(('name', 'outbound', 'conns', 'ok', 'errors', 'uplink', 'downlink', 'total', 'elapsed'), r))
                item['avg_ms'] = int(item['elapsed'] / item['conns'])
                result[name].append(item)
        return result
