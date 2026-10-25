import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy'))
from sidecar_nginx_auth import protect


class NginxAuthTests(unittest.TestCase):
    def auth_script_block(self, text):
        matches = re.findall(r'location\s*=\s*/sidecar-stats/auth\.js\s*\{([^{}]*)\}', text)
        self.assertEqual(len(matches), 1, 'expected one exact auth.js location')
        return matches[0]

    def config(self):
        return '''server {
    location /existing-tunnel { proxy_pass http://forwarding; }
    location /sidecar-stats/api/ { proxy_pass http://127.0.0.1:18082/api/; }
    location /sidecar-stats/ {
        auth_basic "Admin";
        auth_basic_user_file /etc/nginx/.htpasswd-admin;
        alias /var/www/dashboard/;
        index index.html;
    }
}'''

    def test_preserves_tunnel_and_basic_auth(self):
        result = protect(self.config())
        self.assertIn('location /existing-tunnel { proxy_pass http://forwarding; }', result)
        self.assertIn('auth_request /_sidecar_session_check;', result)
        self.assertIn('alias /var/www/dashboard/auth.js;', result)
        self.assertEqual(result, protect(result))

    def test_auth_script_has_explicit_javascript_type_without_mime_include(self):
        result = protect(self.config())
        body = self.auth_script_block(result)
        self.assertIn('default_type application/javascript;', body)
        self.assertIn('add_header X-Content-Type-Options nosniff always;', body)
        self.assertNotIn('include mime.types', result)
        self.assertEqual(protect(result), result)

    def test_existing_v1_guard_is_upgraded_once_with_javascript_type(self):
        protected = protect(self.config())
        body = self.auth_script_block(protected)
        legacy_body = re.sub(r'\s*default_type\s+application/javascript;', '', body)
        legacy = protected.replace(body, legacy_body, 1)
        self.assertNotIn('default_type application/javascript;', self.auth_script_block(legacy))
        upgraded = protect(legacy)
        self.assertIn('default_type application/javascript;', self.auth_script_block(upgraded))
        self.assertEqual(upgraded.count('# sidecar-session-guard-v1'), 1)
        self.assertEqual(upgraded.count('auth_request /_sidecar_session_check;'), 1)
        self.assertIn('location /existing-tunnel { proxy_pass http://forwarding; }', upgraded)
        self.assertEqual(protect(upgraded), upgraded)

    def test_fail_closed_on_ambiguous_or_incomplete_config(self):
        for text in (self.config() + self.config(), self.config().replace('auth_basic "Admin";', ''),
                     self.config().replace('alias /var/www/dashboard/;', ''), self.config()[:-8]):
            with self.assertRaises(ValueError):
                protect(text)

    def test_refuses_existing_error_or_session_policy(self):
        with self.assertRaises(ValueError):
            protect(self.config().replace('index index.html;', 'error_page 401 /login;'))
