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
        self.patches = [
            patch.object(admin, "CONFIG_PATH", self.config),
            patch.object(admin, "STATE_DIR", self.directory.name),
            patch.object(admin, "STATE_PATH", os.path.join(self.directory.name, "state.json")),
            patch.object(admin, "EXIT_POOL", self.pool),
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
        if payload is not None:
            headers["Content-Type"] = "application/json"
            if authorized:
                headers["X-Sidecar-Admin"] = "1"
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
        self.assertEqual(status, 403)
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
