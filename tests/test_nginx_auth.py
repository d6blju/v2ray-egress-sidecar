import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy'))
from sidecar_nginx_auth import protect


class NginxAuthTests(unittest.TestCase):
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

    def test_fail_closed_on_ambiguous_or_incomplete_config(self):
        for text in (self.config() + self.config(), self.config().replace('auth_basic "Admin";', ''),
                     self.config().replace('alias /var/www/dashboard/;', ''), self.config()[:-8]):
            with self.assertRaises(ValueError):
                protect(text)

    def test_refuses_existing_error_or_session_policy(self):
        with self.assertRaises(ValueError):
            protect(self.config().replace('index index.html;', 'error_page 401 /login;'))
