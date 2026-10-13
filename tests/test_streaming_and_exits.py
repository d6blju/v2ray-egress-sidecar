import contextlib
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


DEPLOY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy")
sys.path.insert(0, DEPLOY)
from sidecar_exits import ExitPool, exit_metadata
from sidecar_streaming import MATRIX_PROFILES, inspect_page, inspect_bbc_region, test_service as probe_service, test_matrix as probe_matrix, title_ids
from sidecar_streaming_jobs import StreamingJobs


def container(name="gluetun-ui", country="Bangladesh", port=18892, status="running"):
    bindings = {"8888/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}
    return {"Name": "/" + name, "Id": name + "-id", "Created": "2026-01-01T00:00:00Z",
            "Config": {"Env": ["VPN_SERVICE_PROVIDER=nordvpn", "OPENVPN_PROTOCOL=udp", "SERVER_COUNTRIES=" + country,
                               "OPENVPN_PASSWORD=fixture-secret"], "Labels": {"v2ray-egress-sidecar.managed": "1"}},
            "HostConfig": {"PortBindings": bindings},
            "NetworkSettings": {"Ports": bindings if status == "running" else {}},
            "State": {"Status": status, "Health": {"Status": "healthy"}}}


class DockerFixture:
    def __init__(self, items):
        self.items = copy.deepcopy(items)
        self.calls = []
        self.connections = ""
        self.ss_error = False

    def __call__(self, args, timeout=15):
        self.calls.append(list(args))
        if args[:2] == ["docker", "ps"]:
            return "\n".join(item["Id"] for item in self.items)
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([item for item in self.items if item["Id"] in args[2:]])
        if args[:3] == ["docker", "image", "inspect"]:
            return "{}"
        if args[:2] == ["docker", "start"]:
            for item in self.items:
                if item["Name"] == "/" + args[2]:
                    item["State"]["Status"] = "running"
            return args[2]
        if args[:2] == ["docker", "rm"]:
            self.items = [item for item in self.items if item["Name"] != "/" + args[-1]]
            return args[-1]
        if args[:2] == ["docker", "run"]:
            name = args[args.index("--name") + 1]
            port = int(args[args.index("-p") + 1].split(":")[1])
            country = next(value.split("=", 1)[1] for value in args if value.startswith("SERVER_COUNTRIES="))
            time.sleep(0.02)
            self.items.append(container(name, country, port))
            return name + "-id"
        if args[0] == "ss":
            if self.ss_error:
                raise RuntimeError("ss unavailable")
            return self.connections
        raise AssertionError("unexpected command: " + repr(args))


class FixturePool(ExitPool):
    def __init__(self, docker, directory, shared_lock=None):
        super().__init__(runner=docker, state_dir=directory)
        self.mutex = shared_lock or threading.RLock()

    @contextlib.contextmanager
    def locked(self):
        with self.mutex:
            yield

    def _available_memory(self):
        return 512000

    def _free_port(self, preferred, exits):
        used = {item["port"] for item in exits}
        return preferred if preferred not in used else 18893


class ExitReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def pool(self, items):
        docker = DockerFixture(items)
        return FixturePool(docker, self.temporary.name), docker

    def test_metadata_excludes_credentials(self):
        info = exit_metadata(container())
        self.assertNotIn("fixture-secret", json.dumps(info))
        self.assertNotIn("Env", info)

    def test_same_country_uses_existing_port_and_container(self):
        pool, docker = self.pool([container("gluetun-bd", port=18891)])
        first = pool.ensure("Bangladesh", preferred_port=18892)
        second = pool.ensure("Bangladesh", preferred_port=18894)
        self.assertEqual((first["container"], first["port"]), ("gluetun-bd", 18891))
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(first["reused"])
        self.assertFalse(any(call[:2] in (["docker", "run"], ["docker", "rm"]) for call in docker.calls))

    def test_stopped_same_country_is_started_without_recreation(self):
        pool, docker = self.pool([container(status="exited")])
        result = pool.ensure("Bangladesh")
        self.assertTrue(result["reused"])
        self.assertIn(["docker", "start", "gluetun-ui"], docker.calls)
        self.assertFalse(any(call[:2] == ["docker", "run"] for call in docker.calls))

    def test_protocol_mismatch_does_not_create_a_duplicate(self):
        pool, docker = self.pool([container()])
        with self.assertRaises(ValueError):
            pool.ensure("Bangladesh", "tcp")
        self.assertEqual(len(docker.items), 1)

    def test_unhealthy_existing_exit_is_not_replaced(self):
        item = container()
        item["State"]["Health"]["Status"] = "unhealthy"
        pool, docker = self.pool([item])
        with self.assertRaises(RuntimeError):
            pool.ensure("Bangladesh")
        self.assertFalse(any(call[:2] in (["docker", "run"], ["docker", "rm"]) for call in docker.calls))

    def test_prunes_only_unreferenced_disconnected_duplicate(self):
        pool, docker = self.pool([container("gluetun-bd", port=18891), container()])
        self.assertEqual(pool.prune_duplicates({18891}), ["gluetun-ui"])
        self.assertEqual(pool.prune_duplicates(set()), [])
        self.assertEqual(len(docker.items), 1)

    def test_active_duplicate_and_unknown_connection_state_are_protected(self):
        for unavailable in (False, True):
            pool, docker = self.pool([container("gluetun-bd", port=18891), container()])
            docker.connections = "0 0 127.0.0.1:18892 127.0.0.1:42123"
            docker.ss_error = unavailable
            self.assertEqual(pool.prune_duplicates({18891}), [])

    def test_missing_credentials_does_not_delete_existing_exits(self):
        pool, docker = self.pool([container()])
        with patch.dict(os.environ, {"SIDECAR_NORDVPN_ENV": os.path.join(self.temporary.name, "missing.env")}):
            with self.assertRaises(RuntimeError):
                pool.ensure("Japan", protected_ports=set())
        self.assertEqual(len(docker.items), 1)
        self.assertFalse(any(call[:2] == ["docker", "rm"] for call in docker.calls))

    def test_two_concurrent_country_requests_create_only_one_exit(self):
        docker = DockerFixture([])
        lock = threading.RLock()
        pools = [FixturePool(docker, self.temporary.name, lock) for _ in range(2)]
        credentials = os.path.join(self.temporary.name, "fixture.env")
        with open(credentials, "w") as output:
            output.write("OPENVPN_USER=fixture\nOPENVPN_PASSWORD=fixture\n")
        with patch.dict(os.environ, {"SIDECAR_NORDVPN_ENV": credentials}):
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(lambda pool: pool.ensure("Japan"), pools))
        self.assertEqual(sum(call[:2] == ["docker", "run"] for call in docker.calls), 1)
        self.assertEqual(results[0]["id"], results[1]["id"])

    def test_capacity_cannot_evict_configured_exits(self):
        pool, docker = self.pool([container()])
        pool.max_countries = 1
        credentials = os.path.join(self.temporary.name, "fixture.env")
        with open(credentials, "w") as output:
            output.write("OPENVPN_USER=fixture\nOPENVPN_PASSWORD=fixture\n")
        with patch.dict(os.environ, {"SIDECAR_NORDVPN_ENV": credentials}):
            with self.assertRaises(RuntimeError):
                pool.ensure("Japan", protected_ports={18892})
        self.assertEqual(len(docker.items), 1)


def response(body, status=200, url="https://www.netflix.com/", headers="", code=0):
    return {"body": body, "headers": headers, "http_status": status, "url": url,
            "seconds": 0.1, "curl_exit": code, "error": ""}


class StreamingEvidenceTests(unittest.TestCase):
    def test_matrix_has_social_and_ai_services_on_the_same_proxy(self):
        calls = []
        def requester(proxy, url):
            calls.append((proxy, url))
            if "geoip" in url:
                return response('{"ip":"192.0.2.25","country_code":"BD"}', url=url)
            return response("<title>Service</title>", url=url)
        result = probe_matrix("Bangladesh", {"proxy": "127.0.0.1:18891", "reused": True}, ["70143836"], requester)
        self.assertEqual([report["profile"] for report in result["reports"]], MATRIX_PROFILES)
        self.assertIn("openai-chatgpt", MATRIX_PROFILES)
        self.assertIn("tiktok", MATRIX_PROFILES)
        self.assertEqual({proxy for proxy, _ in calls}, {"http://127.0.0.1:18891"})
        self.assertTrue(all(report["geoip"]["country_code"] == "BD" for report in result["reports"]))
        self.assertTrue(all(not report["playback_verified"] for report in result["reports"]))

    def test_200_region_error_is_not_success(self):
        item = inspect_page("disney-plus", response("<title>Unavailable In Your Region</title>", url="https://www.disneyplus.com/welcome/unavailable"))
        self.assertEqual(item["status"], "region_unavailable")
        self.assertFalse(item["playback_verified"])

    def test_403_and_browser_challenges_are_not_geo_claims(self):
        item = inspect_page("peacock", response("<title>403 Forbidden</title>", status=403))
        self.assertEqual(item["status"], "http_refused")
        item = inspect_page("crunchyroll", response("<title>Just a moment...</title><script>cf-chl-x</script>", status=403))
        self.assertEqual(item["status"], "challenge")
        self.assertIsNone(item["region"])

    def test_marketing_homepage_is_not_playback(self):
        item = inspect_page("hbo-max", response("<title>HBO Max</title>", headers="Set-Cookie: countryCode=BD; Path=/"))
        self.assertEqual(item["region"], "BD")
        self.assertEqual(item["status"], "homepage_accessible")
        self.assertFalse(item["playback_verified"])

    def test_netflix_structured_geo_and_public_title_are_separate(self):
        body = '<title>Netflix</title><script>netflix.reactContext = {"models":{"geo":{"data":{"id":"BD","countryName":"Bangladesh"}}}};</script>'
        body += '<script type="application/ld+json">{"@type":"TVSeries","name":"Example &amp; Series"}</script>'
        item = inspect_page("netflix", response(body), "70143836")
        self.assertEqual(item["region"], "BD")
        self.assertEqual(item["status"], "title_visible")
        self.assertFalse(item["playback_verified"])

    def test_arbitrary_urls_and_too_many_titles_are_rejected(self):
        for value in ["http://www.netflix.com/title/70143836", "https://netflix.com.evil.test/title/70143836", "https://127.0.0.1/title/70143836", "file:///etc/passwd", "https://www.netflix.com/browse"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                title_ids([value])
        with self.assertRaises(ValueError):
            title_ids(["80000001", "80000002", "80000003", "80000004", "80000005"])
        self.assertEqual(title_ids(["https://www.netflix.com/bd/title/70143836", "70143836"]), ["70143836"])

    def test_current_netflix_html_h1_and_localized_redirect(self):
        item = inspect_page("netflix", response('<title>Netflix</title><h1>Breaking <span>Bad</span></h1>', url='https://www.netflix.com/tr-en/title/70143836'), '70143836')
        self.assertEqual((item["title"], item["status"], item["region"]), ("Breaking Bad", "title_visible", "TR"))
        self.assertFalse(item["playback_verified"])

    def test_netflix_country_menu_is_not_request_geolocation(self):
        item = inspect_page("netflix", response('<title>Netflix</title><script type="application/json">{"phoneCountries":[{"id":"US","countryName":"United States"}]}</script>'))
        self.assertIsNone(item["region"])

    def test_bbc_public_media_geolocation_error_is_not_homepage_access(self):
        item = inspect_bbc_region(response('{"result":"geolocation"}'))
        self.assertEqual(item["status"], "region_unavailable")
        item = inspect_bbc_region(response('{"media":[{"connection":[{"href":"https://example.test/live.m3u8"}]}]}'))
        self.assertEqual(item["status"], "manifest_available")
        self.assertFalse(item["playback_verified"])

    def test_all_requests_use_selected_proxy_and_ip_fallback_is_explicit(self):
        calls = []
        def requester(proxy, url):
            calls.append((proxy, url))
            if "geoip" in url:
                return response("{}", status=403, url=url)
            if "cdn-cgi/trace" in url:
                return response("ip=192.0.2.25\nloc=BD\n", url=url)
            return response("<title>Netflix</title>", url=url)
        result = probe_service("netflix", "Bangladesh", {"proxy": "127.0.0.1:18891", "reused": True}, ["70143836"], requester)
        self.assertEqual(result["geoip"]["country_code"], "BD")
        self.assertEqual(result["geoip"]["source"], "Cloudflare trace")
        self.assertEqual({proxy for proxy, _ in calls}, {"http://127.0.0.1:18891"})
        self.assertFalse(result["playback_verified"])


class StreamingQueueTests(unittest.TestCase):
    def test_exit_creation_waits_for_route_mutation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            entered = threading.Event()
            class Pool:
                def ensure(self, *args):
                    entered.set()
                    return {"proxy": "127.0.0.1:18891", "reused": True}
            mutation = threading.Lock()
            mutation.acquire()
            jobs = StreamingJobs(Pool(), lambda: {18891}, directory,
                                 tester=lambda *args: {}, mutation_lock=mutation)
            try:
                job = jobs.submit("netflix", "Bangladesh", "udp", [], 18892)
                self.assertFalse(entered.wait(0.1))
                mutation.release()
                self.assertTrue(entered.wait(2))
                jobs.executor.shutdown(wait=True)
                self.assertEqual(jobs.get(job["id"])["status"], "done")
            finally:
                if mutation.locked():
                    mutation.release()
                jobs.executor.shutdown(wait=True)

    def test_matrix_ensures_country_exit_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            class Pool:
                count = 0
                def ensure(self, *args):
                    self.count += 1
                    return {"proxy": "127.0.0.1:18891", "reused": True}
            calls = []
            def matrix_tester(country, info, titles):
                calls.append((country, info, titles))
                return {"reports": [], "playback_verified": False}
            pool = Pool()
            jobs = StreamingJobs(pool, lambda: {18891}, directory,
                                 tester=lambda *args: self.fail("single tester used for matrix"), matrix_tester=matrix_tester)
            try:
                job = jobs.submit("matrix", "Bangladesh", "udp", ["70143836"], 18892)
                jobs.executor.shutdown(wait=True)
                self.assertEqual(pool.count, 1)
                self.assertEqual(calls[0][1]["proxy"], "127.0.0.1:18891")
                self.assertEqual(jobs.get(job["id"])["status"], "done")
            finally:
                jobs.executor.shutdown(wait=True)

    def test_repeated_clicks_share_one_job_and_one_country_ensure(self):
        with tempfile.TemporaryDirectory() as directory:
            entered, release = threading.Event(), threading.Event()
            class Pool:
                count = 0
                def ensure(self, *args):
                    self.count += 1
                    entered.set()
                    release.wait(3)
                    return {"proxy": "127.0.0.1:18892", "reused": True}
            pool = Pool()
            jobs = StreamingJobs(pool, lambda: {18892}, directory, tester=lambda *args: {"playback_verified": False})
            try:
                first = jobs.submit("netflix", "Bangladesh", "udp", [], 18892)
                self.assertTrue(entered.wait(2))
                second = jobs.submit("netflix", "Bangladesh", "udp", [], 18892)
                self.assertEqual(first["id"], second["id"])
                self.assertEqual(jobs.active_countries(), {"bangladesh"})
                release.set()
                jobs.executor.shutdown(wait=True)
                self.assertEqual(pool.count, 1)
                self.assertEqual(jobs.get(first["id"])["status"], "done")
            finally:
                release.set()
                jobs.executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
