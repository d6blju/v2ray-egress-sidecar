import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from http.cookies import SimpleCookie
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'deploy'))
sys.path.insert(0, os.path.dirname(__file__))
from sidecar_auth import AuthManager, AuthError
import test_control_api as fixtures


class AuthStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = AuthManager(self.tmp.name, secure=False, iterations=1000)
        self.auth.set_password('test', 'password')

    def test_modern_hash_has_no_plaintext_and_sessions_survive_restart(self):
        self.auth.authenticate('test', 'password', 'test-peer')
        with open(self.auth.users_path) as source:
            record = json.load(source)['test']
        self.assertEqual(record['algorithm'], 'pbkdf2_sha256')
        self.assertNotIn('password', record.values())
        token = self.auth.create('test')
        second = AuthManager(self.tmp.name, secure=False, iterations=1000)
        self.assertEqual(second.lookup(token)['username'], 'test')
        self.assertEqual(second.csrf(token), self.auth.csrf(token))
        with self.auth.db() as db:
            self.assertNotEqual(db.execute('SELECT token_hash FROM sessions').fetchone()[0], token)

    def test_password_change_and_logout_revoke_sessions(self):
        token = self.auth.create('test')
        self.auth.set_password('test', 'new-test-password')
        self.assertIsNone(self.auth.lookup(token))
        token = self.auth.create('test')
        self.auth.logout(token)
        self.assertIsNone(self.auth.lookup(token))

    def test_deleted_or_unknown_account_cannot_create_or_keep_session(self):
        with self.assertRaises(AuthError):
            self.auth.create('unknown')
        token = self.auth.create('test')
        os.unlink(self.auth.users_path)
        self.assertIsNone(self.auth.lookup(token))
        with self.assertRaises(AuthError):
            self.auth.create('test')

    def test_idle_and_absolute_expiry(self):
        token = self.auth.create('test')
        with patch('sidecar_auth.time.time', return_value=time.time() + 1801):
            self.assertIsNone(self.auth.lookup(token))
        token = self.auth.create('test')
        with self.auth.db() as db:
            db.execute('UPDATE sessions SET expires=?', (time.time() - 1,))
        self.assertIsNone(self.auth.lookup(token))

    def test_login_rotates_prelogin_session(self):
        old = self.auth.create()
        token = self.auth.create('test', old=old)
        self.assertNotEqual(old, token)
        self.assertNotEqual(self.auth.csrf(old), self.auth.csrf(token))
        self.assertIsNone(self.auth.lookup(old))

    def test_cookie_flags(self):
        secure = AuthManager(self.tmp.name, cookie_path='/example/')
        cookie = secure.cookie('sample')
        for value in ('HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/example/'):
            self.assertIn(value, cookie)
        self.assertNotIn('Domain=', cookie)

    def test_login_rate_limit_is_persistent(self):
        for _ in range(10):
            with self.assertRaises(AuthError):
                self.auth.authenticate('test', 'wrong', 'test-peer')
        second = AuthManager(self.tmp.name, secure=False, iterations=1000)
        with self.assertRaises(AuthError) as caught:
            second.authenticate('test', 'password', 'test-peer')
        self.assertEqual(caught.exception.status, 429)

    def test_legacy_migration_and_external_password_change(self):
        legacy = os.path.join(self.tmp.name, 'test.htpasswd')
        def save(password):
            with open(legacy, 'w') as output:
                output.write('legacy:{SHA}' + base64.b64encode(hashlib.sha1(password.encode()).digest()).decode() + '\n')
        save('password')
        auth = AuthManager(self.tmp.name, legacy, secure=False, iterations=1000)
        auth.authenticate('legacy', 'password', 'test-peer')
        token = auth.create('legacy')
        save('changed-test-password')
        self.assertIsNone(auth.lookup(token))
        with self.assertRaises(AuthError):
            auth.authenticate('legacy', 'password', 'test-peer')
        auth.authenticate('legacy', 'changed-test-password', 'test-peer')

    @unittest.skipUnless(shutil.which('openssl'), 'APR1 migration requires OpenSSL')
    def test_apr1_compatibility_without_password_in_command_arguments(self):
        raw = subprocess.check_output(['openssl', 'passwd', '-apr1', '-salt', 'testsalt', '-stdin'], input=b'password\n').decode().strip()
        self.assertTrue(self.auth.verify_legacy('password', raw))
        self.assertFalse(self.auth.verify_legacy('wrong', raw))


class AuthAPITests(unittest.TestCase):
    setUp = fixtures.ControlAPITests.setUp

    def call(self, path, payload=None, token=None, csrf=None, headers=None):
        h = dict(headers or {})
        if token:
            h['Cookie'] = 'sidecar_session=' + token
        if payload is not None:
            h.setdefault('Content-Type', 'application/json')
        if csrf:
            h['X-CSRF-Token'] = csrf
        request = Request(self.base + path, headers=h, data=json.dumps(payload).encode() if payload is not None else None)
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response), response.headers

    def test_anonymous_and_old_admin_header_cannot_access_routes(self):
        for path, body in (('/api/state', None), ('/api/ipcheck', None), ('/api/apply', {}), ('/api/clear', {}), ('/api/streaming/test', {})):
            status, data, _ = self.call(path, body, headers={'X-Sidecar-Admin': '1'})
            self.assertEqual(status, 401)
        status, _, _ = self.call('/api/health')
        self.assertEqual(status, 200)

    def test_valid_session_requires_csrf_json_and_same_origin(self):
        status, _, _ = self.call('/api/clear', {}, self.token)
        self.assertEqual(status, 403)
        status, _, _ = self.call('/api/clear', {}, self.token, 'invalid')
        self.assertEqual(status, 403)
        status, _, _ = self.call('/api/clear', {}, self.token, self.auth.csrf(self.token), {'Content-Type': 'text/plain'})
        self.assertEqual(status, 415)
        status, _, _ = self.call('/api/clear', {}, self.token, self.auth.csrf(self.token), {'Origin': 'https://attacker.example'})
        self.assertEqual(status, 403)
        status, _, _ = self.call('/api/state', token=self.token)
        self.assertEqual(status, 200)

    def test_native_login_rotation_csrf_and_logout(self):
        status, pre, headers = self.call('/api/session')
        jar = SimpleCookie(); jar.load(headers['Set-Cookie'])
        old = jar['sidecar_session'].value
        status, _, _ = self.call('/api/login', {'username': 'test', 'password': 'password'}, old)
        self.assertEqual(status, 403)
        status, logged, headers = self.call('/api/login', {'username': 'test', 'password': 'password'}, old, pre['csrf_token'])
        self.assertEqual(status, 200)
        jar = SimpleCookie(); jar.load(headers.get_all('Set-Cookie')[0])
        token = jar['sidecar_session'].value
        self.assertNotEqual(token, old)
        self.assertIsNone(self.auth.lookup(old))
        status, _, _ = self.call('/api/logout', {}, token, logged['csrf_token'])
        self.assertEqual(status, 200)
        self.assertIsNone(self.auth.lookup(token))
        status, _, _ = self.call('/api/state', token=token)
        self.assertEqual(status, 401)

    def test_existing_basic_credentials_bootstrap_and_logout_blocks_auto_login(self):
        basic = 'Basic ' + base64.b64encode(b'test:password').decode()
        status, logged, headers = self.call('/api/session', headers={'Authorization': basic, 'X-Sidecar-Session': '1'})
        self.assertEqual(status, 200)
        self.assertTrue(logged['authenticated'])
        status, logged, _ = self.call('/api/session', headers={'Authorization': basic, 'X-Sidecar-Session': '1', 'Cookie': 'sidecar_logged_out=1'})
        self.assertFalse(logged['authenticated'])
        status, _, _ = self.call('/api/session', headers={'Authorization': basic, 'X-Sidecar-Session': '1', 'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(status, 403)


if __name__ == '__main__':
    unittest.main()
