import argparse
import html
import json
import os
import re
import subprocess
import tempfile
import time
from html.parser import HTMLParser
from urllib.parse import urlparse

from sidecar_services import SERVICES


REFERENCE = "https://github.com/lmc999/RegionRestrictionCheck"
STREAMING = {item["id"]: item for item in SERVICES if item.get("streaming_url") or item.get("probe_url")}
MATRIX_PROFILES = ["tiktok", "disney-plus", "netflix", "youtube", "prime-video", "reddit", "openai-chatgpt", "hbo-max", "claude-anthropic"]
DEFAULT_NETFLIX_TITLES = ["80057281", "81280792", "70143836"]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class Page(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.text = []
        self.scripts = []
        self.in_title = False
        self.heading = None
        self.headings = []
        self.hidden = 0
        self.script = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag == "h1":
            self.heading = []
        if tag in ("script", "style"):
            self.hidden += 1
        if tag == "script":
            self.script = []

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "h1" and self.heading is not None:
            self.headings.append(" ".join("".join(self.heading).split()))
            self.heading = None
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None

    def handle_data(self, value):
        if self.in_title:
            self.title.append(value)
        if self.heading is not None:
            self.heading.append(value)
        if self.script is not None:
            self.script.append(value)
        if not self.hidden:
            self.text.append(value)


def title_ids(values):
    result = []
    for value in values or []:
        for token in re.split(r"[\s,;]+", str(value).strip()):
            if not token:
                continue
            if not re.fullmatch(r"[0-9]{6,10}", token):
                parsed = urlparse(token)
                if parsed.scheme != "https" or parsed.hostname not in ("netflix.com", "www.netflix.com"):
                    raise ValueError("enter a Netflix title ID or an https://www.netflix.com/title/... link")
                match = re.fullmatch(r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:title|watch)/([0-9]{6,10})/?", parsed.path)
                if not match:
                    raise ValueError("invalid Netflix title link")
                token = match.group(1)
            if token not in result:
                result.append(token)
    if len(result) > 4:
        raise ValueError("at most four Netflix titles per test")
    return result


def fetch(proxy, url, timeout=12):
    parsed = urlparse(proxy)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password or not parsed.port:
        raise ValueError("use a localhost Gluetun HTTP proxy")
    with tempfile.TemporaryDirectory() as directory:
        body_path = os.path.join(directory, "body")
        header_path = os.path.join(directory, "headers")
        args = [
            "curl", "--silent", "--show-error", "--ipv4", "--location", "--max-redirs", "4",
            "--compressed", "--connect-timeout", "5", "--max-time", str(timeout),
            "--limit-rate", "128k", "--max-filesize", "1048576", "--proxy", proxy,
            "--noproxy", "", "--user-agent", USER_AGENT, "--header", "Accept-Language: en-US,en;q=0.9",
            "--dump-header", header_path, "--output", body_path,
            "--write-out", "%{http_code}\n%{url_effective}\n%{time_total}", url,
        ]
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=timeout + 5)
        fields = result.stdout.splitlines()
        with open(body_path, "rb") if os.path.exists(body_path) else open(os.devnull, "rb") as body:
            content = body.read(1048576).decode("utf-8", "replace")
        with open(header_path, "rb") if os.path.exists(header_path) else open(os.devnull, "rb") as headers:
            header = headers.read(65536).decode("utf-8", "replace")
        return {
            "http_status": int(fields[0]) if fields and fields[0].isdigit() else 0,
            "url": fields[1] if len(fields) > 1 else url,
            "seconds": float(fields[2]) if len(fields) > 2 else 0,
            "curl_exit": result.returncode, "body": content, "headers": header,
            "error": result.stderr[:200],
        }


def json_documents(page):
    decoder = json.JSONDecoder()
    for script in page.scripts:
        candidate = script.strip()
        marker = re.search(r"(?:netflix\.reactContext|window\.__NEXT_DATA__)\s*=\s*", script)
        if marker:
            candidate = script[marker.end():]
        try:
            yield decoder.raw_decode(candidate)[0]
        except ValueError:
            pass


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def inspect_page(profile, response, title_id=None):
    page = Page(response.get("body", ""))
    title = " ".join(page.title).strip()
    visible = " ".join(page.text).lower()
    documents = list(json_documents(page))
    region, region_source = None, None
    if profile == "hbo-max":
        match = re.search(r"\bcountryCode=([A-Z]{2})(?:[^A-Z]|$)", response.get("headers", ""))
        if match:
            region, region_source = match.group(1), "HBO Max country cookie"
    if profile == "netflix":
        for document in documents:
            for item in objects(document):
                for key in ("geo", "requestCountry", "countryOfRequest"):
                    value = item.get(key)
                    if not isinstance(value, dict):
                        continue
                    value = value.get("data", value)
                    if not isinstance(value, dict):
                        continue
                    code = value.get("id")
                    if isinstance(code, str) and re.fullmatch(r"[A-Z]{2}", code) and (value.get("countryName") or key != "geo"):
                        region, region_source = code, "Netflix country metadata"
                        break
                if region:
                    break
            if region:
                break
        localized = re.match(r"^/([a-z]{2})(?:-[a-z]{2})?/title/[0-9]+", urlparse(response.get("url", "")).path, re.I)
        if not region and localized and localized.group(1).lower() not in ("en", "ja", "ko", "zh"):
            region, region_source = localized.group(1).upper(), "Netflix localized redirect"

    path = urlparse(response.get("url", "")).path.lower()
    status = "homepage_accessible"
    if response.get("curl_exit") or not response.get("http_status"):
        status = "network_error"
    elif any(marker in response.get("body", "").lower() for marker in ("cf-chl-", "challenge-platform")) or title.lower() in ("just a moment...", "attention required! | cloudflare"):
        status = "challenge"
    elif response["http_status"] >= 400:
        status = "http_refused"
    elif any(marker in path for marker in ("/unavailable", "/geo-block", "/not-available", "plutotv-is-not-available")) or any(marker in title.lower() for marker in ("not available in your region", "unavailable in your region")):
        status = "region_unavailable"
    elif title_id:
        status = "title_unknown"
        for document in documents:
            for item in objects(document):
                kind = item.get("@type")
                if (kind in ("Movie", "TVSeries", "TVEpisode") or isinstance(kind, list) and any(value in kind for value in ("Movie", "TVSeries", "TVEpisode"))) and item.get("name"):
                    title = str(item["name"])
                    status = "title_visible"
                    break
            if status == "title_visible":
                break
        heading = next((text for text in page.headings if text.lower() not in ("netflix", "oh no!", "oh no", "sign in", "join now")), None)
        title_path = re.search(r"/title/" + re.escape(title_id) + r"/?$", path)
        if status != "title_visible" and heading and title_path:
            title, status = heading, "title_visible"
        if status != "title_visible" and ("oh no!" in visible or "this title is not available" in visible):
            status = "title_unavailable"
    elif not title:
        status = "page_unknown"
    return {
        "status": status, "http_status": response.get("http_status", 0),
        "url": response.get("url"), "seconds": response.get("seconds", 0),
        "title": html.unescape(title)[:180], "title_id": title_id,
        "region": region, "region_source": region_source,
        "playback_verified": False, "error": response.get("error") or None,
    }


def inspect_bbc_region(response):
    item = inspect_page("bbc-iplayer", response)
    try:
        data = json.loads(response.get("body", ""))
    except ValueError:
        return item
    for value in objects(data):
        if any(str(value.get(key, "")).lower() in ("geolocation", "geo-restricted", "notukerror") for key in ("result", "id", "error", "code")):
            item["status"] = "region_unavailable"
            return item
    if isinstance(data, dict) and isinstance(data.get("media"), list) and any(media.get("connection") for media in data["media"] if isinstance(media, dict)):
        item["status"] = "manifest_available"
    return item


def test_service(profile_id, country, exit_info, titles=None, requester=fetch):
    service = STREAMING.get(profile_id)
    if not service:
        raise ValueError("choose a known streaming service")
    proxy = "http://" + exit_info["proxy"]
    def request(url):
        try:
            return requester(proxy, url)
        except (OSError, subprocess.SubprocessError) as error:
            return {"http_status": 0, "url": url, "seconds": 0, "curl_exit": -1,
                    "body": "", "headers": "", "error": str(error)[:200]}
    started = int(time.time())
    geo = {}
    try:
        response = requester(proxy, "https://api.ip.sb/geoip")
        if response.get("curl_exit") == 0 and response.get("http_status") == 200:
            data = json.loads(response["body"])
            geo = {key: data.get(key) for key in ("ip", "country", "country_code", "city", "organization")}
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    if not geo.get("ip"):
        try:
            response = requester(proxy, "https://www.cloudflare.com/cdn-cgi/trace")
            if response.get("curl_exit") == 0 and response.get("http_status") == 200:
                fields = dict(line.split("=", 1) for line in response["body"].splitlines() if "=" in line)
                geo = {"ip": fields.get("ip"), "country_code": fields.get("loc"), "source": "Cloudflare trace"}
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    result = {
        "profile": profile_id, "service": service["name"], "requested_country": country,
        "exit": dict(exit_info), "geoip": geo, "started_at": started,
        "transport": "TCP / IPv4", "method_reference": REFERENCE,
        "homepage": inspect_page(profile_id, request(service.get("streaming_url") or service["probe_url"])),
        "titles": [], "playback_verified": False,
        "playback_status": "requires_account_and_player",
    }
    if profile_id == "netflix":
        for identifier in title_ids(titles) or DEFAULT_NETFLIX_TITLES:
            response = request("https://www.netflix.com/title/" + identifier)
            result["titles"].append(inspect_page(profile_id, response, identifier))
    if profile_id == "bbc-iplayer":
        result["eligibility"] = inspect_bbc_region(request("https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/mediaset/pc/vpid/bbc_one_london/format/json"))
    result["finished_at"] = int(time.time())
    return result


def test_matrix(country, exit_info, titles=None, requester=fetch):
    started = int(time.time())
    reports = [test_service(profile, country, exit_info, titles if profile == "netflix" else [], requester)
               for profile in MATRIX_PROFILES]
    return {"reports": reports, "requested_country": country, "exit": dict(exit_info),
            "started_at": started, "finished_at": int(time.time()), "transport": "TCP / IPv4",
            "playback_verified": False, "method_reference": REFERENCE}


def main():
    parser = argparse.ArgumentParser(description="Probe streaming region and public title visibility; does not verify paid playback")
    parser.add_argument("profile", choices=sorted(STREAMING))
    parser.add_argument("--proxy", required=True, help="localhost Gluetun HTTP proxy URL")
    parser.add_argument("--country", default="")
    parser.add_argument("--title", action="append", default=[])
    args = parser.parse_args()
    if not args.proxy.startswith("http://"):
        parser.error("use an http://localhost:port Gluetun proxy")
    info = {"proxy": args.proxy[len("http://"):]}
    print(json.dumps(test_service(args.profile, args.country, info, args.title), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
