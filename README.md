# v2ray-egress-sidecar

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

Switching country usually means recreating the Gluetun container on the same
local proxy port. It does not require a V2Ray/Xray restart, and the sidecar only
needs config hot reload if the outbound tag or rules change.

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

---

# 中文说明

`v2ray-egress-sidecar` 是一个接在 V2Ray/Xray 出口后面的 SOCKS5 sidecar，用来做：

- 按域名/服务统计流量和连接数。
- 根据域名规则选择出口。
- 将部分 TCP 流量转发到 SOCKS5 或 HTTP CONNECT 代理，例如 Gluetun/NordVPN。
- 热加载配置，不需要为了普通路由变更重启 V2Ray/Xray。

它不是 V2Ray 的重写，也不是官方 V2Ray/Xray 项目的一部分。典型链路是：

```text
客户端 -> V2Ray/Xray -> v2ray-egress-sidecar -> 直连 / SOCKS5 / HTTP CONNECT
```

## 功能

- 提供 SOCKS5 TCP `CONNECT` 入站，供 V2Ray/Xray 连接。
- 支持 SOCKS5 `UDP ASSOCIATE`，UDP 当前以直连方式转发。
- 输出 JSONL 统计：目标域名、出口、连接数、上传/下载字节、错误、连接存活时间。
- 定时检查配置文件并热加载。
- 出口类型支持 `direct`、`socks5`、`http_connect`。
- 域名规则支持精确匹配、后缀匹配、包含匹配。
- 支持最大连接数、TCP idle 清理、半关闭清理、UDP idle 清理。
- 附带 systemd 安装脚本、统计脚本、Gluetun/NordVPN 辅助脚本。

## 限制

- sidecar 不解密 TLS。它只能看到 V2Ray/Xray 传给 SOCKS 的目标域名。如果上游只传 IP，就不能可靠还原真实域名。
- `http_connect` 出口只能承载 TCP，不能承载 UDP。
- UDP 当前是 direct relay。要让 UDP 也走 VPN，需要把 sidecar 放进 VPN 网络命名空间，或新增 UDP-capable 出口。
- 当前版本还没有实现 QUIC Initial / TLS ClientHello SNI 解析。

## 编译

```bash
git clone https://github.com/d6blju/v2ray-egress-sidecar.git
cd v2ray-egress-sidecar
cargo build --release
```

编译产物：

```text
target/release/v2ray-egress-sidecar
```

## 安装

在项目根目录执行：

```bash
sudo ./deploy/install.sh
```

安装后路径：

- 程序：`/usr/local/bin/v2ray-egress-sidecar`
- 配置：`/etc/v2ray-egress-sidecar/config.toml`
- 统计：`/var/lib/v2ray-egress-sidecar/stats.jsonl`
- 服务：`v2ray-egress-sidecar.service`

手动安装：

```bash
sudo install -d /etc/v2ray-egress-sidecar /var/lib/v2ray-egress-sidecar
sudo install -m 0755 target/release/v2ray-egress-sidecar /usr/local/bin/v2ray-egress-sidecar
sudo install -m 0644 config.example.toml /etc/v2ray-egress-sidecar/config.toml
sudo install -m 0644 systemd/v2ray-egress-sidecar.service /etc/systemd/system/v2ray-egress-sidecar.service
sudo systemctl daemon-reload
sudo systemctl enable --now v2ray-egress-sidecar.service
```

查看状态：

```bash
systemctl status v2ray-egress-sidecar.service
journalctl -u v2ray-egress-sidecar.service -f
```

## 配置

示例：

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
```

常用字段：

- `listen`：SOCKS5 监听地址。修改监听地址需要重启 sidecar。
- `stats_path`：统计 JSONL 文件路径。
- `reload_interval_secs`：配置文件检查间隔。
- `max_connections`：最大活跃连接数，`0` 表示不限制。
- `tcp_idle_timeout_secs`：TCP 空闲超时。
- `tcp_half_close_timeout_secs`：单边关闭后的等待时间。
- `udp_idle_timeout_secs`：UDP association 空闲超时。

规则按顺序匹配。没有命中规则时，使用配置里的第一个 outbound 作为默认出口。

普通路由和出口配置会热加载；错误配置会被拒绝，继续使用上一份有效配置。

## 接入 V2Ray/Xray

在 V2Ray/Xray 里新增一个 SOCKS outbound：

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

再把需要统计或需要选择出口的域名路由到这个 outbound：

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

压测确认没问题后，也可以把 V2Ray/Xray 的默认出口指向 sidecar。

## NordVPN/Gluetun 出口

建议不要在宿主机直接运行 `nordvpn connect`，否则可能改变服务器默认路由。推荐用 Gluetun 容器暴露本机 HTTP CONNECT 代理：

```bash
sudo install -d -m 0700 /etc/gluetun
sudo tee /etc/gluetun/nordvpn.env >/dev/null <<'EOF'
OPENVPN_USER=你的 manual setup 用户名
OPENVPN_PASSWORD=你的 manual setup 密码
EOF
sudo chmod 0600 /etc/gluetun/nordvpn.env
```

启动某个国家出口：

```bash
sudo ./deploy/nordvpn-gluetun-start.sh ph "Philippines" 18888
```

在 sidecar 配置里使用：

```toml
[[outbounds]]
tag = "nord-ph"
kind = "http_connect"
addr = "127.0.0.1:18888"
```

切换国家通常是重建同一个本地端口后的 Gluetun 容器；如果 sidecar 里的规则和端口不变，不需要重启 V2Ray/Xray。

## 查看统计

原始日志：

```bash
tail -f /var/lib/v2ray-egress-sidecar/stats.jsonl
```

聚合统计：

```bash
python3 ./deploy/v2ray-sidecar-stats.py domain --since 1h --top 30
python3 ./deploy/v2ray-sidecar-stats.py service --since 1h --top 30
python3 ./deploy/v2ray-sidecar-stats.py outbound --since 24h
python3 ./deploy/v2ray-sidecar-stats.py errors --top 30 --sort errors
```

`avg_life` 表示平均连接/UDP association 存活时间，不是 ping 延迟。
