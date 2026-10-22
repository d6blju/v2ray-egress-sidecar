import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

DEPLOY = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'deploy')
sys.path.insert(0, DEPLOY)
from sidecar_stats_store import StatsStore, pressure
from v2ray_cpu_watch import Watcher, delta, assess

spec = importlib.util.spec_from_file_location('dashboard_incremental', os.path.join(DEPLOY, 'v2ray-egress-dashboard-render.py'))
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'stats.jsonl')
        self.now = time.time()
        self.store = StatsStore(os.path.join(self.tmp.name, 'store.db'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def row(self, **kwargs):
        row = dict(ended_at_unix_ms=int(self.now * 1000), target_host='github.com', outbound='direct',
                   network='tcp', uplink_bytes=100, downlink_bytes=300, elapsed_ms=4000, status='ok')
        row.update(kwargs)
        return row

    def append(self, rows):
        with open(self.path, 'ab') as fh:
            for row in rows:
                fh.write((json.dumps(row) + '\n').encode())

    def test_exactly_once_partial_invalid_and_restart(self):
        self.append([self.row()])
        with open(self.path, 'ab') as fh:
            fh.write(b'not json\n{"ts":')
        first = self.store.ingest(self.path, now=self.now)
        self.assertEqual((first['new_rows'], first['invalid_rows']), (1, 1))
        self.assertEqual(self.store.ingest(self.path, now=self.now)['new_rows'], 0)
        with open(self.path, 'ab') as fh:
            fh.write((str(self.now) + '}\n').encode())
        self.store.close()
        self.store = StatsStore(os.path.join(self.tmp.name, 'store.db'))
        self.assertEqual(self.store.ingest(self.path, now=self.now)['new_rows'], 1)
        self.assertEqual(self.store.summary(300000, '5m', self.now)['totals']['conns'], 2)

    @unittest.skipIf(os.name == 'nt', 'POSIX open-inode rotation; exercised on Linux before deployment')
    def test_rotation_late_writer_and_retention(self):
        self.append([self.row()])
        late = open(self.path, 'ab')
        try:
            self.store.ingest(self.path, now=self.now)
            self.store.rotate(self.path, max_bytes=1, now=self.now)
            self.assertFalse(os.path.exists(self.path))
            late.write((json.dumps(self.row(target_host='api.github.com')) + '\n').encode())
            late.flush()
            self.append([self.row(target_host='example.com')])
            self.assertEqual(self.store.ingest(self.path, now=self.now)['new_rows'], 2)
            self.assertEqual(self.store.summary(300000, '5m', self.now)['totals']['conns'], 3)
        finally:
            late.close()
        import glob
        archive = glob.glob(self.path + '.archive-*')[0]
        os.utime(archive, (self.now - 4 * 86400,) * 2)
        self.store.rotate(self.path, max_bytes=1000000, now=self.now)
        self.assertFalse(os.path.exists(archive))

    def test_sql_summaries_match_existing_semantics(self):
        rows = [self.row(), self.row(target_host='raw.githubusercontent.com', error='dns lookup failed'),
                self.row(network='udp', target_port=443, outbound='vpn'),
                self.row(ended_at_unix_ms=int((self.now - 600) * 1000), target_host='example.com')]
        self.append(rows)
        self.store.ingest(self.path, now=self.now)
        for window in (300000, 3600000):
            actual = self.store.summary(window, 'test', self.now)
            with patch.object(dashboard.time, 'time', return_value=self.now):
                expected = dashboard.summarize(rows, window, 'test')
            for key in ('conns', 'ok', 'errors', 'uplink', 'downlink', 'total', 'tcp', 'udp'):
                self.assertEqual(actual['totals'][key], expected['totals'].get(key, 0))
            for group in ('services', 'domains', 'outbounds', 'errors', 'udp'):
                self.assertEqual({(r['name'], r['outbound']): r['total'] for r in actual[group]},
                                 {(r['name'], r['outbound']): r['total'] for r in expected[group]})

    def test_truncate_and_byte_budget(self):
        self.append([self.row() for _ in range(50)])
        result = self.store.ingest(self.path, max_bytes=1024, now=self.now)
        self.assertGreater(result['backlog_bytes'], 0)
        self.assertLessEqual(result['read_bytes'], 1025)
        with open(self.path, 'wb') as fh:
            fh.write((json.dumps(self.row(target_host='new.example')) + '\n').encode())
        self.assertEqual(self.store.ingest(self.path, now=self.now)['new_rows'], 1)

    def test_expiry_without_new_events(self):
        self.append([self.row()])
        self.store.ingest(self.path, now=self.now)
        self.store.ingest(self.path, now=self.now + 86401)
        self.assertEqual(self.store.summary(86400000, '24h', self.now + 86401)['totals']['conns'], 0)

    def test_oversized_record_does_not_block_valid_records(self):
        with open(self.path, 'wb') as fh:
            fh.write(b'x' * 200000 + b'\n')
        self.append([self.row()])
        for _ in range(4):
            self.store.ingest(self.path, max_bytes=65536, now=self.now)
        self.assertEqual(self.store.summary(300000, '5m', self.now)['totals']['conns'], 1)
        self.assertEqual(self.store.backlog(self.path), 0)

    def test_resource_pause_preserves_last_good_page_and_read_cursor(self):
        self.append([self.row()])
        out = os.path.join(self.tmp.name, 'web')
        os.mkdir(out)
        previous = {'generated_at': 123, 'loaded_rows': 42, 'windows': {'5m': {'total': 9}}}
        with open(os.path.join(out, 'stats.json'), 'w') as fh:
            json.dump(previous, fh)
        argv = ['renderer', '--stats', self.path, '--out', out, '--database', os.path.join(self.tmp.name, 'paused.db')]
        with patch.object(sys, 'argv', argv), patch.object(dashboard, 'pressure', return_value='cpu pressure: 90%'):
            dashboard.main()
        with open(os.path.join(out, 'stats.json')) as fh:
            paused = json.load(fh)
        self.assertEqual(paused['windows'], previous['windows'])
        self.assertEqual(paused['loaded_rows'], 42)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'paused.db')))
        with patch.object(sys, 'argv', argv), patch.object(dashboard, 'pressure', return_value=None):
            dashboard.main()
        with open(os.path.join(out, 'stats.json')) as fh:
            resumed = json.load(fh)
        self.assertEqual(resumed['loaded_rows'], 1)
        self.assertEqual(resumed['collection_status'], 'ok')


class CpuTests(unittest.TestCase):
    def sample(self, tick=0, now=0, start=1):
        return dict(pid=42, start=start, ticks=tick, monotonic=now, timestamp=1000 + now,
                    host_ticks=int(now * 100), idle_ticks=0, iowait_ticks=0, steal_ticks=0,
                    threads={'43': dict(ticks=tick, start=1, state='R')}, rss=1000, network_bytes=0)

    def test_instant_cpu_accounts_for_throttled_wall_time_and_pid_reuse(self):
        value = delta(self.sample(), self.sample(180, 2), hz=100)
        self.assertEqual(value['cpu_percent'], 90)
        self.assertEqual(value['threads'][0]['cpu_percent'], 90)
        self.assertIsNone(delta(self.sample(), self.sample(100, 1, start=2), hz=100))

    def test_trigger_precapture_postcapture_and_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            watch = Watcher(directory, 'missing')
            with patch.object(watch, 'capture') as capture:
                for i in range(10):
                    value = delta(self.sample(i * 90, i), self.sample((i + 1) * 90, i + 1), hz=100)
                    watch.observe(value)
                self.assertEqual(capture.call_count, 1)
                self.assertGreaterEqual(len(capture.call_args[0][0]), 6)

    def test_findings_do_not_invent_root_cause(self):
        value = delta(self.sample(), self.sample(99, 1), hz=100)
        findings = assess([value], {'fd_count': None, 'recent_stats': {'errors': [{'error': 'DNS lookup failed', 'count': 2}]}})
        self.assertIn('dns_errors', [f['type'] for f in findings])
        self.assertIn('profile_needed', [f['type'] for f in findings])
        self.assertEqual(next(f for f in findings if f['type'] == 'cpu_without_large_host_traffic')['confidence'], 'possible')

    def test_report_files_are_complete_and_raw_evidence_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            watch = Watcher(directory, 'missing.db')
            value = delta(self.sample(), self.sample(99, 1), hz=100)
            with patch('v2ray_cpu_watch.command', return_value='bounded test evidence'):
                watch.capture([value])
            with open(os.path.join(directory, 'latest.json')) as fh:
                report = json.load(fh)
            self.assertEqual(report['pid'], 42)
            self.assertEqual(report['samples'][0]['cpu_percent'], 99)
            self.assertEqual(report['context']['recent_stats']['available'], False)
            self.assertEqual(report['automatic_action'], 'evidence collection only')


if __name__ == '__main__':
    unittest.main()
