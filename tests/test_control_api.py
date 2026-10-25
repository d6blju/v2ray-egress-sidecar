import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_streaming_and_exits import DEPLOY, DockerFixture, FixturePool, container
from sidecar_streaming_jobs import StreamingJobs
from sidecar_auth import AuthManager

spec = importlib.util.spec_from_file_location("control_api", os.path.join(DEPLOY, "v2ray-egress-admin-api.py"))
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


CONFIG = '''listen = "127.0.0.1:10880"
[[outbounds]]
tag = "direct"
kind = "direct"
[[outbounds]]
tag = "manual-nord"
kind = "http_connect"
addr = "127.0.0.1:18891"
[[rules]]
domain_suffix = ".ip.sb"
outbound = "manual-nord"
'''


class ControlAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = os.path.join(self.directory.name, "config.toml")
        with open(self.config, "w") as output:
            output.write(CONFIG)
        self.docker = DockerFixture([container("gluetun-bd", port=18891)])
        self.pool = FixturePool(self.docker, self.directory.name)
        self.auth = AuthManager(self.directory.name, secure=False, iterations=1000)
        self.auth.set_password('test', 'password')
        self.token = self.auth.create('test')
        self.patches = [
            patch.object(admin, "CONFIG_PATH", self.config),
            patch.object(admin, "STATE_DIR", self.directory.name),
            patch.object(admin, "STATE_PATH", os.path.join(self.directory.name, "state.json")),
            patch.object(admin, "EXIT_POOL", self.pool),
            patch.object(admin, 'AUTH', self.auth),
            patch.object(admin, "country_options", return_value=[{"name": "Bangladesh", "code": "BD"}]),
            patch.object(admin, "gluetun_status", return_value={"container": "gluetun-bd", "status": "running", "health": "healthy"}),
            patch.object(admin, "check_ip", return_value={"ok": True, "ip": "192.0.2.25"}),
            patch.object(admin, 'validate_route_config', return_value=None),
            patch.object(admin, 'reload_checkpoint', return_value={'pid': 42, 'cursor': 'test'}),
            patch.object(admin, 'confirm_reload', return_value={'confirmed': True, 'pid': 42}),
            patch.object(admin, 'verify_forwarding', return_value=None),
            patch.object(admin, 'verify_exit', return_value={'ok': True, 'ip': '192.0.2.25', 'country_code': 'BD'}),
            patch.object(admin.time, "sleep", return_value=None),
        ]
        for replacement in self.patches:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.jobs = StreamingJobs(self.pool, admin.protected_proxy_ports, self.directory.name,
                                  tester=lambda *args: {"playback_verified": False},
                                  matrix_tester=lambda *args: {"reports": [], "playback_verified": False})
        jobs_patch = patch.object(admin, "STREAMING_JOBS", self.jobs)
        jobs_patch.start()
        self.addCleanup(jobs_patch.stop)
        self.addCleanup(self.jobs.executor.shutdown, True)
        self.server = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, payload=None, authorized=True):
        headers = {}
        if authorized:
            headers['Cookie'] = 'sidecar_session=' + self.token
        if payload is not None:
            headers["Content-Type"] = "application/json"
            if authorized:
                headers['X-CSRF-Token'] = self.auth.csrf(self.token)
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, data=body, headers=headers)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_test_apply_clear_reapply_reuses_exit_and_keeps_manual_rules(self):
        status, queued = self.request("/api/streaming/test", {"profile": "netflix", "country": "Bangladesh", "protocol": "udp"})
        self.assertEqual(status, 202)
        self.jobs.executor.shutdown(wait=True)
        status, job = self.request("/api/streaming/" + queued["job"]["id"])
        self.assertEqual(job["job"]["status"], "done")
        with open(self.config) as source:
            self.assertEqual(source.read(), CONFIG)
        body = {"profile": "hbo-max", "country": "Bangladesh", "protocol": "udp", "include_ip_sb": True}
        status, applied = self.request("/api/apply", body)
        self.assertEqual(status, 200)
        self.assertTrue(applied["exit_reused"])
        self.assertEqual(applied["state"]["proxy"], "127.0.0.1:18891")
        with open(self.config) as source:
            self.assertIn('domain_suffix = ".discomax.com"', source.read())
        status, cleared = self.request("/api/clear", {})
        self.assertEqual(status, 200)
        self.assertFalse(cleared["state"]["active"])
        self.assertIsNone(cleared["state"]["country"])
        self.assertIsNone(cleared["state"]["proxy"])
        self.assertIsNone(cleared["state"]["container"])
        self.assertIsNone(cleared["state"]["gluetun"])
        with open(self.config) as source:
            config = source.read()
        self.assertNotIn("nord-ui", config)
        self.assertIn('tag = "manual-nord"', config)
        self.assertIn('outbound = "manual-nord"', config)
        self.assertEqual(admin.protected_proxy_ports(), {18891})
        status, reapplied = self.request("/api/apply", body)
        self.assertEqual(status, 200)
        self.assertTrue(reapplied["exit_reused"])
        self.assertEqual(reapplied["state"]["container"], "gluetun-bd")
        self.assertEqual(len(self.docker.items), 1)
        self.assertFalse(any(call[:2] == ["docker", "run"] for call in self.docker.calls))

    def test_missing_admin_header_and_invalid_title_cannot_create_exits(self):
        status, data = self.request("/api/streaming/test", {}, authorized=False)
        self.assertEqual(status, 401)
        status, data = self.request("/api/streaming/test", {"profile": "netflix", "country": "Bangladesh", "titles": ["https://127.0.0.1/private"]})
        self.assertEqual(status, 500)
        self.assertEqual(self.jobs.recent(), [])
        self.assertFalse(any(call[:2] == ["docker", "run"] for call in self.docker.calls))

    def test_matrix_endpoint_preserves_routes_and_reuses_exit(self):
        status, queued = self.request("/api/streaming/test", {"profile": "matrix", "country": "Bangladesh"})
        self.assertEqual(status, 202)
        self.jobs.executor.shutdown(wait=True)
        status, report = self.request("/api/streaming/" + queued["job"]["id"])
        self.assertEqual(report["job"]["status"], "done")
        self.assertIn("reports", report["job"]["result"])
        with open(self.config) as source:
            self.assertEqual(source.read(), CONFIG)
        self.assertFalse(any(call[:2] == ["docker", "run"] for call in self.docker.calls))

    def test_exit_probe_failure_keeps_manual_and_current_rules(self):
        body = {'profile': 'hbo-max', 'country': 'Bangladesh'}
        with patch.object(admin, 'verify_exit', side_effect=RuntimeError('probe failed')):
            status, data = self.request('/api/apply', body)
        self.assertEqual(status, 500)
        with open(self.config) as source:
            self.assertEqual(source.read(), CONFIG)
        status, data = self.request('/api/state')
        self.assertEqual(data['state']['last_transaction']['status'], 'failed')
        self.assertFalse(data['state']['transaction_pending'])

    def test_clear_postcheck_failure_restores_managed_route(self):
        status, data = self.request('/api/apply', {'profile': 'hbo-max', 'country': 'Bangladesh'})
        self.assertEqual(status, 200)
        with open(self.config) as source:
            applied = source.read()
        with patch.object(admin, 'verify_forwarding', side_effect=RuntimeError('forwarding failed')):
            status, data = self.request('/api/clear', {})
        self.assertEqual(status, 500)
        with open(self.config) as source:
            self.assertEqual(source.read(), applied)
        status, data = self.request('/api/state')
        self.assertTrue(data['state']['active'])
        self.assertTrue(data['state']['config_matches_state'])
        self.assertEqual(data['state']['last_transaction']['status'], 'rolled_back')

    def apply_service(self, profile, domains, country='Bangladesh'):
        status, data = self.request('/api/apply', {
            'profile': profile, 'country': country, 'protocol': 'udp',
            'domains': domains, 'include_ip_sb': False,
        })
        self.assertEqual(status, 200, data)
        return data

    def test_services_share_exit_and_edit_replaces_only_selected_domains(self):
        first = self.apply_service('binance', ['binance.com', 'binance.us'])
        second = self.apply_service('netflix', ['netflix.com'])
        original_binance = second['state']['routes'][0]
        original_netflix = second['state']['routes'][1]
        self.assertEqual(original_binance, first['state']['routes'][0])
        self.assertEqual(original_binance['proxy'], original_netflix['proxy'])
        with open(self.config) as source:
            config = source.read()
        self.assertEqual(config.count('tag = "nord-ui"'), 1)
        self.assertIn('domain_suffix = ".binance.us"', config)
        self.assertIn('domain_suffix = ".netflix.com"', config)
        edited = self.apply_service('binance', ['binance.com', 'binanceapi.com'])
        routes = {route['profile']: route for route in edited['state']['routes']}
        self.assertEqual(routes['netflix'], original_netflix)
        self.assertEqual(routes['binance']['domains'], ['binance.com', 'binanceapi.com'])
        with open(self.config) as source:
            config = source.read()
        self.assertNotIn('domain_suffix = ".binance.us"', config)
        self.assertIn('domain_suffix = ".binanceapi.com"', config)
        self.assertIn('outbound = "manual-nord"', config)
        self.assertFalse(any(call[:2] == ['docker', 'run'] for call in self.docker.calls))

    def test_edit_country_keeps_other_service_and_protects_both_exits(self):
        self.apply_service('binance', ['binance.com'])
        initial = self.apply_service('netflix', ['netflix.com'])
        other = initial['state']['routes'][1]
        self.docker.items.append(container('gluetun-hk', country='Hong Kong', port=18894))
        options = [{'name': 'Bangladesh', 'code': 'BD'}, {'name': 'Hong Kong', 'code': 'HK'}]
        with patch.object(admin, 'country_options', return_value=options):
            edited = self.apply_service('binance', ['binance.com'], country='Hong Kong')
        routes = {route['profile']: route for route in edited['state']['routes']}
        self.assertEqual(routes['netflix'], other)
        self.assertEqual(routes['binance']['country'], 'Hong Kong')
        self.assertEqual(routes['binance']['proxy'], '127.0.0.1:18894')
        self.assertEqual(admin.protected_proxy_ports(), {18891, 18894})
        with open(self.config) as source:
            config = source.read()
        self.assertEqual(config.count('kind = "http_connect"'), 3)
        self.assertIn('outbound = "manual-nord"', config)

    def test_conflicting_edit_cannot_steal_another_service_domain(self):
        self.apply_service('binance', ['binance.com'])
        before = self.apply_service('netflix', ['netflix.com'])
        self.docker.items.append(container('gluetun-hk', country='Hong Kong', port=18894))
        with open(self.config) as source:
            config_before = source.read()
        options = [{'name': 'Bangladesh', 'code': 'BD'}, {'name': 'Hong Kong', 'code': 'HK'}]
        with patch.object(admin, 'country_options', return_value=options):
            status, data = self.request('/api/apply', {
                'profile': 'netflix', 'country': 'Hong Kong',
                'domains': ['binance.com'], 'include_ip_sb': False,
            })
        self.assertEqual(status, 500)
        self.assertIn('conflict', data['details'])
        with open(self.config) as source:
            self.assertEqual(source.read(), config_before)
        status, latest = self.request('/api/state')
        self.assertEqual(latest['state']['routes'], before['state']['routes'])

    def test_same_exit_shared_domain_is_retained_by_each_service(self):
        first = self.apply_service('binance', ['shared.example', 'binance.com'])
        second = self.apply_service('netflix', ['shared.example', 'netflix.com'])
        self.assertEqual(second['state']['routes'][0], first['state']['routes'][0])
        with open(self.config) as source:
            self.assertEqual(source.read().count('domain_suffix = ".shared.example"'), 1)
        edited = self.apply_service('binance', ['binance.com'])
        self.assertEqual(edited['state']['routes'][0], second['state']['routes'][1])
        with open(self.config) as source:
            self.assertIn('domain_suffix = ".shared.example"', source.read())
        status, deleted = self.request('/api/routes/delete', {'profile': 'binance'})
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted['state']['routes'], [second['state']['routes'][1]])

    def test_delete_one_service_preserves_shared_exit_and_last_delete_cleans_ui_rules(self):
        self.apply_service('binance', ['binance.com'])
        initial = self.apply_service('netflix', ['netflix.com'])
        remaining = initial['state']['routes'][1]
        status, deleted = self.request('/api/routes/delete', {'profile': 'binance'})
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted['deleted_profile'], 'binance')
        self.assertEqual(deleted['state']['routes'], [remaining])
        self.assertTrue(deleted['state']['active'])
        with open(self.config) as source:
            config = source.read()
        self.assertNotIn('domain_suffix = ".binance.com"', config)
        self.assertIn('domain_suffix = ".netflix.com"', config)
        self.assertIn('tag = "nord-ui"', config)
        self.assertFalse(any(call[:2] in (['docker', 'rm'], ['docker', 'stop']) for call in self.docker.calls))
        status, deleted = self.request('/api/routes/delete', {'profile': 'netflix'})
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted['state']['routes'], [])
        self.assertFalse(deleted['state']['active'])
        self.assertIsNone(deleted['state']['profile'])
        self.assertIsNone(deleted['state']['proxy'])
        with open(self.config) as source:
            config = source.read()
        self.assertNotIn('nord-ui', config)
        self.assertIn('outbound = "manual-nord"', config)
        self.assertEqual(admin.protected_proxy_ports(), {18891})

    def test_delete_postcheck_failure_restores_all_services_and_state(self):
        self.apply_service('binance', ['binance.com'])
        before = self.apply_service('netflix', ['netflix.com'])
        with open(self.config) as source:
            config_before = source.read()
        with open(admin.STATE_PATH) as source:
            state_before = source.read()
        with patch.object(admin, 'verify_forwarding', side_effect=RuntimeError('forwarding failed')):
            status, data = self.request('/api/routes/delete', {'profile': 'binance'})
        self.assertEqual(status, 500)
        with open(self.config) as source:
            self.assertEqual(source.read(), config_before)
        with open(admin.STATE_PATH) as source:
            self.assertEqual(source.read(), state_before)
        status, latest = self.request('/api/state')
        self.assertEqual(latest['state']['routes'], before['state']['routes'])
        self.assertEqual(latest['state']['last_transaction']['status'], 'rolled_back')
        self.assertFalse(latest['state']['transaction_pending'])

    def test_delete_missing_service_is_404_and_missing_profile_is_400(self):
        before = self.apply_service('binance', ['binance.com'])
        status, data = self.request('/api/routes/delete', {'profile': 'netflix'})
        self.assertEqual(status, 404)
        status, data = self.request('/api/routes/delete', {})
        self.assertEqual(status, 400)
        status, latest = self.request('/api/state')
        self.assertEqual(latest['state']['routes'], before['state']['routes'])

    def test_delete_requires_login_and_csrf(self):
        self.apply_service('binance', ['binance.com'])
        status, data = self.request('/api/routes/delete', {'profile': 'binance'}, authorized=False)
        self.assertEqual(status, 401)
        request = Request(self.base + '/api/routes/delete', data=b'{"profile":"binance"}',
                          headers={'Cookie': 'sidecar_session=' + self.token, 'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        status, latest = self.request('/api/state')
        self.assertEqual([route['profile'] for route in latest['state']['routes']], ['binance'])

    def test_legacy_single_route_migrates_when_another_service_is_saved(self):
        applied = self.apply_service('binance', ['binance.com'])
        with open(admin.STATE_PATH) as source:
            legacy = json.load(source)
        legacy.pop('routes')
        with open(admin.STATE_PATH, 'w') as output:
            json.dump(legacy, output)
        status, viewed = self.request('/api/state')
        self.assertEqual(viewed['state']['routes'][0]['profile'], 'binance')
        updated = self.apply_service('netflix', ['netflix.com'])
        self.assertEqual([route['profile'] for route in updated['state']['routes']], ['binance', 'netflix'])
        self.assertEqual(updated['state']['routes'][0]['domains'], applied['state']['domains'])

    def test_explicit_empty_domain_list_is_not_refilled_from_preset(self):
        applied = self.apply_service('binance', ['binance.com'])
        status, data = self.request('/api/apply', {
            'profile': 'binance', 'country': 'Bangladesh', 'domains': [], 'include_ip_sb': False,
        })
        self.assertEqual(status, 500)
        self.assertIn('no domains', data['details'])
        status, latest = self.request('/api/state')
        self.assertEqual(latest['state']['routes'], applied['state']['routes'])

    def test_unchecking_ip_helper_removes_saved_helper_domains(self):
        status, applied = self.request('/api/apply', {
            'profile': 'binance', 'country': 'Bangladesh',
            'domains': ['binance.com'], 'include_ip_sb': True,
        })
        self.assertEqual(status, 200, applied)
        self.assertIn('ip.sb', applied['state']['domains'])
        self.assertIn('api.ip.sb', applied['state']['domains'])
        edited = self.apply_service('binance', applied['state']['domains'])
        self.assertEqual(edited['state']['domains'], ['binance.com'])
        self.assertFalse(edited['state']['include_ip_sb'])
        with open(self.config) as source:
            config = source.read()
        # The manually managed ip.sb rule survives, but its temporary UI copy does not.
        self.assertEqual(config.count('domain_suffix = ".ip.sb"'), 1)
        self.assertNotIn('domain_suffix = ".api.ip.sb"', config)
        self.assertIn('outbound = "manual-nord"', config)


class IPCheckTests(unittest.TestCase):
    def test_ipcheck_returns_country_city_and_uses_sidecar(self):
        with patch.object(admin, "run_cmd", return_value=json.dumps({"ip": "192.0.2.25", "country": "Bangladesh", "country_code": "BD", "city": "Dhaka"})) as command:
            result = admin.check_ip()
        self.assertEqual((result["country_code"], result["city"], result["country_source"]), ("BD", "Dhaka", "ip.sb"))
        args = command.call_args[0][0]
        self.assertEqual(args[args.index("--socks5-hostname") + 1], admin.SIDECAR_SOCKS)
        self.assertIn("--noproxy", args)

    def test_geo_failure_looks_up_measured_ip_not_fallback_request_ip(self):
        with patch.object(admin, "run_cmd", side_effect=[RuntimeError("geo failed"), "192.0.2.25\n", '{"success":true,"ip":"192.0.2.25","country_code":"TR"}']) as command:
            result = admin.check_ip()
        self.assertEqual((result["ip"], result["country_code"]), ("192.0.2.25", "TR"))
        self.assertEqual(command.call_args[0][0][-1], "https://ipwho.is/192.0.2.25")
        self.assertIn("measured ip.sb IP", result["country_source"])

    def test_invalid_geo_response_cannot_be_reported_as_an_ip(self):
        with patch.object(admin, "run_cmd", side_effect=['{"ip":"<html>"}', "<html>"]):
            result = admin.check_ip()
        self.assertFalse(result["ok"])

    def test_unknown_country_is_explicit_when_only_plain_ip_works(self):
        with patch.object(admin, "run_cmd", side_effect=["{}", "192.0.2.25\n", '{"success":false}']):
            result = admin.check_ip()
        self.assertEqual(result, {"ok": True, "ip": "192.0.2.25", "country_source": None})

    def test_fallback_ip_mismatch_cannot_change_measured_exit(self):
        with patch.object(admin, "run_cmd", side_effect=["{}", "192.0.2.25", '{"success":true,"ip":"192.0.2.99","country_code":"US"}']):
            result = admin.check_ip()
        self.assertEqual(result["ip"], "192.0.2.25")
        self.assertNotIn("country_code", result)


if __name__ == "__main__":
    unittest.main()
