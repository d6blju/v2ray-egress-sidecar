#!/usr/bin/env python3
import json
import ipaddress
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


CONFIG_PATH = os.environ.get("SIDECAR_CONFIG", "/etc/v2ray-egress-sidecar/config.toml")
STATE_DIR = os.environ.get("SIDECAR_ADMIN_STATE_DIR", "/var/lib/v2ray-egress-sidecar-admin")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
COUNTRY_CACHE_PATH = os.path.join(STATE_DIR, "nordvpn-countries.json")
COUNTRY_OPTIONS_CACHE_PATH = os.path.join(STATE_DIR, "nordvpn-country-options.json")
COUNTRY_URL = "https://api.nordvpn.com/v1/servers/countries"
START_SCRIPT = os.environ.get("SIDECAR_GLUETUN_START", "/usr/local/bin/nordvpn-gluetun-start")
OUTBOUND_TAG = os.environ.get("SIDECAR_UI_OUTBOUND_TAG", "nord-ui")
CONTAINER_TAG = os.environ.get("SIDECAR_UI_CONTAINER_TAG", "ui")
CONTAINER_NAME = os.environ.get("SIDECAR_UI_CONTAINER_NAME", "gluetun-ui")
PROXY_PORT = os.environ.get("SIDECAR_UI_PROXY_PORT", "18892")
PROXY_ADDR = "127.0.0.1:%s" % PROXY_PORT
SIDECAR_SOCKS = os.environ.get("SIDECAR_SOCKS", "127.0.0.1:10880")
API_HOST = os.environ.get("SIDECAR_ADMIN_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("SIDECAR_ADMIN_PORT", "18082"))
MANAGED_BEGIN = "# sidecar-ui-managed-begin"
MANAGED_END = "# sidecar-ui-managed-end"

DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+$")
COUNTRY_RE = re.compile(r"^[A-Za-z0-9 .,'()&+-]+$")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "v2ray-egress-sidecar"))
from sidecar_services import PROFILES
from sidecar_exits import ExitPool
from sidecar_streaming import STREAMING, title_ids
from sidecar_streaming_jobs import StreamingJobs
from sidecar_route_transaction import RouteTransaction, RouteError, read_text, digest
from sidecar_auth import AuthManager, AuthError

EXIT_POOL = ExitPool()
STREAMING_JOBS = None
MUTATION_LOCK = threading.Lock()
COUNTRY_REFRESH_LOCK = threading.Lock()
COUNTRY_LAST_ATTEMPT = 0
CHECK_BIN = os.environ.get('SIDECAR_CHECK_BIN', '/usr/local/bin/v2ray-egress-config-check')
SIDECAR_SERVICE = os.environ.get('SIDECAR_SERVICE', 'v2ray-egress-sidecar.service')
AUTH = None


def auth_manager():
    global AUTH
    if AUTH is None:
        AUTH = AuthManager(STATE_DIR, htpasswd=os.environ.get('SIDECAR_HTPASSWD_FILE'),
                           secure=os.environ.get('SIDECAR_COOKIE_SECURE', '1') == '1',
                           cookie_path=os.environ.get('SIDECAR_COOKIE_PATH', '/'))
    return AUTH


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

FALLBACK_COUNTRIES = [
    "Australia",
    "Bangladesh",
    "Canada",
    "France",
    "Germany",
    "Hong Kong",
    "Japan",
    "Netherlands",
    "Philippines",
    "Singapore",
    "Taiwan",
    "United Kingdom",
    "United States",
]

FALLBACK_COUNTRY_CODES = {
    "Australia": "AU",
    "Bangladesh": "BD",
    "Canada": "CA",
    "France": "FR",
    "Germany": "DE",
    "Hong Kong": "HK",
    "Japan": "JP",
    "Netherlands": "NL",
    "Philippines": "PH",
    "Singapore": "SG",
    "Taiwan": "TW",
    "United Kingdom": "GB",
    "United States": "US",
}


def ensure_state_dir():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)


def json_response(handler, status, payload, cookies=None):
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'DENY')
    handler.send_header('Referrer-Policy', 'same-origin')
    for cookie in cookies or []:
        handler.send_header('Set-Cookie', cookie)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler, status, message, details=None):
    payload = {"ok": False, "error": message}
    if details:
        payload["details"] = details
    json_response(handler, status, payload)


def run_cmd(args, timeout=60):
    result = subprocess.run(args, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    out = (result.stdout or "")[-4000:]
    err = (result.stderr or "")[-4000:]
    if result.returncode != 0:
        raise RuntimeError("%s failed: %s%s" % (" ".join(args), out, err))
    return out.strip()


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, data):
    ensure_state_dir()
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def fetch_country_options():
    cached = load_json(COUNTRY_OPTIONS_CACHE_PATH, None)
    cache_age = 10**9
    if cached and os.path.exists(COUNTRY_OPTIONS_CACHE_PATH):
        cache_age = time.time() - os.path.getmtime(COUNTRY_OPTIONS_CACHE_PATH)
    if cached and cache_age < 86400 and isinstance(cached, list):
        return cached
    try:
        with urllib.request.urlopen(COUNTRY_URL, timeout=8) as response:
            data = json.load(response)
        options = sorted(
            [
                {"name": item["name"], "code": item.get("code") or ""}
                for item in data
                if item.get("name")
            ],
            key=lambda item: item["name"],
        )
        if options:
            save_json(COUNTRY_OPTIONS_CACHE_PATH, options)
            save_json(COUNTRY_CACHE_PATH, [item["name"] for item in options])
            return options
    except Exception:
        pass
    legacy = load_json(COUNTRY_CACHE_PATH, None)
    if legacy and isinstance(legacy, list):
        return [
            {"name": name, "code": FALLBACK_COUNTRY_CODES.get(name, "")}
            for name in legacy
        ]
    return [
        {"name": name, "code": FALLBACK_COUNTRY_CODES.get(name, "")}
        for name in FALLBACK_COUNTRIES
    ]


def country_options():
    global COUNTRY_LAST_ATTEMPT
    cached = load_json(COUNTRY_OPTIONS_CACHE_PATH, None)
    if not isinstance(cached, list) or not cached:
        legacy = load_json(COUNTRY_CACHE_PATH, FALLBACK_COUNTRIES)
        if not isinstance(legacy, list) or not legacy:
            legacy = FALLBACK_COUNTRIES
        cached = [{"name": name, "code": FALLBACK_COUNTRY_CODES.get(name, "")} for name in legacy]
    age = time.time() - os.path.getmtime(COUNTRY_OPTIONS_CACHE_PATH) if os.path.exists(COUNTRY_OPTIONS_CACHE_PATH) else 86401
    if age >= 86400 and (COUNTRY_LAST_ATTEMPT == 0 or time.monotonic() - COUNTRY_LAST_ATTEMPT >= 300) and COUNTRY_REFRESH_LOCK.acquire(False):
        COUNTRY_LAST_ATTEMPT = time.monotonic()
        def refresh():
            try:
                fetch_country_options()
            finally:
                COUNTRY_REFRESH_LOCK.release()
        threading.Thread(target=refresh, name="country-refresh", daemon=True).start()
    return cached


def countries():
    return [item["name"] for item in country_options()]


def normalize_domain(value):
    value = value.strip().lower()
    value = value.replace("domain:", "")
    if value.startswith("*."):
        value = value[2:]
    value = value.strip(".")
    if not value or not DOMAIN_RE.match(value) or "." not in value:
        raise ValueError("bad domain: %s" % value)
    return value


def normalize_domains(values):
    domains = []
    seen = set()
    for raw in values:
        for part in re.split(r"[\s,;]+", raw or ""):
            if not part:
                continue
            domain = normalize_domain(part)
            if domain not in seen:
                seen.add(domain)
                domains.append(domain)
    return domains


def profile_domains(profile_id):
    for profile in PROFILES:
        if profile["id"] == profile_id:
            return list(profile["domains"])
    if profile_id == "custom":
        return []
    raise ValueError("unknown profile: %s" % profile_id)


def remove_managed_block(text):
    while MANAGED_BEGIN in text and MANAGED_END in text:
        start = text.index(MANAGED_BEGIN)
        end = text.index(MANAGED_END, start) + len(MANAGED_END)
        if end < len(text) and text[end:end + 1] == "\n":
            end += 1
        text = text[:start].rstrip() + "\n\n" + text[end:].lstrip()
    return text.strip() + "\n"


def toml_sections(text):
    sections = []
    current = []
    for line in text.splitlines():
        if line.startswith("[[") and current:
            sections.append("\n".join(current).rstrip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).rstrip())
    return sections


def section_value(section, key):
    pattern = re.compile(r'^\s*%s\s*=\s*([\'"])(.*?)\1\s*(?:#.*)?$' % re.escape(key))
    for line in section.splitlines():
        match = pattern.match(line)
        if match:
            return match.group(2)
    return None


def remove_managed_artifacts(text):
    clean = remove_managed_block(text)
    kept = []
    for section in toml_sections(clean):
        stripped = section.lstrip()
        if stripped.startswith("[[outbounds]]") and section_value(section, "tag") == OUTBOUND_TAG:
            continue
        if stripped.startswith("[[rules]]") and section_value(section, "outbound") == OUTBOUND_TAG:
            continue
        if section.strip():
            kept.append(section.strip())
    return "\n\n".join(kept).strip() + "\n"


def saved_routes(state):
    if not state.get('active'):
        return []
    if 'routes' in state:
        return [dict(route) for route in state['routes']]
    # Migrate the existing single service without losing its exit or domains.
    return [{key: state.get(key) for key in
             ('profile', 'country', 'protocol', 'domains', 'include_ip_sb', 'proxy',
              'container', 'updated_at', 'exit_check')}]


def upsert_route(routes, selected):
    kept = []
    for route in routes:
        if route['profile'] == selected['profile']:
            continue
        for domain in selected['domains']:
            for existing in route['domains']:
                overlap = (domain == existing or domain.endswith('.' + existing)
                           or existing.endswith('.' + domain))
                if overlap and route['proxy'] != selected['proxy']:
                    raise ValueError('domain %s conflicts with saved service %s; other services were not changed'
                                     % (domain, route['profile']))
        kept.append(dict(route))
    return kept + [dict(selected)]


def delete_service_state(previous, profile_id):
    routes = saved_routes(previous)
    remaining = [route for route in routes if route['profile'] != profile_id]
    if len(remaining) == len(routes):
        return None
    state = dict(previous)
    if remaining:
        selected = next((r for r in remaining if r['profile'] == previous.get('profile')), remaining[-1])
        state.update(selected)
        state['active'] = True
    else:
        state.update(active=False, profile=None, country=None, protocol=None, domains=[],
                     include_ip_sb=False, proxy=None, container=None, gluetun=None, exit_check=None)
    state.update(routes=remaining, updated_at=int(time.time()))
    return state


def candidate_config(current, routes):
    clean = remove_managed_artifacts(current)
    lines = [MANAGED_BEGIN, '# managed_by = v2ray-sidecar-admin-api', '']
    tags = {}
    for route in routes:
        proxy = route['proxy']
        if proxy not in tags:
            tag = OUTBOUND_TAG if not tags else OUTBOUND_TAG + '-' + str(len(tags) + 1)
            tags[proxy] = tag
            lines.extend(['[[outbounds]]', 'tag = ' + json.dumps(tag),
                          'kind = "http_connect"', 'addr = ' + json.dumps(proxy), ''])
    # Specific suffixes win when service domains overlap.
    rules = [(domain, route) for route in routes for domain in route['domains']]
    seen = set()
    for domain, route in sorted(rules, key=lambda item: -len(item[0])):
        if domain in seen:
            continue
        seen.add(domain)
        lines.extend(['# profile = ' + route['profile'], '# country = ' + route['country'],
                      '[[rules]]', 'domain_suffix = ' + json.dumps('.' + domain),
                      'outbound = ' + json.dumps(tags[route['proxy']]), ''])
    lines.append(MANAGED_END)
    block = '\n'.join(lines) + '\n'
    marker = '\n[[rules]]'
    if marker in clean:
        idx = clean.index(marker) + 1
        return clean[:idx] + block + '\n' + clean[idx:]
    return clean.rstrip() + '\n\n' + block


def validate_route_config(text):
    if text is None:
        raise ValueError('configuration is missing')
    if text.count(MANAGED_BEGIN) != text.count(MANAGED_END) or text.count(MANAGED_BEGIN) > 1:
        raise ValueError('unbalanced UI-managed configuration markers')
    if MANAGED_BEGIN in text and text.index(MANAGED_BEGIN) > text.index(MANAGED_END):
        raise ValueError('invalid UI-managed marker order')
    ensure_state_dir()
    fd, tmp = tempfile.mkstemp(prefix='.validate-', suffix='.toml', dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        run_cmd([CHECK_BIN, '--check', '--config', tmp], timeout=10)
    finally:
        os.unlink(tmp)


def reload_checkpoint():
    pid = int(run_cmd(['systemctl', 'show', '-p', 'MainPID', SIDECAR_SERVICE], timeout=5).split('=')[-1])
    if not pid:
        raise RouteError('sidecar is not running')
    rows = run_cmd(['journalctl', '-u', SIDECAR_SERVICE, '-n', '1', '-o', 'json', '--no-pager'], timeout=5)
    cursor = json.loads(rows.splitlines()[-1])['__CURSOR']
    return {'pid': pid, 'cursor': cursor}


def confirm_reload(checkpoint, text):
    interval = re.search(r'^\s*reload_interval_secs\s*=\s*(\d+)', text, re.M)
    timeout = min(120, max(15, int(interval.group(1)) * 2 + 5 if interval else 15))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if read_text(CONFIG_PATH) != text:
            raise RouteError('configuration changed while waiting for sidecar reload')
        pid = int(run_cmd(['systemctl', 'show', '-p', 'MainPID', SIDECAR_SERVICE], timeout=5).split('=')[-1])
        if pid != checkpoint['pid']:
            raise RouteError('sidecar process changed during transaction')
        rows = run_cmd(['journalctl', '-u', SIDECAR_SERVICE, '--after-cursor', checkpoint['cursor'],
                        '-n', '30', '-o', 'json', '--no-pager'], timeout=5)
        for line in rows.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if str(entry.get('_PID')) != str(pid):
                continue
            message = entry.get('MESSAGE', '')
            if 'config reload rejected:' in message or 'config reload failed:' in message:
                raise RouteError('sidecar rejected candidate configuration')
            if 'config reloaded successfully' in message:
                return {'confirmed': True, 'pid': pid, 'config_sha256': digest(text)}
        time.sleep(.5)
    raise RouteError('sidecar hot-reload acknowledgement timed out')


def verify_exit(info, expected_code=None):
    raw = run_cmd(['curl', '-fsS', '--ipv4', '--noproxy', '', '--proxy', 'http://' + info['proxy'],
                   '--connect-timeout', '4', '--max-time', '10', 'https://www.cloudflare.com/cdn-cgi/trace'], timeout=15)
    fields = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    ip = str(ipaddress.ip_address(fields.get('ip', '')))
    country = fields.get('loc', '').upper()
    if not re.fullmatch('[A-Z]{2}', country):
        raise RouteError('exit probe did not return a country')
    if expected_code and country != expected_code:
        raise RouteError('exit country mismatch: requested %s, measured %s' % (expected_code, country))
    return {'ok': True, 'ip': ip, 'country_code': country, 'country_source': 'Cloudflare via selected exit'}


def verify_forwarding():
    run_cmd(['curl', '-fsS', '--ipv4', '--noproxy', '', '--socks5-hostname', SIDECAR_SOCKS,
             '--connect-timeout', '4', '--max-time', '10', '-o', '/dev/null', 'https://www.example.com/'], timeout=15)


def route_transaction():
    return RouteTransaction(CONFIG_PATH, STATE_PATH, STATE_DIR)


def validate_route_state():
    text = read_text(STATE_PATH)
    if text is not None and not isinstance(json.loads(text), dict):
        raise ValueError('state file is not a JSON object')


def gluetun_status(container=None):
    container = container or CONTAINER_NAME
    try:
        status = run_cmd(["docker", "inspect", "-f", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}", container], timeout=10)
    except Exception as exc:
        return {"container": container, "status": "missing", "health": "missing", "error": str(exc)}
    parts = status.split()
    return {
        "container": container,
        "status": parts[0] if parts else "unknown",
        "health": parts[1] if len(parts) > 1 else "unknown",
    }


def protected_proxy_ports():
    with open(CONFIG_PATH, "r", encoding="utf-8") as source:
        text = source.read()
    pending = read_text(route_transaction().journal)
    if pending:
        text += '\n' + json.loads(pending)['old_config']
    sections = toml_sections(text)
    outbounds, referenced = [], set()
    for section in sections:
        if section.lstrip().startswith("[[outbounds]]"):
            outbounds.append((section_value(section, "tag"), section_value(section, "addr")))
        if section.lstrip().startswith("[[rules]]"):
            referenced.add(section_value(section, "outbound"))
    if outbounds:
        referenced.add(outbounds[0][0])
    ports = set()
    for tag, address in outbounds:
        if tag in referenced and address and re.fullmatch(r"127\.0\.0\.1:[0-9]+", address):
            ports.add(int(address.split(":")[1]))
    if STREAMING_JOBS:
        active = STREAMING_JOBS.active_countries()
        ports.update(item["port"] for item in EXIT_POOL.list() if item["country"].casefold() in active)
    return ports


def preferred_proxy_port(country):
    state = load_json(STATE_PATH, {})
    address = state.get("proxy", "")
    if state.get("country") == country and re.fullmatch(r"127\.0\.0\.1:[0-9]+", address):
        return int(address.split(":")[1])
    return int(PROXY_PORT)


def start_gluetun(country, protocol):
    info = EXIT_POOL.ensure(country, protocol, preferred_proxy_port(country), protected_proxy_ports())
    return {"exit": info, "status": {key: info[key] for key in ("container", "status", "health")}}


def check_ip():
    args = ["curl", "-fsS", "--ipv4", "--noproxy", "", "--connect-timeout", "4", "--max-time", "10", "--socks5-hostname", SIDECAR_SOCKS]
    try:
        raw = run_cmd(args + ["https://api.ip.sb/geoip"], timeout=15)
        data = json.loads(raw)
        ipaddress.ip_address(data["ip"])
        if data.get("country_code") or data.get("country"):
            return dict({"ok": True, "country_source": "ip.sb"}, **{key: data.get(key) for key in ("ip", "country", "country_code", "city", "organization")})
    except Exception:
        pass
    try:
        ip = run_cmd(args + ["https://api.ip.sb/ip"], timeout=15)
        ip = str(ipaddress.ip_address(ip.strip()))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        # Look up the measured IP: another hostname can use a different sidecar route.
        raw = run_cmd(args + ["https://ipwho.is/" + ip], timeout=15)
        data = json.loads(raw)
        if data.get("success") is True and str(ipaddress.ip_address(data["ip"])) == ip and data.get("country_code"):
            return {"ok": True, "ip": ip, "country": data.get("country"), "country_code": data["country_code"],
                    "city": data.get("city"), "country_source": "ipwho.is (measured ip.sb IP)"}
    except Exception:
        pass
    return {"ok": True, "ip": ip, "country_source": None}


def current_state(exits=None):
    state = load_json(STATE_PATH, {})
    state.setdefault("active", False)
    state.setdefault("outbound", OUTBOUND_TAG)
    state.setdefault("proxy", PROXY_ADDR)
    container = state.get("container")
    # An inactive UI route must not be reconstructed from a cached exit. Manual
    # rules may still use that exit, but they are intentionally outside this UI state.
    if exits is not None and state["active"] and not container:
        match = next((item for item in exits if item["country"] == state.get("country") and EXIT_POOL.healthy(item)), None)
        if match:
            state.update(container=match["container"], proxy=match["proxy"], protocol=match["protocol"])
            container = match["container"]
    state["routes"] = saved_routes(state)
    state["gluetun"] = gluetun_status(container) if state["active"] and container else None
    transaction = route_transaction()
    state['last_transaction'] = transaction.result()
    state['last_failure'] = load_json(transaction.failure, None)
    config = read_text(CONFIG_PATH)
    state['config_matches_state'] = bool(config is not None and state.get('config_sha256') == digest(config)) if state.get('config_sha256') else None
    state['transaction_pending'] = bool(read_text(transaction.journal))
    return state


class Handler(BaseHTTPRequestHandler):
    server_version = "v2ray-sidecar-admin-api/1.0"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        try:
            auth = auth_manager()
            if self.path == '/api/health':
                json_response(self, 200, {'ok': True, 'auth_configured': bool(auth.users() or (auth.htpasswd and os.path.isfile(auth.htpasswd)))})
                return
            if self.path in ('/api/session', '/api/auth/check'):
                token, session, cookie = auth.bootstrap(self, internal=self.path == '/api/auth/check')
                if self.path == '/api/auth/check':
                    self.send_response(204 if session['username'] else 401)
                    if cookie:
                        self.send_header('Set-Cookie', cookie)
                    self.send_header('Content-Length', '0')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    return
                json_response(self, 200, {'ok': True, 'authenticated': bool(session['username']),
                                         'username': session['username'], 'csrf_token': auth.csrf(token),
                                         'expires_at': session['expires']}, [cookie] if cookie else None)
                return
            auth.require(self)
        except AuthError as error:
            error_response(self, error.status, str(error))
            return
        if self.path == "/api/state":
            options = country_options()
            try:
                exits = EXIT_POOL.list()
                exit_error = None
            except Exception as error:
                exits, exit_error = [], str(error)
            json_response(self, 200, {
                "ok": True,
                "profiles": PROFILES,
                "countries": [item["name"] for item in options],
                "country_options": options,
                "country_refreshing": COUNTRY_REFRESH_LOCK.locked(),
                "state": current_state(exits),
                "exits": exits,
                "exit_error": exit_error,
                "streaming_jobs": STREAMING_JOBS.recent() if STREAMING_JOBS else [],
                "notes": {
                    "ip_check": "api.ip.sb is tested through sidecar SOCKS on %s" % SIDECAR_SOCKS,
                    "udp": "Current NordVPN HTTP CONNECT outbound carries TCP only; sidecar UDP remains direct.",
                },
            })
            return
        if self.path.startswith("/api/streaming/"):
            job = STREAMING_JOBS.get(self.path.rsplit("/", 1)[-1]) if STREAMING_JOBS else None
            if job is None:
                error_response(self, 404, "test not found")
            else:
                json_response(self, 200, {"ok": True, "job": job})
            return
        if self.path == "/api/ipcheck":
            json_response(self, 200, check_ip())
            return
        error_response(self, 404, "not found")

    def do_POST(self):
        try:
            auth = auth_manager()
            token, session = auth.require(self, write=True, prelogin=self.path == '/api/login')
        except AuthError as error:
            error_response(self, error.status, str(error))
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0:
                raise ValueError()
        except ValueError:
            error_response(self, 400, 'bad content length')
            return
        if length > 65536:
            error_response(self, 413, "payload too large")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError('JSON object required')
        except Exception as exc:
            error_response(self, 400, "bad json", str(exc))
            return
        try:
            if self.path == '/api/login':
                username = auth.authenticate(payload.get('username'), payload.get('password'), auth.peer(self))
                new_token = auth.create(username, old=token)
                new_session = auth.lookup(new_token)
                json_response(self, 200, {'ok': True, 'authenticated': True, 'username': username,
                                         'csrf_token': auth.csrf(new_token), 'expires_at': new_session['expires']},
                              [auth.cookie(new_token), auth.cookie('', 'sidecar_logged_out', 0)])
                return
            if self.path == '/api/logout':
                auth.logout(token)
                json_response(self, 200, {'ok': True}, [auth.cookie('', max_age=0), auth.cookie('1', 'sidecar_logged_out')])
                return
            if self.path == "/api/apply":
                with MUTATION_LOCK:
                    self.handle_apply(payload)
                return
            if self.path == "/api/routes/delete":
                with MUTATION_LOCK:
                    self.handle_delete(payload)
                return
            if self.path == "/api/streaming/test":
                profile = str(payload.get("profile") or "")
                country = str(payload.get("country") or "").strip()
                protocol = str(payload.get("protocol") or "udp")
                if (profile not in STREAMING and profile != "matrix") or country not in countries() or protocol not in ("udp", "tcp"):
                    raise ValueError("choose a known streaming service, country and protocol")
                titles = title_ids(payload.get("titles") or [])
                if profile not in ("netflix", "matrix") and titles:
                    raise ValueError("custom title probes currently support Netflix only")
                job = STREAMING_JOBS.submit(profile, country, protocol, titles, preferred_proxy_port(country))
                json_response(self, 202, {"ok": True, "job": job})
                return
            if self.path == "/api/clear":
                with MUTATION_LOCK:
                    transaction = route_transaction()
                    with transaction.locked():
                        transaction.recover(reload_checkpoint, confirm_reload)
                        validate_route_state()
                        current = read_text(CONFIG_PATH)
                        state = load_json(STATE_PATH, {})
                        state.update(
                        active=False,
                        profile=None,
                        country=None,
                        protocol=None,
                        domains=[],
                        routes=[],
                        include_ip_sb=False,
                        proxy=None,
                        container=None,
                        gluetun=None,
                        updated_at=int(time.time()),
                        outbound=OUTBOUND_TAG,
                        )
                        state, result = transaction.execute(current, remove_managed_artifacts(current), state,
                                                            validate_route_config, reload_checkpoint, confirm_reload, verify_forwarding)
                json_response(self, 200, {"ok": True, "state": current_state(), "backup": result['backup']})
                return
        except AuthError as error:
            error_response(self, error.status, str(error))
            return
        except Exception as exc:
            if self.path in ('/api/apply', '/api/clear', '/api/routes/delete'):
                transaction = route_transaction()
                if not getattr(exc, 'transaction_result', None):
                    transaction.fail(exc)
            error_response(self, 500, "operation failed", str(exc))
            return
        error_response(self, 404, "not found")

    def handle_delete(self, payload):
        profile_id = str(payload.get('profile') or '')
        if not profile_id:
            error_response(self, 400, 'service profile is required')
            return
        transaction = route_transaction()
        with transaction.locked():
            transaction.recover(reload_checkpoint, confirm_reload)
            validate_route_state()
            current = read_text(CONFIG_PATH)
            previous = load_json(STATE_PATH, {})
            state = delete_service_state(previous, profile_id)
            if state is None:
                error_response(self, 404, 'saved service route was not found; refresh the page')
                return
            if previous.get('config_sha256') != digest(current):
                raise RouteError('configuration changed externally; reconcile saved routes before deleting')
            candidate = (candidate_config(current, state['routes']) if state['routes']
                         else remove_managed_artifacts(current))
            state, result = transaction.execute(current, candidate, state, validate_route_config,
                                                reload_checkpoint, confirm_reload, verify_forwarding)
        json_response(self, 200, {'ok': True, 'state': current_state(), 'deleted_profile': profile_id,
                                 'backup': result['backup']})

    def handle_apply(self, payload):
        profile_id = str(payload.get("profile") or "custom")
        country = str(payload.get("country") or "").strip()
        protocol = str(payload.get("protocol") or "udp").strip().lower()
        include_ip_sb = bool(payload.get("include_ip_sb", True))
        if profile_id != 'custom' and profile_id not in {p['id'] for p in PROFILES}:
            raise ValueError('choose a known service profile')
        country_names = countries()
        if not country or country not in country_names:
            raise ValueError("choose a valid NordVPN country")
        # Explicit domains are the complete edited list, including removals.
        domains = payload.get('domains') if 'domains' in payload else profile_domains(profile_id)
        if not isinstance(domains, list) or any(not isinstance(d, str) for d in domains):
            raise ValueError('domains must be a list of strings')
        domains = list(domains)
        if include_ip_sb:
            domains.extend(["ip.sb", "api.ip.sb"])
        else:
            domains = [d for d in normalize_domains(domains) if d not in ('ip.sb', 'api.ip.sb')]
        domains = normalize_domains(domains)
        if not domains:
            raise ValueError("no domains selected")
        transaction = route_transaction()
        with transaction.locked():
            transaction.recover(reload_checkpoint, confirm_reload)
            validate_route_state()
            current = read_text(CONFIG_PATH)
            validate_route_config(current)
            gluetun = start_gluetun(country, protocol)
            info = gluetun["exit"]
            expected_code = next((c.get('code') for c in country_options() if c['name'] == country), None)
            exit_check = verify_exit(info, expected_code)
            previous = load_json(STATE_PATH, {})
            if previous.get('active') and previous.get('config_sha256') != digest(current):
                raise RouteError('configuration changed externally; reconcile saved routes before applying')
            state = {
            "active": True,
            "profile": profile_id,
            "country": country,
            "protocol": protocol,
            "domains": domains,
            "include_ip_sb": include_ip_sb,
            "outbound": OUTBOUND_TAG,
            "proxy": info["proxy"],
            "container": info["container"],
            "updated_at": int(time.time()),
            "gluetun": gluetun.get("status", {}),
            'exit_check': exit_check,
            }
            routes = upsert_route(saved_routes(previous), state)
            state['routes'] = routes
            candidate = candidate_config(current, routes)
            def postcheck():
                verify_exit(info, expected_code)
                verify_forwarding()
            state, result = transaction.execute(current, candidate, state, validate_route_config,
                                                reload_checkpoint, confirm_reload, postcheck)
        json_response(self, 200, {"ok": True, "state": current_state(), "backup": result['backup'],
                                 "exit_check": exit_check, "ip_check": check_ip(), "exit_reused": info["reused"]})


def main():
    global STREAMING_JOBS
    ensure_state_dir()
    if API_HOST not in ('localhost',) and not ipaddress.ip_address(API_HOST).is_loopback:
        raise RuntimeError('management API must listen on loopback')
    auth_manager()
    transaction = route_transaction()
    with transaction.locked():
        try:
            transaction.recover(reload_checkpoint, confirm_reload)
        except Exception as error:
            # Keep read-only status available; the pending journal blocks new writes.
            print('route recovery needs attention: %s' % error, file=sys.stderr)
    STREAMING_JOBS = StreamingJobs(EXIT_POOL, protected_proxy_ports, STATE_DIR, mutation_lock=MUTATION_LOCK)
    def maintain_exits():
        while True:
            time.sleep(60)
            try:
                if read_text(route_transaction().journal):
                    continue
                if time.time() - os.path.getmtime(CONFIG_PATH) < 30:
                    continue
                with MUTATION_LOCK:
                    EXIT_POOL.prune_duplicates(protected_proxy_ports())
            except Exception:
                pass
    threading.Thread(target=maintain_exits, name="exit-maintenance", daemon=True).start()
    server = ThreadingHTTPServer((API_HOST, API_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
