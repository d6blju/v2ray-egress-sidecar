import argparse
import html
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from html.parser import HTMLParser
from urllib.parse import urlparse

from sidecar_services import SERVICES
from sidecar_unlock import classify_dns


REFERENCE = "https://github.com/lmc999/RegionRestrictionCheck"
STREAMING = {item["id"]: item for item in SERVICES if item.get("streaming_url") or item.get("probe_url")}
MATRIX_PROFILES = ["tiktok", "disney-plus", "netflix", "youtube", "prime-video", "dazn", "reddit", "openai-chatgpt", "hbo-max", "claude-anthropic"]
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


def fetch(proxy, url, timeout=12, method="GET", headers=None, data=None):
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
        if method != "GET":
            args[1:1] = ["--request", method.upper()]
        insert_at = args.index("--dump-header")
        for header_name, header_value in (headers or {}).items():
            args[insert_at:insert_at] = ["--header", "%s: %s" % (header_name, header_value)]
            insert_at += 2
        if data is not None:
            args[insert_at:insert_at] = ["--data-binary", data]
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


def unlock_result(status, evidence, region=None, region_source=None):
    return {
        "status": status,
        "evidence": evidence,
        "region": region,
        "region_source": region_source,
        "playback_verified": False,
    }


def _response_status(response):
    if response.get("curl_exit") or not response.get("http_status"):
        return "network_error"
    if response.get("http_status", 0) >= 400:
        return "http_refused"
    return "ok"


def inspect_chatgpt(request):
    compliance = request("https://api.openai.com/compliance/cookie_requirements")
    ios = request("https://ios.chat.openai.com/")
    body1 = compliance.get("body", "")
    body2 = ios.get("body", "")
    if _response_status(compliance) == "network_error" or _response_status(ios) == "network_error":
        return unlock_result("network_error", "OpenAI 专用接口请求失败")
    if not body1 or not body2:
        return unlock_result("unknown", "OpenAI 专用接口返回空响应")

    unsupported = "unsupported_country" in body1.lower()
    vpn = "vpn" in body2.lower()
    # Keep the four-way result used by RegionRestrictionCheck: the two
    # endpoints distinguish country support from browser-only availability.
    if not unsupported and not vpn:
        return unlock_result("unlocked", "OpenAI 合规接口和 iOS 接口均未返回限制标记")
    if unsupported and vpn:
        return unlock_result("region_unavailable", "OpenAI 同时返回 unsupported_country 和 VPN")
    if vpn:
        return unlock_result("browser_only", "OpenAI 仅允许网页端，iOS 接口返回 VPN 标记")
    return unlock_result("mobile_only", "OpenAI 合规接口返回 unsupported_country，移动接口未返回 VPN")


def inspect_tiktok(homepage):
    body = homepage.get("body", "")
    match = re.search(r"[\\\"']region[\\\"']\\s*:\\s*[\\\"']([A-Z]{2})[\\\"']", body)
    if match:
        return unlock_result("unlocked", "TikTok 页面 region 字段", match.group(1), "TikTok page metadata")
    if "idc ip" in body.lower():
        return unlock_result("homepage_only", "TikTok 仅返回 IDC IP 字段，未取得服务地区")
    return unlock_result("unknown", "TikTok 页面没有可判定的 region 字段")


def inspect_disney(request):
    # RegionRestrictionCheck uses an anonymous Disney client flow. Keep its
    # public client material outside the repository because it changes and is
    # not needed by the sidecar itself.
    device_auth = os.environ.get("SIDECAR_DISNEY_DEVICE_AUTH", "").strip()
    token_payload = os.environ.get("SIDECAR_DISNEY_TOKEN_PAYLOAD", "").strip()
    graph_payload = os.environ.get("SIDECAR_DISNEY_GRAPHQL_PAYLOAD", "").strip()
    if not device_auth or not token_payload or not graph_payload:
        return unlock_result("unknown", "Disney+ 专用接口未配置匿名客户端参数")

    devices = request(
        "https://disney.api.edge.bamgrid.com/devices", method="POST",
        headers={"Authorization": device_auth, "Content-Type": "application/json"},
        data=json.dumps({"deviceFamily": "browser", "applicationRuntime": "chrome",
                         "deviceProfile": "windows", "attributes": {}}),
    )
    device_body = devices.get("body", "")
    if devices.get("http_status") == 403 or "403 error" in device_body.lower():
        return unlock_result("ip_blocked", "Disney+ devices 接口返回 403")
    assertion_match = re.search(r'"assertion"\s*:\s*"([^"]+)"', device_body)
    if not assertion_match:
        return unlock_result("unknown", "Disney+ devices 接口没有 assertion")

    token = request(
        "https://disney.api.edge.bamgrid.com/token", method="POST",
        headers={"Authorization": device_auth, "Content-Type": "application/x-www-form-urlencoded"},
        data=token_payload.replace("DISNEYASSERTION", assertion_match.group(1)),
    )
    token_body = token.get("body", "")
    if "forbidden-location" in token_body.lower() or token.get("http_status") == 403:
        return unlock_result("ip_blocked", "Disney+ token 接口返回 forbidden-location/403")
    refresh_match = re.search(r'"refresh_token"\s*:\s*"([^"]+)"', token_body)
    if not refresh_match:
        return unlock_result("unknown", "Disney+ token 接口没有 refresh_token")

    graph = request(
        "https://disney.api.edge.bamgrid.com/graph/v1/device/graphql", method="POST",
        headers={"Authorization": device_auth, "Content-Type": "application/json"},
        data=graph_payload.replace("ILOVEDISNEY", refresh_match.group(1)),
    )
    preview = request("https://disneyplus.com")
    graph_body = graph.get("body", "")
    region_match = re.search(r'"countryCode"\s*:\s*"([^"]+)"', graph_body)
    supported_match = re.search(r'"inSupportedLocation"\s*:\s*(false|true)', graph_body)
    if not region_match:
        return unlock_result("region_unavailable", "Disney+ GraphQL 没有 countryCode")
    region = region_match.group(1).upper()
    if region == "JP":
        return unlock_result("unlocked", "Disney+ GraphQL 返回支持地区", region, "Disney+ device GraphQL")
    if re.search(r"preview|unavailable", preview.get("url", ""), re.I):
        return unlock_result("region_unavailable", "Disney+ preview 地址表示服务不可用", region, "Disney+ device GraphQL")
    if supported_match and supported_match.group(1) == "false":
        return unlock_result("service_available", "Disney+ 返回地区信息但尚未在该地区完整开放", region, "Disney+ device GraphQL")
    if supported_match and supported_match.group(1) == "true":
        return unlock_result("unlocked", "Disney+ GraphQL 返回 inSupportedLocation=true", region, "Disney+ device GraphQL")
    return unlock_result("unknown", "Disney+ GraphQL 没有 inSupportedLocation", region, "Disney+ device GraphQL")


def inspect_dazn(request):
    payload = json.dumps({
        "Version": "2", "LandingPageKey": "generic", "Languages": "zh-CN",
        "Platform": "web", "Manufacturer": "", "PromoCode": "",
        "PlatformAttributes": {},
    })
    response = request(
        "https://startup.core.indazn.com/misl/v5/Startup", method="POST",
        headers={
            "Accept": "*/*", "Content-Type": "application/json",
            "Origin": "https://www.dazn.com", "Referer": "https://www.dazn.com/",
            "X-Session-Id": str(uuid.uuid4()),
        }, data=payload,
    )
    if _response_status(response) != "ok":
        return unlock_result(_response_status(response), "DAZN Startup 接口请求失败")
    if "security policy has been breached" in response.get("body", "").lower():
        return unlock_result("ip_blocked", "DAZN Startup 返回 Security policy has been breached")
    try:
        data = json.loads(response.get("body", ""))
    except ValueError:
        return unlock_result("unknown", "DAZN Startup 返回不是 JSON")
    values = list(objects(data))
    allowed = next((item.get("isAllowed") for item in values if "isAllowed" in item), None)
    region = next((item.get("GeolocatedCountry") for item in values if item.get("GeolocatedCountry")), None)
    if allowed is False:
        return unlock_result("region_unavailable", "DAZN isAllowed=false", region, "DAZN Startup")
    if allowed is True:
        return unlock_result("unlocked", "DAZN isAllowed=true", region, "DAZN Startup")
    return unlock_result("unknown", "DAZN Startup 没有 isAllowed 字段", region, "DAZN Startup" if region else None)


def inspect_prime(homepage):
    body = homepage.get("body", "")
    region_match = re.search(r'"currentTerritory"\s*:\s*"([^"]+)"', body)
    if "isServiceRestricted" in body:
        return unlock_result("region_unavailable", "Prime Video 返回 isServiceRestricted")
    if region_match:
        return unlock_result("unlocked", "Prime Video 返回 currentTerritory", region_match.group(1), "Prime Video page metadata")
    if homepage.get("status") == "network_error":
        return unlock_result("network_error", "Prime Video 页面网络请求失败")
    return unlock_result("unknown", "Prime Video 页面没有 currentTerritory 或限制字段")


def inspect_youtube_premium(homepage):
    body = homepage.get("body", "")
    lower = body.lower()
    if "www.google.cn" in lower:
        return unlock_result("region_unavailable", "YouTube Premium 页面跳转到 Google CN", "CN", "YouTube Premium page")
    if "premium is not available in your country" in lower:
        return unlock_result("region_unavailable", "YouTube Premium 页面提示所在国家不可用")
    region_match = re.search(r'"INNERTUBE_CONTEXT_GL"\s*:\s*"([^"]+)"', body)
    region = region_match.group(1).upper() if region_match else "UNKNOWN"
    if "ad-free" in lower:
        return unlock_result("unlocked", "YouTube Premium 页面包含 ad-free 能力标记", region, "YouTube Premium page metadata")
    if homepage.get("status") == "network_error":
        return unlock_result("network_error", "YouTube Premium 页面网络请求失败")
    return unlock_result("unknown", "YouTube Premium 页面没有 ad-free 或地区限制字段", region if region != "UNKNOWN" else None, "YouTube Premium page metadata" if region != "UNKNOWN" else None)


def inspect_claude(homepage):
    final_url = homepage.get("url", "")
    if final_url == "https://claude.ai/":
        return unlock_result("unlocked", "Claude 最终地址保持在 claude.ai")
    if final_url == "https://www.anthropic.com/app-unavailable-in-region":
        return unlock_result("region_unavailable", "Claude 跳转到地区不可用页面")
    if homepage.get("status") == "network_error":
        return unlock_result("network_error", "Claude 页面网络请求失败")
    return unlock_result("unknown", "Claude 最终跳转地址不是已知结果")


def classify_unlock(profile_id, homepage, titles, eligibility, request, homepage_response=None):
    if profile_id == "netflix":
        title_reports = titles or []
        visible = [item for item in title_reports if item.get("status") == "title_visible"]
        region = next((item.get("region") for item in visible if item.get("region")), None)
        if visible:
            return unlock_result("unlocked", "至少一个公开片名详情页可见", region, "Netflix title metadata")
        statuses = [item.get("http_status") for item in title_reports]
        if statuses and all(value == 404 for value in statuses):
            return unlock_result("originals_only", "测试片名均为 HTTP 404，仅能推断可能是 Originals Only")
        if any(value == 403 for value in statuses):
            return unlock_result("region_unavailable", "Netflix 测试片名返回 HTTP 403")
        if any(item.get("status") == "title_unavailable" for item in title_reports):
            return unlock_result("originals_only", "Netflix 公开详情页提示片名不可用")
        if homepage.get("status") == "challenge":
            return unlock_result("challenge", "Netflix 首页需要浏览器验证")
        return unlock_result("unknown", "Netflix 片名接口没有返回足够证据")

    if profile_id == "openai-chatgpt":
        return inspect_chatgpt(request)
    if profile_id == "tiktok":
        return inspect_tiktok(homepage)
    if profile_id == "disney-plus":
        return inspect_disney(request)
    if profile_id == "dazn":
        return inspect_dazn(request)
    if profile_id == "prime-video":
        return inspect_prime(homepage_response or homepage)
    if profile_id == "youtube":
        return inspect_youtube_premium(homepage_response or homepage)
    if profile_id == "claude-anthropic":
        return inspect_claude(homepage)
    if profile_id == "bbc-iplayer":
        if eligibility and eligibility.get("status") == "manifest_available":
            return unlock_result("unlocked", "BBC media selector 返回可用媒体 manifest", "GB", "BBC media selector")
        if eligibility and eligibility.get("status") == "region_unavailable":
            return unlock_result("region_unavailable", "BBC media selector 返回地区限制")
    if homepage.get("status") == "region_unavailable":
        return unlock_result("region_unavailable", "服务页面明确返回地区不可用", homepage.get("region"), homepage.get("region_source"))
    if homepage.get("status") == "challenge":
        return unlock_result("challenge", "服务页面需要浏览器验证")
    if homepage.get("status") == "http_refused":
        return unlock_result("http_refused", "服务页面被 HTTP 拒绝")
    if homepage.get("status") == "network_error":
        return unlock_result("network_error", "服务页面网络请求失败")
    if homepage.get("status") == "homepage_accessible":
        return unlock_result("homepage_only", "仅确认首页可访问，未取得地区/片库专用证据", homepage.get("region"), homepage.get("region_source"))
    return unlock_result("unknown", "服务没有返回可判定的专用结果")


def test_service(profile_id, country, exit_info, titles=None, requester=fetch, dns_runner=None, dns_cache=None):
    service = STREAMING.get(profile_id)
    if not service:
        raise ValueError("choose a known streaming service")
    proxy = "http://" + exit_info["proxy"]
    def request(url, method="GET", headers=None, data=None):
        try:
            if method == "GET" and not headers and data is None:
                return requester(proxy, url)
            if requester is fetch:
                return requester(proxy, url, method=method, headers=headers, data=data)
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
    homepage_response = request(service.get("streaming_url") or service["probe_url"])
    result = {
        "profile": profile_id, "service": service["name"], "requested_country": country,
        "exit": dict(exit_info), "geoip": geo, "started_at": started,
        "transport": "TCP / IPv4", "method_reference": REFERENCE,
        "homepage": inspect_page(profile_id, homepage_response),
        "titles": [], "playback_verified": False,
        "playback_status": "requires_account_and_player",
    }
    if profile_id == "netflix":
        for identifier in title_ids(titles) or DEFAULT_NETFLIX_TITLES:
            response = request("https://www.netflix.com/title/" + identifier)
            result["titles"].append(inspect_page(profile_id, response, identifier))
    if profile_id == "bbc-iplayer":
        result["eligibility"] = inspect_bbc_region(request("https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/mediaset/pc/vpid/bbc_one_london/format/json"))
    result["dns"] = classify_dns(profile_id, exit_info, dns_runner, dns_cache)
    result["unlock"] = classify_unlock(profile_id, result["homepage"], result["titles"], result.get("eligibility"), request, homepage_response)
    result["method"] = result["dns"]["method"]
    result["method_label"] = result["dns"]["method_label"]
    result["finished_at"] = int(time.time())
    return result


def test_matrix(country, exit_info, titles=None, requester=fetch):
    started = int(time.time())
    dns_cache = {}
    reports = [test_service(profile, country, exit_info, titles if profile == "netflix" else [], requester,
                             dns_cache=dns_cache)
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
