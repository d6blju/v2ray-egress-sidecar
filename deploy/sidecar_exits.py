import argparse
import contextlib
import hashlib
import json
import os
import re
import socket
import subprocess
import time


def run(args, timeout=15):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("%s failed: %s" % (" ".join(args[:2]), result.stderr[-800:]))
    return result.stdout.strip()


def exit_metadata(item):
    env = dict(value.partition("=")[::2] for value in item.get("Config", {}).get("Env", []) if "=" in value)
    ports = (item.get("NetworkSettings", {}).get("Ports") or {}).get("8888/tcp") or (item.get("HostConfig", {}).get("PortBindings") or {}).get("8888/tcp") or []
    local = [int(port["HostPort"]) for port in ports if port.get("HostIp") == "127.0.0.1"]
    if env.get("VPN_SERVICE_PROVIDER") != "nordvpn" or not local:
        return None
    country = env.get("SERVER_COUNTRIES", "").strip()
    if not country or "," in country:
        return None
    state = item.get("State", {})
    name = item.get("Name", "").lstrip("/")
    labels = item.get("Config", {}).get("Labels") or {}
    return {
        "container": name, "id": item.get("Id", "")[:12], "country": country,
        "protocol": env.get("OPENVPN_PROTOCOL", "udp"), "port": local[0],
        "proxy": "127.0.0.1:%d" % local[0], "status": state.get("Status", "unknown"),
        "health": state.get("Health", {}).get("Status", "no-health"),
        "created": item.get("Created", ""),
        "managed": labels.get("v2ray-egress-sidecar.managed") == "1" or name in ("gluetun-ui", "gluetun-bd", "gluetun-ph"),
    }


class ExitPool:
    def __init__(self, runner=run, state_dir=None):
        self.run = runner
        self.state_dir = state_dir or os.environ.get("SIDECAR_EXIT_STATE_DIR", "/var/lib/v2ray-egress-sidecar-exits")
        self.max_countries = int(os.environ.get("SIDECAR_MAX_VPN_COUNTRIES", "3"))
        self.min_available_kb = int(os.environ.get("SIDECAR_VPN_MIN_AVAILABLE_KB", "327680"))
        self.max_host_cpu_percent = float(os.environ.get("SIDECAR_MAX_HOST_CPU_PERCENT", "80"))
        self.host_cpu_reserve_percent = float(os.environ.get("SIDECAR_HOST_CPU_RESERVE_PERCENT", "20"))
        self.container_memory = os.environ.get("SIDECAR_GLUETUN_MEMORY", "128m")
        self.container_pids = int(os.environ.get("SIDECAR_GLUETUN_PIDS", "128"))
        budget = max(1.0, self.max_host_cpu_percent - self.host_cpu_reserve_percent)
        self.container_cpu_percent = budget / max(1, self.max_countries)

    @contextlib.contextmanager
    def locked(self):
        import fcntl
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        with open(os.path.join(self.state_dir, "pool.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def list(self):
        ids = self.run(["docker", "ps", "-a", "--filter", "name=gluetun-", "--format", "{{.ID}}"], 10).splitlines()
        if not ids:
            return []
        items = json.loads(self.run(["docker", "inspect"] + ids, 10))
        exits = [exit_metadata(item) for item in items]
        return sorted((item for item in exits if item is not None), key=lambda item: (item["created"], item["port"]))

    def healthy(self, item):
        return item["status"] == "running" and item["health"] in ("healthy", "no-health")

    def connected(self, port):
        try:
            connections = self.run(["ss", "-Hnt", "state", "established"], 5)
        except Exception:
            return True
        return re.search(r"(?:127\.0\.0\.1|\[::ffff:127\.0\.0\.1\]):%d(?:\s|$)" % port, connections) is not None

    def _prune_duplicates(self, exits, protected_ports):
        removed = []
        countries = {}
        for item in exits:
            countries.setdefault(item["country"].casefold(), []).append(item)
        for items in countries.values():
            if len(items) < 2:
                continue
            keep = sorted(items, key=lambda item: (item["port"] not in protected_ports, not self.healthy(item), item["created"]))[0]
            for item in items:
                if item is keep or not item["managed"] or item["port"] in protected_ports or self.connected(item["port"]):
                    continue
                self.run(["docker", "rm", "-f", item["container"]], 15)
                removed.append(item["container"])
        return removed

    def prune_duplicates(self, protected_ports):
        with self.locked():
            return self._prune_duplicates(self.list(), set(protected_ports))

    def _available_memory(self):
        try:
            with open("/proc/meminfo") as info:
                for line in info:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1])
        except OSError:
            pass
        return 0

    def _host_cpu_percent(self):
        try:
            with open("/proc/loadavg", encoding="utf-8") as info:
                load = float(info.read().split()[0])
            return load / max(1, os.cpu_count() or 1) * 100.0
        except (OSError, ValueError, IndexError):
            return 0.0

    def _container_cpus(self):
        cpus = self.container_cpu_percent / 100.0 * max(1, os.cpu_count() or 1)
        return "%.3f" % max(0.05, cpus)

    def _resource_args(self):
        return [
            "--memory", self.container_memory,
            "--memory-swap", self.container_memory,
            "--cpus", self._container_cpus(),
            "--pids-limit", str(self.container_pids),
        ]

    def _limit_container(self, item):
        if not item.get("managed"):
            return
        self.run(["docker", "update"] + self._resource_args() + [item["container"]], 15)

    def _free_port(self, preferred, exits):
        used = {item["port"] for item in exits}
        for port in [preferred] + list(range(18893, 18933)):
            if port in used or not 1024 <= port <= 65535:
                continue
            with socket.socket() as listener:
                try:
                    listener.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    pass
        raise RuntimeError("no available localhost VPN proxy port")

    def ensure(self, country, protocol="udp", preferred_port=18892, protected_ports=None):
        country = country.strip()
        if not re.fullmatch(r"[A-Za-z0-9 .,'()&+-]+", country) or protocol not in ("udp", "tcp"):
            raise ValueError("invalid country or protocol")
        with self.locked():
            exits = self.list()
            existing = [item for item in exits if item["country"].casefold() == country.casefold()]
            if existing:
                compatible = [item for item in existing if item["protocol"] == protocol]
                if not compatible:
                    raise ValueError("this country already has an exit using %s; reuse that protocol" % existing[0]["protocol"])
                item = sorted(compatible, key=lambda value: (not self.healthy(value), value["port"] != preferred_port, value["created"]))[0]
                if item["status"] in ("exited", "created"):
                    self._limit_container(item)
                    self.run(["docker", "start", item["container"]], 15)
                elif not self.healthy(item):
                    raise RuntimeError("existing exit is unhealthy; it was not replaced")
                else:
                    self._limit_container(item)
                return self._wait(item["container"], reused=True)

            credential_file = os.environ.get("SIDECAR_NORDVPN_ENV", "/etc/gluetun/nordvpn.env")
            if not os.path.isfile(credential_file):
                raise RuntimeError("NordVPN manual setup credentials are not configured")
            with open(credential_file, "r", encoding="utf-8") as credentials:
                fields = dict(line.strip().partition("=")[::2] for line in credentials if "=" in line and not line.lstrip().startswith("#"))
            if not fields.get("OPENVPN_USER") or not fields.get("OPENVPN_PASSWORD"):
                raise RuntimeError("NordVPN manual setup credentials are incomplete")
            image = os.environ.get("SIDECAR_GLUETUN_IMAGE", "qmcgaw/gluetun:latest")
            self.run(["docker", "image", "inspect", image], 10)
            protected = set(protected_ports or ())
            if protected_ports is not None:
                self._prune_duplicates(exits, protected)
            exits = self.list()
            projected_cpu = self._host_cpu_percent() + self.container_cpu_percent
            if projected_cpu > self.max_host_cpu_percent:
                raise RuntimeError("VPN exit capacity reached; host CPU would exceed %.0f%% (now %.1f%%, new exit %.1f%%)" %
                                   (self.max_host_cpu_percent, self._host_cpu_percent(), self.container_cpu_percent))
            while len({item["country"].casefold() for item in exits}) >= self.max_countries or self._available_memory() < self.min_available_kb:
                unused = [item for item in exits if item["managed"] and item["port"] not in protected and not self.connected(item["port"])]
                if protected_ports is None or not unused:
                    raise RuntimeError("VPN exit capacity reached; all cached exits are in use or memory is low")
                self.run(["docker", "rm", "-f", unused[0]["container"]], 15)
                exits = self.list()
            port = self._free_port(preferred_port, exits)
            suffix = re.sub(r"[^a-z0-9]+", "-", country.lower()).strip("-")[:36]
            name = "gluetun-egress-%s-%s" % (suffix, hashlib.sha256(country.casefold().encode()).hexdigest()[:6])
            self.run([
                "docker", "run", "-d", "--name", name,
                "--label", "v2ray-egress-sidecar.managed=1", "--cap-add=NET_ADMIN",
                "--device", "/dev/net/tun:/dev/net/tun", "--restart", "unless-stopped",
                *self._resource_args(), "-p", "127.0.0.1:%d:8888/tcp" % port,
                "--env-file", credential_file, "-e", "VPN_SERVICE_PROVIDER=nordvpn",
                "-e", "VPN_TYPE=openvpn", "-e", "OPENVPN_PROTOCOL=" + protocol,
                "-e", "SERVER_COUNTRIES=" + country, "-e", "HTTPPROXY=on",
                "-e", "HTTPPROXY_LISTENING_ADDRESS=:8888", image,
            ], 30)
            return self._wait(name, reused=False)

    def _wait(self, name, reused):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            item = next((entry for entry in self.list() if entry["container"] == name), None)
            if item and self.healthy(item):
                return dict(item, reused=reused)
            if item and item["status"] in ("dead", "exited"):
                break
            time.sleep(1)
        raise RuntimeError("VPN exit did not become healthy: " + name)


def main():
    parser = argparse.ArgumentParser(description="Reuse or start a country-specific NordVPN exit")
    parser.add_argument("country")
    parser.add_argument("--protocol", choices=("udp", "tcp"), default="udp")
    parser.add_argument("--preferred-port", type=int, default=18892)
    args = parser.parse_args()
    print(json.dumps(ExitPool().ensure(args.country, args.protocol, args.preferred_port)))


if __name__ == "__main__":
    main()
