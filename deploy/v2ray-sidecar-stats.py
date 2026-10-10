#!/usr/bin/env python3
import argparse
import json
import sys
import time
from collections import defaultdict


DEFAULT_STATS_FILE = "/var/lib/v2ray-egress-sidecar/stats.jsonl"

SERVICE_SUFFIXES = [
    ("x-twitter", [
        "x.com",
        "twitter.com",
        "t.co",
        "twimg.com",
        "tweetdeck.com",
        "pscp.tv",
        "periscope.tv",
    ]),
    ("openai-chatgpt", [
        "chatgpt.com",
        "openai.com",
        "oaistatic.com",
        "oaiusercontent.com",
    ]),
    ("claude-anthropic", [
        "claude.ai",
        "anthropic.com",
        "anthropicusercontent.com",
        "claudeusercontent.com",
    ]),
    ("youtube", [
        "youtube.com",
        "youtu.be",
        "ytimg.com",
        "googlevideo.com",
        "ggpht.com",
        "gvt1.com",
        "gvt2.com",
    ]),
    ("netflix", [
        "netflix.com",
        "netflix.net",
        "nflxvideo.net",
        "nflximg.net",
        "nflxext.com",
        "nflxso.net",
        "nflxsearch.net",
    ]),
    ("google", [
        "google.com",
        "googleapis.com",
        "gstatic.com",
    ]),
    ("apple", [
        "apple.com",
        "icloud.com",
        "itunes.apple.com",
        "push.apple.com",
    ]),
    ("microsoft", [
        "microsoft.com",
        "windows.com",
        "office.com",
        "live.com",
        "bing.com",
        "msftconnecttest.com",
    ]),
    ("ip-check", [
        "ip.sb",
        "ipinfo.io",
        "ifconfig.co",
        "icanhazip.com",
    ]),
    ("nordvpn", [
        "nordvpn.com",
        "nordaccount.com",
    ]),
    ("cloudflare", [
        "cloudflare.com",
        "cloudflare-dns.com",
    ]),
]


MULTI_PART_PUBLIC_SUFFIXES = {
    "com.cn",
    "com.hk",
    "com.tw",
    "co.jp",
    "co.kr",
    "co.uk",
    "com.au",
    "com.br",
}


def parse_duration(value):
    if not value:
        return None
    unit = value[-1].lower()
    number = value[:-1] if unit in "smhd" else value
    try:
        amount = float(number)
    except ValueError:
        raise SystemExit("bad duration: %s" % value)
    if unit == "s":
        return amount
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 3600
    if unit == "d":
        return amount * 86400
    return amount


def event_time_seconds(row):
    if "ended_at_unix_ms" in row:
        return float(row["ended_at_unix_ms"]) / 1000.0
    if "ts" in row:
        return float(row["ts"])
    if "started_at_unix_ms" in row:
        return float(row["started_at_unix_ms"]) / 1000.0
    return None


def host_from_row(row):
    return (row.get("target_host") or row.get("domain") or "").lower().strip(".")


def root_domain(host):
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    suffix2 = ".".join(parts[-2:])
    suffix3 = ".".join(parts[-3:])
    if suffix2 in MULTI_PART_PUBLIC_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    if suffix3 in MULTI_PART_PUBLIC_SUFFIXES and len(parts) >= 4:
        return ".".join(parts[-4:])
    return suffix2


def service_name(host):
    for name, suffixes in SERVICE_SUFFIXES:
        for suffix in suffixes:
            if host == suffix or host.endswith("." + suffix):
                return name
    return root_domain(host) or "(unknown)"


def bytes_human(value):
    value = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return "%d B" % value
            return "%.2f %s" % (value, unit)
        value /= 1024.0


def duration_human(value_ms):
    value_ms = int(round(float(value_ms or 0)))
    if value_ms < 1000:
        return "%d ms" % value_ms
    seconds = value_ms // 1000
    if seconds < 60:
        if seconds < 10 and value_ms % 1000:
            return "%.1fs" % (value_ms / 1000.0)
        return "%ds" % seconds
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return "%dm %ds" % (minutes, seconds) if seconds else "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh %dm" % (hours, minutes) if minutes else "%dh" % hours
    days, hours = divmod(hours, 24)
    return "%dd %dh" % (days, hours) if hours else "%dd" % days


def key_for_mode(mode, row):
    host = host_from_row(row)
    outbound = row.get("outbound") or "(none)"
    if mode == "domain":
        return (host or "(unknown)", outbound)
    if mode == "service":
        return (service_name(host), outbound)
    if mode == "outbound":
        return (outbound, "")
    if mode == "errors":
        return (host or "(unknown)", outbound)
    raise SystemExit("bad mode: %s" % mode)


def load_rows(path, since_seconds):
    cutoff = None
    if since_seconds is not None:
        cutoff = time.time() - since_seconds
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            ts = event_time_seconds(row)
            if cutoff is not None and ts is not None and ts < cutoff:
                continue
            yield row


def aggregate(args):
    groups = defaultdict(lambda: {
        "conns": 0,
        "ok": 0,
        "errors": 0,
        "uplink": 0,
        "downlink": 0,
        "elapsed": 0,
    })

    for row in load_rows(args.file, parse_duration(args.since)):
        is_error = row.get("error") is not None or row.get("status") not in (None, "ok")
        if args.mode == "errors" and not is_error:
            continue
        key = key_for_mode(args.mode, row)
        item = groups[key]
        item["conns"] += 1
        item["uplink"] += int(row.get("uplink_bytes") or 0)
        item["downlink"] += int(row.get("downlink_bytes") or 0)
        item["elapsed"] += int(row.get("elapsed_ms") or 0)
        if is_error:
            item["errors"] += 1
        else:
            item["ok"] += 1

    rows = []
    for key, item in groups.items():
        total = item["uplink"] + item["downlink"]
        avg_ms = item["elapsed"] / item["conns"] if item["conns"] else 0
        rows.append((key, item, total, avg_ms))

    sort_by = args.sort
    if sort_by == "conns":
        rows.sort(key=lambda x: x[1]["conns"], reverse=True)
    elif sort_by == "errors":
        rows.sort(key=lambda x: x[1]["errors"], reverse=True)
    else:
        rows.sort(key=lambda x: x[2], reverse=True)

    if args.top > 0:
        rows = rows[:args.top]

    print("%-38s %-14s %8s %8s %8s %12s %12s %12s %12s" % (
        "name", "outbound", "conns", "ok", "errors", "uplink", "downlink", "total", "avg_life"
    ))
    for key, item, total, avg_ms in rows:
        name, outbound = key
        print("%-38s %-14s %8d %8d %8d %12s %12s %12s %12s" % (
            name[:38],
            outbound[:14],
            item["conns"],
            item["ok"],
            item["errors"],
            bytes_human(item["uplink"]),
            bytes_human(item["downlink"]),
            bytes_human(total),
            duration_human(avg_ms),
        ))


def main():
    parser = argparse.ArgumentParser(
        description="Summarize v2ray-egress-sidecar JSONL stats by domain, service, outbound, or errors. avg_life is average connection/session lifetime."
    )
    parser.add_argument("mode", nargs="?", default="domain",
                        choices=["domain", "service", "outbound", "errors"])
    parser.add_argument("--file", default=DEFAULT_STATS_FILE)
    parser.add_argument("--top", type=int, default=30,
                        help="number of rows to show; use 0 for all")
    parser.add_argument("--since", default=None,
                        help="time window, for example 10m, 1h, 1d")
    parser.add_argument("--sort", choices=["bytes", "conns", "errors"], default="bytes")
    aggregate(parser.parse_args())


if __name__ == "__main__":
    main()
