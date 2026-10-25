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

- Session-based management login, salted password hashes, CSRF protection,
  persistent login throttling, and logout/session revocation. Existing Nginx
  Basic Auth credentials can migrate without changing the account or password.
  See [authentication and safe upgrades](docs/authentication.md).

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
http://127.0.0.1:18082/api/health
```

Use nginx to expose it. Start with the example:

```bash
sudo cp /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf.example /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf
sudo htpasswd -cB /etc/nginx/.htpasswd-v2ray-egress-sidecar sidecar
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

Before exposing the UI, configure `SIDECAR_HTPASSWD_FILE` to the existing Nginx
password file, `SIDECAR_COOKIE_SECURE=1` and `SIDECAR_COOKIE_PATH=/sidecar-stats/`
in the API service environment. Install the static session gate with
`sidecar_nginx_auth.py`, validate with `nginx -t`, then gracefully reload Nginx.
Use `/api/health` for readiness; all other management data requires a session.
Sessions expire after 30 minutes idle / 12 hours total and survive API restarts.
Changing a password revokes its sessions. Never commit auth state, password files,
session databases, keys or server backups. Full installation, migration and
automation examples: [Management Authentication](docs/authentication.md).

Using the routing control page:

1. Choose a site/app profile or enter custom domain suffixes.
2. Choose a NordVPN country. The country search accepts Chinese names, English
   names, and country codes when the browser supports region names.
3. Choose OpenVPN UDP or TCP.
4. Click `保存此服务路由` (save this service route). Each profile has its own saved
   route; saving Binance after Polymarket keeps both. Services using the same
   country and VPN protocol reuse an exit.
5. Click `Check ip.sb` to see the current sidecar exit IP, country, country code,
   and city when available. A fallback looks up the measured IP and labels
   its source, rather than assuming different domains share an exit.
6. Use a saved service's `编辑` (edit) button to load its country, protocol and
   domains. The profile is locked while editing. Saving replaces that service's
   complete domain list, so removing a domain from the text box removes its rule.
   `取消编辑` cancels the form changes without saving.
7. Use `删除` (delete) beside a saved service to remove only its route. Use
   `清空全部服务路由` to remove all UI-managed routes. Manual rules are preserved.

`Custom` is one saved profile, not an additional unnamed route on every save.
An exact domain already owned by another saved service is rejected. Overlapping
suffixes assigned to different exits are also rejected; saving never takes a
domain away from another service.

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
It includes services such as GitHub, X/Twitter, ChatGPT, Claude, YouTube,
Netflix, Binance, Polymarket, Telegram, Discord, Meta, Spotify, Steam, Apple, Microsoft, Slack,
Zoom, Dropbox, and Docker. For example, `github.com`, `githubusercontent.com`,
`githubassets.com`, `github.io`, and their subdomains all appear as `GitHub`.
Connections, bytes, errors, and average lifetime are combined per service and
outbound; `Top Domains` still shows the individual hostnames.

The Polymarket preset uses the `polymarket.com` suffix, covering its API and other
subdomains. Its connectivity check probes the homepage; it does not verify login
or trading. Third-party login, wallet, customer-support and analytics domains are
not automatically included in that suffix.

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
Run the authentication and route-editor client regressions with
`node tests/test_auth_client.js` and `node tests/test_control_client.js`.

## Route Transactions And Rollback

Apply and Clear now validate the existing and candidate configurations, verify
the selected exit's HTTPS connectivity/country, and wait for a successful hot
reload from the same sidecar PID before committing UI state. A post-publication
failure restores the old configuration/state and verifies the rollback. A private,
fsynced journal recovers interrupted operations on API startup. Detected external
edits are preserved and block further changes until reconciled.

The page shows pending recovery, config/state agreement and the last transaction;
the private state directory also retains the previous failure.
Selected-exit checks and `ip.sb` route checks are displayed separately. Existing
connections retain their exit; new connections can briefly use a candidate before
a failed postcheck rolls it back. V2Ray and the forwarding sidecar keep running.

Build with `cargo build --release` before `sudo bash deploy/install-ui.sh`, then
restart only `v2ray-egress-sidecar-admin-api.service`. The new `--check` mode runs
configuration validation without opening a listener. Older servers can install
it as a separate checker without replacing their running Rust binary.
See [transaction behavior, recovery, upgrade settings and tests](docs/route-transactions.md).

## Incremental Statistics And CPU Diagnostics

The dashboard now imports only appended JSONL records into SQLite, retains 24-hour
statistics, rotates consumed logs at 64 MiB, and pauses under host resource pressure.
A lightweight continuous observer captures real V2Ray CPU spikes and generates
private evidence reports plus a dashboard summary. It never restarts V2Ray.

| Component | Default behavior |
| --- | --- |
| Dashboard | Refresh every 60 seconds; read up to 4 MiB of new logs per run |
| Resource limits | Renderer: 5% of one CPU / 256 MiB; observer: 2% / 32 MiB |
| Statistics | Last 24 hours, grouped by domain, service, outbound and protocol |
| Rotation | Rename consumed logs at 64 MiB; delete consumed archives after three days |
| Pressure guard | Pause at CPU >=80%, available memory <20%, or >=2048 established host TCP sockets |
| CPU incidents | Sample every second; >=80% triggers history plus five further samples; five-minute cooldown |
| Reports | On capture, keep up to 20 archives and prune those older than three days; retain the latest report and publish only its summary |

From the repository root, install or upgrade the UI and observer with:

```bash
sudo bash deploy/install-ui.sh
sudo systemctl restart v2ray-cpu-diagnose.service
sudo systemctl status v2ray-cpu-diagnose.service
sudo cat /var/log/v2ray-cpu-diagnostics/status.json
sudo cat /var/log/v2ray-cpu-diagnostics/latest.txt
```

`latest.txt` and `latest.json` appear after the first CPU incident. `status.json`
shows the observer heartbeat and latest CPU sample even when no incident occurred.
The dashboard's CPU section separates current monitoring state, CPU percentage,
threshold and PID from a collapsed historical incident. Labels and explanations
are in Chinese and timestamps use Beijing time. A historical CPU peak is not a
current fault; 100% CPU is approximately one logical core. An expired heartbeat
is shown as stale rather than presenting the old sample as current.
Initial import may take several batches; the page shows `catching up` and backlog
bytes until it finishes. Pressure pauses preserve the last successful statistics.
The installer disables the old diagnostic timer. Restart only the observer to load
an upgrade; V2Ray/Xray and the Rust forwarding service do not need restarting.

Reports distinguish measured evidence from hypotheses. CPU without large host
traffic can suggest several causes; identifying the exact Go function still needs
a CPU profile. Completed-session domain counters cannot identify all active TLS
connections or prove a connection leak from a single snapshot.
See [deployment, limits, diagnosis and rollback](docs/observability.md), including
the alternate paths used by older installations. Do not commit the database,
raw logs, incident reports, credentials or local server configuration.

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
reported instead of silently replacing a country's active exit. New and
reused managed exits are capped at 128 MiB memory, 0.2 CPU on a single-core
host, and 128 processes. New exits are admitted only when the projected host
CPU stays below 80% and at least 320 MiB remains available; the defaults also
allow at most three countries. Configure the pool with
`SIDECAR_MAX_VPN_COUNTRIES=3`, `SIDECAR_VPN_MIN_AVAILABLE_KB=327680`,
`SIDECAR_MAX_HOST_CPU_PERCENT=80`, `SIDECAR_HOST_CPU_RESERVE_PERCENT=20`,
`SIDECAR_GLUETUN_MEMORY=128m`, `SIDECAR_GLUETUN_PIDS=128`, and
`SIDECAR_EXIT_STATE_DIR=/var/lib/v2ray-egress-sidecar-exits`.

Command-line probes through an existing exit:

```bash
python3 ./deploy/sidecar_streaming.py netflix --proxy http://127.0.0.1:18888 --country "United States"
python3 ./deploy/sidecar_streaming.py hbo-max --proxy http://127.0.0.1:18888
```

After upgrading, restart only `v2ray-egress-sidecar-admin-api.service`.

