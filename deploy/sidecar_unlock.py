import ipaddress
import os
import random
import re
import subprocess
import time


REFERENCE = "https://github.com/lmc999/RegionRestrictionCheck"
DNS_DOMAINS = {
    "netflix": ["netflix.com"],
    "disney-plus": ["disneyplus.com"],
    "tiktok": ["tiktok.com"],
    "openai-chatgpt": ["chat.openai.com", "ios.chat.openai.com", "api.openai.com"],
    "bbc-iplayer": ["bbc.co.uk"],
    "dazn": ["dazn.com"],
    "hbo-max": ["max.com"],
    "youtube": ["youtube.com"],
    "prime-video": ["primevideo.com"],
    "reddit": ["reddit.com"],
    "claude-anthropic": ["claude.ai"],
}

METHOD_LABELS = {
    "native": "原生",
    "dns": "DNS",
    "unknown": "未知",
    "unavailable": "未检测",
}


def _valid_hostname(value):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", value or ""))


def _valid_container(value):
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value or ""))


def _parse_ip(value):
    value = (value or "").strip().split()[0] if value else ""
    value = value.split("#", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _parse_nslookup(output):
    addresses = []
    server = None
    after_name = False
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("name:"):
            after_name = True
            continue
        if not re.search(r"\baddress(?:es)?:", stripped, re.I):
            continue
        value = stripped.split(":", 1)[1].strip()
        parsed = _parse_ip(value)
        if not parsed:
            continue
        if not after_name and server is None:
            server = parsed
        elif after_name:
            addresses.append(parsed)
        elif server is not None:
            addresses.append(parsed)
    return {"resolver": server, "answers": list(dict.fromkeys(addresses))}


def _parse_getent(output):
    answers = []
    for line in (output or "").splitlines():
        parsed = _parse_ip(line)
        if parsed:
            answers.append(parsed)
    return {"resolver": None, "answers": list(dict.fromkeys(answers))}


def _docker_dns(container, hostname):
    if not _valid_container(container) or not _valid_hostname(hostname):
        return {"available": False, "answers": [], "resolver": None, "error": "invalid DNS target"}
    commands = [
        (["docker", "exec", container, "nslookup", hostname], _parse_nslookup),
        (["docker", "exec", container, "getent", "ahostsv4", hostname], _parse_getent),
    ]
    errors = []
    for args, parser in commands:
        try:
            result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, timeout=8)
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(str(error)[:160])
            continue
        if result.returncode == 0 or result.stdout:
            parsed = parser(result.stdout)
            return {"available": True, "answers": parsed["answers"],
                    "resolver": parsed.get("resolver"),
                    "error": result.stderr[:160] if result.returncode else ""}
        errors.append(result.stderr[:160])
    return {"available": False, "answers": [], "resolver": None,
            "error": "; ".join(value for value in errors if value)[:200]}


def _same_ipv4_subnet(first, second):
    try:
        a = ipaddress.ip_address(first)
        b = ipaddress.ip_address(second)
    except ValueError:
        return False
    if a.version != b.version:
        return False
    if a.version == 4:
        return int(a) >> 8 == int(b) >> 8
    return int(a) >> 80 == int(b) >> 80


def _dns_ip_check(answer, resolver):
    if not answer:
        return 0
    try:
        ip = ipaddress.ip_address(answer)
    except ValueError:
        return 0
    if not ip.is_global or _same_ipv4_subnet(answer, resolver):
        return 0
    return 1


def _probe_one(container, hostname, runner):
    first = runner(container, hostname)
    if not first.get("available"):
        return {"domain": hostname, "available": False, "checks": {},
                "answers": [], "resolver": None, "error": first.get("error", "")}
    random_name = "test%d%d.%s" % (random.randrange(10000, 99999), random.randrange(10000, 99999), hostname)
    third = runner(container, random_name)
    answers = first.get("answers") or []
    checks = {
        # RegionRestrictionCheck's Check_DNS_1: private/local or resolver-adjacent
        # answers are a DNS-unlock signal.
        "answer_ip": _dns_ip_check(answers[0] if answers else None, first.get("resolver")),
        # Its Check_DNS_2 treats 0, 1 and 2 answers as the special-DNS result.
        "answer_count": 0 if len(answers) in (0, 1, 2) else 1,
        # Its random-name check detects wildcard DNS responses.
        "random_name": 1 if not (third.get("answers") or []) else 0,
    }
    method = "dns" if 0 in checks.values() else "native"
    return {
        "domain": hostname, "available": True, "answers": answers,
        "resolver": first.get("resolver"), "random_answers": third.get("answers") or [],
        "checks": checks, "method": method, "method_label": METHOD_LABELS[method],
        "error": first.get("error", ""),
    }


def classify_dns(service_id, exit_info, runner=None, cache=None):
    runner = runner or _docker_dns
    cache = cache if cache is not None else {}
    container = exit_info.get("container") or ""
    reports = []
    for hostname in DNS_DOMAINS.get(service_id, []):
        key = (container, hostname)
        report = cache.get(key)
        if report is None:
            report = _probe_one(container, hostname, runner)
            cache[key] = report
        reports.append(report)
    available = [report for report in reports if report.get("available")]
    if not available:
        method = "unknown"
    elif any(report.get("method") == "dns" for report in available):
        method = "dns"
    elif len(available) == len(reports):
        method = "native"
    else:
        method = "unknown"
    return {
        "reference": REFERENCE,
        "service": service_id,
        "container": container or None,
        "method": method,
        "method_label": METHOD_LABELS[method],
        "checks": reports,
        "checked_at": int(time.time()),
    }
