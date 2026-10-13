import ipaddress
import json
import os
from functools import lru_cache


REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "service-domains.json")
with open(REGISTRY_PATH, "r", encoding="utf-8") as registry_file:
    SERVICES = json.load(registry_file)

PROFILES = [
    {"id": item["id"], "name": item["name"], "domains": list(item["domains"]),
     "streaming": bool(item.get("streaming_url") or item.get("probe_url"))}
    for item in SERVICES
]

SERVICE_BY_SUFFIX = {}
for item in SERVICES:
    for domain in item.get("stats_domains", item["domains"]):
        suffix = domain.lower().strip(".")
        previous = SERVICE_BY_SUFFIX.get(suffix)
        if previous is not None and previous != item["name"]:
            raise ValueError("ambiguous service domain: %s" % suffix)
        SERVICE_BY_SUFFIX[suffix] = item["name"]

MULTI_PART_PUBLIC_SUFFIXES = {
    "com.cn", "com.hk", "com.tw", "co.jp", "co.kr", "co.uk", "com.au", "com.br",
}


def root_domain(host):
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in MULTI_PART_PUBLIC_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


@lru_cache(maxsize=16384)
def service_name(host):
    host = (host or "").strip().lower().strip(".")
    if not host:
        return "(unknown)"
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host
    except ValueError:
        pass

    # Walk from the exact hostname to its parents so specific apps win.
    parts = host.split(".")
    for start in range(len(parts)):
        name = SERVICE_BY_SUFFIX.get(".".join(parts[start:]))
        if name is not None:
            return name
    return root_domain(host)
