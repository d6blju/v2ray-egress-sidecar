# v2ray-egress-sidecar

[English](README.md) | [简体中文](README.zh-CN.md)

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
- 可选 Web 界面，用于查看统计、修改路由、选择 NordVPN 国家、从最近流量里发现域名。

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

## Web 界面

可选 Web 界面分为两部分：

- 统计面板：读取 `stats.jsonl`，生成 `index.html` 和 `stats.json`。
- 路由控制页：向 `config.toml` 写入 UI 管理的规则块，启动或重建 Gluetun/NordVPN HTTP CONNECT 容器，并通过 `ip.sb` 检查出口 IP。

安装 sidecar 后，再安装界面：

```bash
sudo ./deploy/install-ui.sh
```

安装内容：

- 静态文件：`/var/www/v2ray-egress-sidecar-dashboard`
- 渲染器：`/usr/local/bin/v2ray-egress-dashboard-render`
- 管理 API：`/usr/local/bin/v2ray-egress-admin-api`
- 管理状态：`/var/lib/v2ray-egress-sidecar-admin`
- 服务：
  - `v2ray-egress-sidecar-dashboard-render.timer`
  - `v2ray-egress-sidecar-admin-api.service`

管理 API 只监听本机：

```text
http://127.0.0.1:18082/api/state
```

用 nginx 暴露界面。可以从示例配置开始：

```bash
sudo cp /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf.example /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf
sudo htpasswd -c /etc/nginx/.htpasswd-v2ray-egress-sidecar sidecar
sudo nginx -t
sudo systemctl reload nginx
```

示例配置会提供本机访问地址：

```text
http://127.0.0.1:18081/
```

同时也包含可以复制到现有 HTTPS server block 里的公网路径示例：

```text
https://your-domain.example/sidecar-stats/
https://your-domain.example/sidecar-stats/control.html
```

公网界面和 API 必须加 Basic Auth。不要在无认证的情况下暴露 `/sidecar-stats/api/`。

路由控制页用法：

1. 选择网站/应用 profile，或手动输入域名后缀。
2. 选择 NordVPN 国家。国家搜索支持中文、英文和国家代码，取决于浏览器是否支持地区名称。
3. 选择 OpenVPN UDP 或 TCP。
4. 点击 `Apply Route`。
5. 点击 `Check ip.sb` 查看当前 sidecar 出口 IP。
6. 点击 `Clear UI Rules` 只删除 UI 管理的规则，不删除手写规则。

应用或清理 UI 规则不会重启 V2Ray/Xray。sidecar 会通过热加载读取有效配置。切换 Gluetun 国家可能会重建同一 localhost 代理端口背后的 Gluetun 容器。

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
