import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'deploy'))
import sidecar_route_transaction as tx


class RouteTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, 'config.toml')
        self.state = os.path.join(self.tmp.name, 'state.json')
        tx.atomic(self.config, 'original config\n', 0o640)
        tx.atomic(self.state, '{"active":true,"country":"old"}\n')
        self.route = tx.RouteTransaction(self.config, self.state, self.tmp.name)
        self.old_state = tx.read_text(self.state)
        self.acks = []

    def confirm(self, cursor, text):
        self.acks.append(text)
        return {'confirmed': True, 'pid': 42}

    def run_route(self, **kwargs):
        params = dict(expected='original config\n', candidate='candidate config\n', state={'country': 'new'},
                      validate=lambda text: None, checkpoint=lambda: {'pid': 42}, confirm=self.confirm)
        params.update(kwargs)
        return self.route.execute(**params)

    def test_success_requires_ack_before_state_commit(self):
        def ack(cursor, text):
            self.assertEqual(tx.read_text(self.state), self.old_state)
            self.assertTrue(os.path.exists(self.route.journal))
            return self.confirm(cursor, text)
        state, result = self.run_route(confirm=ack)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(state['config_sha256'], tx.digest('candidate config\n'))
        self.assertEqual(json.loads(tx.read_text(self.state)), state)
        self.assertEqual(tx.read_text(result['backup']), 'original config\n')
        self.assertFalse(os.path.exists(self.route.journal))

    def test_invalid_config_or_external_edit_does_not_publish(self):
        def reject(text):
            raise ValueError('invalid configuration')
        with self.assertRaises(ValueError):
            self.run_route(validate=reject)
        tx.atomic(self.config, 'external config')
        with self.assertRaises(tx.RouteError):
            self.run_route()
        self.assertEqual(tx.read_text(self.config), 'external config')
        self.assertEqual(tx.read_text(self.state), self.old_state)

    def test_reload_rejection_restores_config_and_state(self):
        def reject_candidate(cursor, text):
            if text.startswith('candidate'):
                raise RuntimeError('reload rejected')
            return self.confirm(cursor, text)
        with self.assertRaises(tx.RouteError):
            self.run_route(confirm=reject_candidate)
        self.assertEqual(self.route.result()['status'], 'rolled_back')
        self.assertEqual(tx.read_text(self.config), 'original config\n')
        self.assertEqual(tx.read_text(self.state), self.old_state)
        self.assertEqual(self.acks, ['original config\n'])

    def test_postcheck_failure_restores_old_route(self):
        def fail():
            raise RuntimeError('exit lost connectivity')
        with self.assertRaises(tx.RouteError):
            self.run_route(verify=fail)
        self.assertEqual(self.acks, ['candidate config\n', 'original config\n'])
        self.assertEqual(tx.read_text(self.config), 'original config\n')
        self.assertEqual(tx.read_text(self.state), self.old_state)

    def test_state_write_failure_restores_old_route(self):
        original = tx.atomic
        def fail(path, text, mode=0o600):
            if path == self.state and '"new"' in text:
                raise OSError('injected disk error')
            return original(path, text, mode)
        with patch.object(tx, 'atomic', side_effect=fail), self.assertRaises(tx.RouteError):
            self.run_route()
        self.assertEqual(tx.read_text(self.config), 'original config\n')
        self.assertEqual(tx.read_text(self.state), self.old_state)

    def test_crash_after_publish_is_recovered_on_startup(self):
        def crash(cursor, text):
            raise SystemExit('simulated process death')
        with self.assertRaises(SystemExit):
            self.run_route(confirm=crash)
        self.assertEqual(tx.read_text(self.config), 'candidate config\n')
        result = self.route.recover(lambda: {}, self.confirm)
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(tx.read_text(self.config), 'original config\n')
        self.assertEqual(tx.read_text(self.state), self.old_state)
        self.assertFalse(os.path.exists(self.route.journal))

    def test_external_edit_during_reload_is_never_overwritten(self):
        def external(cursor, text):
            tx.atomic(self.config, 'external replacement')
            return {}
        with self.assertRaises(tx.RouteError):
            self.run_route(confirm=external)
        self.assertEqual(self.route.result()['status'], 'rollback_failed')
        self.assertEqual(tx.read_text(self.config), 'external replacement')
        self.assertTrue(os.path.exists(self.route.journal))
        with self.assertRaises(tx.RouteError):
            self.run_route()

    def test_rollback_ack_failure_stays_pending_and_can_be_recovered(self):
        def fail(cursor, text):
            raise RuntimeError('journal unavailable')
        with self.assertRaises(tx.RouteError):
            self.run_route(confirm=fail)
        self.assertEqual(self.route.result()['status'], 'rollback_failed')
        self.route.recover(lambda: {}, self.confirm)
        self.assertEqual(self.acks, ['original config\n'])
        self.assertEqual(self.route.result()['status'], 'rolled_back')

    def test_receipt_failure_does_not_undo_a_committed_route(self):
        original = self.route.save
        def fail(path, data):
            if path == self.route.receipt:
                raise OSError('receipt storage unavailable')
            return original(path, data)
        with patch.object(self.route, 'save', side_effect=fail):
            state, result = self.run_route()
        self.assertTrue(result['cleanup_pending'])
        self.assertEqual(self.route.result()['status'], 'applied')
        self.assertEqual(self.route.recover(lambda: {}, self.confirm)['status'], 'applied')
        self.assertEqual(tx.read_text(self.config), 'candidate config\n')
        self.assertFalse(os.path.exists(self.route.journal))

    def test_malformed_state_blocks_publication(self):
        tx.atomic(self.state, '{broken')
        with self.assertRaises(ValueError):
            self.run_route()
        self.assertEqual(tx.read_text(self.config), 'original config\n')

    def test_success_keeps_previous_failure_for_the_ui(self):
        self.route.fail('previous failed exit probe')
        self.run_route()
        self.assertEqual(self.route.result()['status'], 'applied')
        self.assertEqual(json.loads(tx.read_text(self.route.failure))['error'], 'previous failed exit probe')


@unittest.skipUnless(os.environ.get('SIDECAR_TEST_CHECK_BIN'), 'requires compiled Rust configuration checker')
class NativeValidationTests(unittest.TestCase):
    def test_real_parser_rejects_bad_config_without_binding_or_writing_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            config = os.path.join(directory, 'config.toml')
            stats = os.path.join(directory, 'absent', 'stats.jsonl')
            valid = 'listen="127.0.0.1:1"\nstats_path="%s"\n[[outbounds]]\ntag="direct"\nkind="direct"\n' % stats
            for text, code in ((valid, 0), (valid + '[[rules]]\ndomain="example.com"\noutbound="missing"\n', 1),
                               (valid + '[[broken]]\na=1\n', 1), (valid + '[[outbounds]]\ntag="direct"\nkind="direct"\n', 1)):
                with open(config, 'w') as output:
                    output.write(text)
                result = subprocess.run([os.environ['SIDECAR_TEST_CHECK_BIN'], '--check', '--config', config],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertFalse(os.path.exists(stats))


if __name__ == '__main__':
    unittest.main()
