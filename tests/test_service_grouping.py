import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace


DEPLOY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy")
sys.path.insert(0, DEPLOY_DIR)
from sidecar_services import PROFILES, SERVICES, service_name


def load_script(filename, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(DEPLOY_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashboard = load_script("v2ray-egress-dashboard-render.py", "dashboard")
cli = load_script("v2ray-sidecar-stats.py", "stats_cli")
admin = load_script("v2ray-egress-admin-api.py", "admin_api")


class ServiceGroupingTests(unittest.TestCase):
    def test_github_domains_and_subdomains(self):
        for host in ["github.com", "api.github.com", "raw.githubusercontent.com",
                     "githubusercontent.com", "githubassets.com", "avatars.githubusercontent.com",
                     "User.GitHub.IO.", "ghcr.io", "github.dev", "api.githubcopilot.com"]:
            with self.subTest(host=host):
                self.assertEqual(service_name(host), "GitHub")

    def test_common_apps(self):
        cases = {
            "pbs.twimg.com": "X / Twitter",
            "ab.chatgpt.com": "OpenAI / ChatGPT",
            "claude.ai": "Claude / Anthropic",
            "r1.googlevideo.com": "YouTube",
            "assets.nflximg.net": "Netflix",
            "telegram.me": "Telegram",
            "cdn.discordapp.com": "Discord",
            "v.tiktokcdn.com": "TikTok",
            "static.cdninstagram.com": "Meta / Instagram / WhatsApp",
            "i.redd.it": "Reddit",
            "i.scdn.co": "Spotify",
            "cdn.steamstatic.com": "Steam",
            "api.ip.sb": "IP Check",
            "static.mzstatic.com": "Apple",
            "courier.push.me.com": "Apple",
            "login.microsoftonline.com": "Microsoft",
            "config.edge.skype.com": "Microsoft",
            "downloads.nordvpn.com": "NordVPN",
            "cloudflare-dns.com": "Cloudflare",
            "a.slack-edge.com": "Slack",
            "us02web.zoom.us": "Zoom",
            "dl.dropboxusercontent.com": "Dropbox",
            "registry-1.docker.io": "Docker",
        }
        for host, expected in cases.items():
            with self.subTest(host=host):
                self.assertEqual(service_name(host), expected)

    def test_specific_apps_win_over_shared_google_domains(self):
        cases = {
            "play.google.com": "Google Play",
            "play.googleapis.com": "Google Play",
            "play-lh.googleusercontent.com": "Google Play",
            "youtubei.googleapis.com": "YouTube",
            "yt3.ggpht.com": "YouTube",
            "storage.googleapis.com": "Google",
            "edgedl.me.gvt1.com": "Google",
            "gvt2.com": "Google",
            "lh3.googleusercontent.com": "Google",
            "www.withgoogle.com": "Google",
            "ad.doubleclick-cn.net": "Google",
        }
        for host, expected in cases.items():
            with self.subTest(host=host):
                self.assertEqual(service_name(host), expected)

    def test_suffix_matching_respects_domain_boundaries(self):
        self.assertEqual(service_name("notgithub.com"), "notgithub.com")
        self.assertEqual(service_name("github.com.example.org"), "example.org")
        self.assertEqual(service_name("mygithubusercontent.com"), "mygithubusercontent.com")
        self.assertEqual(service_name("github.com.evil.co.uk"), "evil.co.uk")
        self.assertEqual(service_name("storage.example.net"), "example.net")

    def test_unknown_ip_hosts_stay_separate(self):
        for host in ["192.0.2.1", "198.51.100.1", "2001:db8::1", "[2001:db8::2]"]:
            with self.subTest(host=host):
                self.assertEqual(service_name(host), host)
        self.assertEqual(service_name(""), "(unknown)")
        self.assertEqual(service_name(None), "(unknown)")

    def test_routing_profiles_use_the_same_registry(self):
        self.assertEqual(len(set(p["id"] for p in PROFILES)), len(PROFILES))
        self.assertEqual(admin.PROFILES, PROFILES)
        for service in SERVICES:
            self.assertEqual(admin.profile_domains(service["id"]), service["domains"])
            for domain in service.get("stats_domains", service["domains"]):
                with self.subTest(domain=domain):
                    self.assertEqual(service_name(domain), service["name"])

    def test_aggregation_merges_hosts_without_losing_counters(self):
        timestamp = int(time.time() * 1000)
        rows = [
            {"target_host": host, "outbound": outbound, "network": network,
             "uplink_bytes": up, "downlink_bytes": down, "elapsed_ms": elapsed,
             "ended_at_unix_ms": timestamp, "error": error}
            for host, outbound, network, up, down, elapsed, error in [
                ("github.com", "direct", "tcp", 100, 900, 1000, None),
                ("raw.githubusercontent.com", "direct", "tcp", 200, 1800, 3000, None),
                ("githubassets.com", "direct", "udp", 300, 2700, 5000, "failed"),
                ("github.io", "nord-ui", "tcp", 400, 3600, 7000, None),
            ]
        ]
        summary = dashboard.summarize(rows, 3600000, "1 hour")
        grouped = {row["outbound"]: row for row in summary["services"]}
        self.assertEqual(len(summary["services"]), 2)
        self.assertTrue(all(row["name"] == "GitHub" for row in summary["services"]))
        direct = grouped["direct"]
        self.assertEqual((direct["conns"], direct["ok"], direct["errors"]), (3, 2, 1))
        self.assertEqual((direct["uplink"], direct["downlink"], direct["total"]), (600, 5400, 6000))
        self.assertEqual(direct["avg_ms"], 3000)
        self.assertEqual(grouped["nord-ui"]["total"], 4000)
        self.assertEqual(sum(r["total"] for r in summary["services"]), summary["totals"]["total"])
        self.assertEqual(len(summary["domains"]), 4)
        for row in rows:
            self.assertEqual(cli.key_for_mode("service", row), ("GitHub", row["outbound"]))

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stats.jsonl")
            with open(path, "w", encoding="utf-8") as fixture:
                for row in rows:
                    fixture.write(json.dumps(row) + "\n")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli.aggregate(SimpleNamespace(file=path, since=None, mode="service", sort="bytes", top=0))
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(all(line.startswith("GitHub") for line in lines[1:]))


if __name__ == "__main__":
    unittest.main()
