"""Synthetic performance check. Run on a helper, not the forwarding server."""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'deploy'))
from sidecar_stats_store import StatsStore


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, 'stats.jsonl')
        now = time.time()
        with open(path, 'w') as fh:
            for i in range(100000):
                row = dict(ended_at_unix_ms=int((now - i % 80000) * 1000), target_host='host%d.example.com' % (i % 500),
                           outbound='direct', network='tcp', uplink_bytes=1024, downlink_bytes=8192, elapsed_ms=5000)
                fh.write(json.dumps(row) + '\n')
        store = StatsStore(os.path.join(directory, 'db.sqlite3'))
        start = time.monotonic()
        while store.ingest(path)['backlog_bytes']:
            pass
        initial_seconds = time.monotonic() - start
        start = time.monotonic()
        incremental = store.ingest(path)
        windows = [store.summary(ms, label) for ms, label in ((300000, '5m'), (3600000, '1h'), (86400000, '24h'))]
        update_seconds = time.monotonic() - start
        count = windows[-1]['totals']['conns']
        assert count == 100000, count
        assert incremental['read_bytes'] == 0, incremental
        print(json.dumps(dict(rows=count, source_bytes=os.path.getsize(path), initial_seconds=round(initial_seconds, 3),
                              incremental_summary_seconds=round(update_seconds, 3), unchanged_read_bytes=incremental['read_bytes'],
                              database_bytes=os.path.getsize(os.path.join(directory, 'db.sqlite3')))))
        store.close()


if __name__ == '__main__':
    main()
