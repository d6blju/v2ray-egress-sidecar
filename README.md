# v2ray-egress-sidecar

[English](README.md) | [简体中文](README.zh-CN.md)

A SOCKS5 egress sidecar for V2Ray/Xray with domain/service traffic stats,
hot-reload routing rules, and VPN exit selection.

`v2ray-egress-sidecar` is not a V2Ray fork and does not replace V2Ray. It sits
behind an existing V2Ray/Xray outbound:

```text
client -> V2Ray/Xray -> v2ray-egress-sidecar -> direct / SOCKS5 / HTTP CONNECT
```

It is also not affiliated with the V2Ray or Xray projects.

## Features

- SOCKS5 TCP `CONNECT` inbound for V2Ray/Xray.
- SOCKS5 `UDP ASSOCIATE` support for direct UDP relay.
- Per-domain JSONL traffic stats: connections, uplink bytes, downlink bytes,
  errors, and connection lifetime.
- Hot reload for route rules, outbounds, stats path, connection limits, and
  idle timeouts.
- Outbound types: `direct`, `socks5`, and `http_connect`.
- Domain matching by exact domain, suffix, or substring.
- Optional connection limit and TCP/UDP idle cleanup.
- Helper scripts for systemd installation, stats summaries, and
  Gluetun/NordVPN HTTP CONNECT exits.
- Optional web UI for stats, route editing, NordVPN country selection, and
  recent-domain discovery.

## Limitations

- The sidecar does not decrypt TLS. It sees the SOCKS target host that V2Ray/Xray
  passes to it. If V2Ray/Xray sends only an IP address, domain-based routing and
  domain stats cannot infer the hidden hostname.
- `http_connect` outbounds carry TCP streams only. UDP traffic cannot be sent
  through an HTTP CONNECT proxy.
- UDP is currently relayed directly. To send UDP through a VPN, run the sidecar
  inside the VPN network namespace or add a UDP-capable outbound design.
- QUIC/TLS ClientHello SNI parsing is not implemented in this version.

## Build

Install Rust, then build the release binary:

```bash
git clone https://github.com/d6blju/v2ray-egress-sidecar.git
cd v2ray-egress-sidecar
cargo build --release
```

The binary will be:

```text
target/release/v2ray-egress-sidecar
```

## Install With Systemd

Run from the repository root:

```bash
sudo ./deploy/install.sh
```

The script installs:

- binary: `/usr/local/bin/v2ray-egress-sidecar`
- config: `/etc/v2ray-egress-sidecar/config.toml`
- stats directory: `/var/lib/v2ray-egress-sidecar`
- service: `v2ray-egress-sidecar.service`

Manual installation:

```bash
sudo install -d /etc/v2ray-egress-sidecar /var/lib/v2ray-egress-sidecar
sudo install -m 0755 target/release/v2ray-egress-sidecar /usr/local/bin/v2ray-egress-sidecar
sudo install -m 0644 config.example.toml /etc/v2ray-egress-sidecar/config.toml
sudo install -m 0644 systemd/v2ray-egress-sidecar.service /etc/systemd/system/v2ray-egress-sidecar.service
sudo systemctl daemon-reload
sudo systemctl enable --now v2ray-egress-sidecar.service
```

Check status:

```bash
systemctl status v2ray-egress-sidecar.service
journalctl -u v2ray-egress-sidecar.service -f
```

## Configuration

Example:

```toml
listen = "127.0.0.1:10880"
stats_path = "/var/lib/v2ray-egress-sidecar/stats.jsonl"
reload_interval_secs = 5
max_connections = 1024
thread_stack_bytes = 262144
tcp_idle_timeout_secs = 300
tcp_half_close_timeout_secs = 10
udp_idle_timeout_secs = 60

[[outbounds]]
tag = "direct"
kind = "direct"

[[outbounds]]
tag = "nord-http"
kind = "http_connect"
addr = "127.0.0.1:18888"

[[rules]]
domain_suffix = ".ip.sb"
outbound = "nord-http"

[[rules]]
domain_suffix = ".netflix.com"
outbound = "nord-http"
```

Supported root keys:

- `listen`: SOCKS5 listen address. Changing this requires a service restart.
- `stats_path`: JSONL stats output path.
- `reload_interval_secs`: how often to check the config file for changes.
- `max_connections`: active connection cap. `0` means unlimited.
- `thread_stack_bytes`: worker thread stack size.
- `tcp_idle_timeout_secs`: TCP stream idle timeout.
- `tcp_half_close_timeout_secs`: timeout after one side half-closes.
- `udp_idle_timeout_secs`: UDP association idle timeout.

Outbound fields:

- `tag`: unique outbound name.
- `kind`: `direct`, `socks5`, or `http_connect`.
- `addr`: proxy address for `socks5` or `http_connect`.
- `username` / `password`: optional SOCKS5 username/password auth.

Rule fields:

- `domain_exact = "example.com"`
- `domain_suffix = ".example.com"`
- `domain_contains = "example"`
- `outbound = "direct"`

Rules are evaluated in order. The first outbound in the config is the default
fallback when no rule matches.

Most config changes are hot reloaded. A bad config is rejected and the last good
config keeps serving traffic.

## V2Ray/Xray Integration

Add a SOCKS outbound in V2Ray/Xray:

```json
{
  "tag": "egress-sidecar",
  "protocol": "socks",
  "settings": {
    "servers": [
      {
        "address": "127.0.0.1",
        "port": 10880
      }
    ]
  }
}
```

Then route selected traffic to it:

```json
{
  "type": "field",
  "outboundTag": "egress-sidecar",
  "domain": [
    "domain:netflix.com",
    "domain:ip.sb"
  ]
}
```

You can also make this outbound the V2Ray/Xray default route after testing
throughput and connection count under your workload.

## NordVPN/Gluetun Exit

This project does not run the official NordVPN client on the host. A safer
pattern is to run a Gluetun container and expose a localhost-only HTTP CONNECT
proxy:

```bash
sudo install -d -m 0700 /etc/gluetun
sudo tee /etc/gluetun/nordvpn.env >/dev/null <<'EOF'
OPENVPN_USER=your-manual-setup-user
OPENVPN_PASSWORD=your-manual-setup-password
EOF
sudo chmod 0600 /etc/gluetun/nordvpn.env
```

Start a country exit:

```bash
sudo ./deploy/nordvpn-gluetun-start.sh ph "Philippines" 18888
```

Use it from `config.toml`:

```toml
[[outbounds]]
tag = "nord-ph"
kind = "http_connect"
addr = "127.0.0.1:18888"
```

The helper returns JSON with the actual `proxy` address and `container` name.
Use that address in your outbound: an existing country exit can use a different
port from the preferred port passed on the command line. Healthy exits are
reused per country; changing country creates or reuses a separate cached exit.
Changing the OpenVPN protocol for an existing country requires retiring its
unused exit first. V2Ray/Xray does not need a restart for these route changes.

## Web UI

The optional web UI has two parts:

- Stats dashboard: reads `stats.jsonl` and renders `index.html` plus
  `stats.json`.
- Routing control page: edits a managed block in `config.toml`, starts or
  reuses a Gluetun/NordVPN HTTP CONNECT exit, and checks `ip.sb`
  through the sidecar.

Install the UI after installing the sidecar:

```bash
sudo ./deploy/install-ui.sh
```

This installs:

- static files: `/var/www/v2ray-egress-sidecar-dashboard`
- renderer: `/usr/local/bin/v2ray-egress-dashboard-render`
- admin API: `/usr/local/bin/v2ray-egress-admin-api`
- admin state: `/var/lib/v2ray-egress-sidecar-admin`
- services:
  - `v2ray-egress-sidecar-dashboard-render.timer`
  - `v2ray-egress-sidecar-admin-api.service`

The admin API listens on localhost only:

```text
http://127.0.0.1:18082/api/state
```

Use nginx to expose it. Start with the example:

```bash
sudo cp /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf.example /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf
sudo htpasswd -c /etc/nginx/.htpasswd-v2ray-egress-sidecar sidecar
sudo nginx -t
sudo systemctl reload nginx
```

The example exposes a local-only dashboard on:

```text
http://127.0.0.1:18081/
```

It also includes public-path snippets for your existing HTTPS server:

```text
https://your-domain.example/sidecar-stats/
https://your-domain.example/sidecar-stats/control.html
```

Keep the public UI and API behind Basic Auth. Do not expose
`/sidecar-stats/api/` without authentication.

Using the routing control page:

1. Choose a site/app profile or enter custom domain suffixes.
2. Choose a NordVPN country. The country search accepts Chinese names, English
   names, and country codes when the browser supports region names.
3. Choose OpenVPN UDP or TCP.
4. Click `Apply Route`.
5. Click `Check ip.sb` to see the current sidecar exit IP, country, country code,
   and city when available. A fallback looks up the measured IP and labels
   its source, rather than assuming different domains share an exit.
6. Click `Clear UI Rules` to remove only the UI-managed rules from
   `config.toml`.

Applying or clearing UI rules does not restart V2Ray/Xray. The sidecar picks up
valid config changes through hot reload. Country exits retain their actual
local proxy ports. Clearing UI rules retains the reusable exit; unused legacy
duplicates are removed only without routing references or active connections.

## Stats

Raw JSONL:

```bash
tail -f /var/lib/v2ray-egress-sidecar/stats.jsonl
```

Summaries:

```bash
python3 ./deploy/v2ray-sidecar-stats.py domain --since 1h --top 30
python3 ./deploy/v2ray-sidecar-stats.py service --since 1h --top 30
python3 ./deploy/v2ray-sidecar-stats.py outbound --since 24h
python3 ./deploy/v2ray-sidecar-stats.py errors --top 30 --sort errors
```

`avg_life` is average connection or UDP association lifetime, not ping latency.

The dashboard's `Top Services`, the CLI's `service` mode, and the routing
profiles use the same domain catalog: [`deploy/service-domains.json`](deploy/service-domains.json).
It includes 37 services, such as GitHub, X/Twitter, ChatGPT, Claude, YouTube,
Netflix, Telegram, Discord, Meta, Spotify, Steam, Apple, Microsoft, Slack,
Zoom, Dropbox, and Docker. For example, `github.com`, `githubusercontent.com`,
`githubassets.com`, `github.io`, and their subdomains all appear as `GitHub`.
Connections, bytes, errors, and average lifetime are combined per service and
outbound; `Top Domains` still shows the individual hostnames.

The most specific matching domain wins. Shared Google download domains such
as `gvt1.com` remain under `Google`, while identifiable YouTube and Google Play
hosts are grouped separately. Shared third-party CDNs and IP-only destinations
cannot reliably identify an app from a hostname alone.

Each catalog entry has `id`, `name`, and `domains`. Optional `stats_domains`
narrows classification when a routing profile includes shared dependencies;
optional `sources` links to provider documentation. Install the updated catalog
with `sudo ./deploy/install-ui.sh`. It is installed with its Python helper in
`/usr/local/lib/v2ray-egress-sidecar/`. After editing it, the next dashboard
refresh and CLI report use the new grouping. Restart only
`v2ray-egress-sidecar-admin-api.service` to refresh the routing profile list.
Existing routing rules are unchanged until you apply a profile.

Run the grouping tests with `python3 -m unittest discover -s tests -v`.

## Streaming Checks And Exit Reuse

Choose a streaming service and country in the routing page, then click the
streaming test button. Netflix, HBO Max, Disney+, Hulu, Prime Video, Paramount+,
Peacock, Apple TV+, Discovery+, Crunchyroll, Twitch, BBC/iPlayer, DAZN, Bilibili,
and YouTube are supported, along with TikTok, Reddit, X, Instagram, ChatGPT,
and Claude web probes. The common-services button shows TikTok, Disney+,
Netflix, YouTube, Prime Video, Reddit, ChatGPT, HBO Max, and Claude as columns,
with status, region, and method rows. Status is based on service-specific
evidence: Netflix title pages, BBC media selection, DAZN Startup, TikTok region
metadata, and OpenAI compliance/iOS endpoints are checked where supported.
An `(IP)` region uses IP geolocation, not a country returned by that service.
Tests run in one background queue, reuse that country's exit, and do not apply
routing rules. Netflix also accepts up to four title IDs or Netflix links and
reports public detail-page visibility.
Netflix redirects and current HTML headings are used as evidence; BBC/iPlayer
also probes its public media-selection endpoint for explicit geography errors.

Results separate IP geolocation, service-reported country, homepage access,
region errors, browser challenges, and public title visibility. For a successful
service-specific result, the detector repeats the reference project's three DNS
checks inside the selected Gluetun container and reports `Native`, `DNS`, or
`Unknown`. This is a heuristic for DNS unlocking, not proof of DRM playback.
Anonymous probes do not verify paid playback, account entitlements, DRM, or the
complete regional catalog. HTTP 200 alone is not proof of streaming access.
Methods reference [RegionRestrictionCheck](https://github.com/lmc999/RegionRestrictionCheck),
without bundling its script or account cookies. If the container has no usable
DNS tool, the method remains `Unknown` instead of being guessed. Requests use the selected
localhost HTTP CONNECT proxy, are limited to 128 KiB/s, and test TCP/IPv4;
the sidecar's direct UDP/QUIC path is outside this test.

The UI and CLI share a process lock to prevent duplicate country containers.
Existing exits retain their container ID and proxy port; stopped exits are
restarted. Only unreferenced exits without active TCP connections can be
removed for duplicate cleanup or cache capacity. A protocol mismatch is
reported instead of silently replacing a country's active exit. New exits
have a 128 MiB memory limit and use the installed Gluetun image. Configure
pool defaults with `SIDECAR_MAX_VPN_COUNTRIES=3`,
`SIDECAR_VPN_MIN_AVAILABLE_KB=163840`, and
`SIDECAR_EXIT_STATE_DIR=/var/lib/v2ray-egress-sidecar-exits`.

Command-line probes through an existing exit:

```bash
python3 ./deploy/sidecar_streaming.py netflix --proxy http://127.0.0.1:18888 --country "United States"
python3 ./deploy/sidecar_streaming.py hbo-max --proxy http://127.0.0.1:18888
```

After upgrading, restart only `v2ray-egress-sidecar-admin-api.service`.

