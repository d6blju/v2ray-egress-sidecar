"""Private-port Nginx integration; never loads or reloads the system config."""
import base64
import http.cookiejar
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor, HTTPRedirectHandler

sys.path.insert(0, os.path.dirname(__file__))
import test_control_api as fixtures
from sidecar_nginx_auth import protect


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


@unittest.skipUnless(shutil.which('nginx') and shutil.which('openssl'), 'requires Nginx and OpenSSL')
class NginxSessionTests(unittest.TestCase):
    setUp = fixtures.ControlAPITests.setUp

    def test_pages_data_logout_and_explicit_login(self):
        directory = self.directory.name
        web = os.path.join(directory, 'web')
        os.mkdir(web)
        for name in ('index.html', 'login.html', 'auth.js', 'stats.json'):
            with open(os.path.join(web, name), 'w') as output:
                output.write('static-test')
        password_file = os.path.join(directory, 'test.htpasswd')
        hashed = subprocess.check_output(['openssl', 'passwd', '-apr1', '-salt', 'testsalt', '-stdin'], input=b'password\n').decode().strip()
        with open(password_file, 'w') as output:
            output.write('test:' + hashed + '\n')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        config = '''user root;
pid %(dir)s/nginx.pid;
error_log %(dir)s/error.log;
events { worker_connections 64; }
http { access_log off;
server { listen 127.0.0.1:%(port)d;
location /sidecar-stats/api/ {
    auth_basic "Test"; auth_basic_user_file %(password)s;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_pass %(api)s/api/;
}
location /sidecar-stats/ {
    auth_basic "Test";
    auth_basic_user_file %(password)s;
    alias %(web)s/;
    index index.html;
}
}}
''' % {'dir': directory, 'port': port, 'password': password_file, 'api': self.base, 'web': web}
        config = protect(config).replace('http://127.0.0.1:18082/api/auth/check', self.base + '/api/auth/check')
        path = os.path.join(directory, 'nginx.conf')
        with open(path, 'w') as output:
            output.write(config)
        process = subprocess.Popen(['nginx', '-p', directory + '/', '-c', path, '-g', 'daemon off;'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def stop():
            if process.poll() is None:
                process.send_signal(signal.SIGQUIT)
                process.wait(timeout=10)
            stderr = process.stderr.read().decode()
            process.stderr.close()
            self.assertEqual(process.returncode, 0, stderr)
        self.addCleanup(stop)
        base = 'http://127.0.0.1:%d/sidecar-stats/' % port
        jar = http.cookiejar.CookieJar()
        client = build_opener(HTTPCookieProcessor(jar), NoRedirect())
        basic = 'Basic ' + base64.b64encode(b'test:password').decode()
        def call(path, data=None, csrf=None, basic_auth=True):
            headers = {'Authorization': basic} if basic_auth else {}
            if csrf:
                headers['X-CSRF-Token'] = csrf
            if data is not None:
                headers['Content-Type'] = 'application/json'
            request = Request(base + path, headers=headers, data=json.dumps(data).encode() if data is not None else None)
            try:
                response = client.open(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return response.status, response.headers, response.read()
        for _ in range(40):
            try:
                status, headers, _ = call('login.html')
                break
            except OSError:
                threading.Event().wait(0.05)
        else:
            self.fail('isolated nginx failed to start')
        self.assertEqual(status, 200)
        self.assertEqual(call('stats.json', basic_auth=False)[0], 401)
        self.assertEqual(call('stats.json')[0], 200)
        self.assertTrue(any(cookie.name == 'sidecar_session' for cookie in jar))
        status, _, body = call('api/session')
        session = json.loads(body.decode())
        self.assertTrue(session['authenticated'])
        self.assertEqual(call('api/logout', {}, 'invalid')[0], 403)
        self.assertEqual(call('api/logout', {}, session['csrf_token'])[0], 200)
        self.assertEqual(call('stats.json')[0], 302)
        self.assertEqual(call('auth.js')[0], 200)
        _, _, body = call('api/session')
        prelogin = json.loads(body.decode())
        self.assertFalse(prelogin['authenticated'])
        self.assertEqual(call('api/login', {'username': 'test', 'password': 'password'}, prelogin['csrf_token'])[0], 200)
        self.assertEqual(call('stats.json')[0], 200)
