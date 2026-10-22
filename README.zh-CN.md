# v2ray-egress-sidecar

[English](README.md) | [简体中文](README.zh-CN.md)

## 管理页面安全

管理 API 已改为真实登录和服务端 Session，写操作（包括登录、退出）同时校验
CSRF token、JSON 类型及请求来源，不再接受 `X-Sidecar-Admin` 作为认证。
密码使用独立随机盐的 PBKDF2-HMAC-SHA256（600,000 次迭代），登录限流持久化。
现有 Nginx Basic Auth 账号密码可以保持不变，首次验证后自动迁移密码验证信息；
退出后需显式重新登录，不会悄悄利用浏览器缓存的 Basic Auth 自动登录。

Session 默认闲置 30 分钟、最长 12 小时，登录后轮换 token，退出或修改密码立即
撤销相关会话。Cookie 使用 HttpOnly、Secure、SameSite=Strict；CSRF token
只保存在页面内存。会话数据库和 CSRF 密钥持久化，管理 API 升级后无需全部重新登录。
Nginx 使用 `auth_request` 同时保护页面和 `stats.json`，不是仅用前端跳转。

安装后请为 API 配置 `SIDECAR_HTPASSWD_FILE`（现有 Nginx 密码文件）、
`SIDECAR_COOKIE_SECURE=1`、`SIDECAR_COOKIE_PATH=/sidecar-stats/`，保持 API 仅监听
回环地址。只在独立 HTTP 测试环境关闭 Secure Cookie。新增的登录页面和 `auth.js`
由 `deploy/install-ui.sh` 一并安装。健康检查改用 `/api/health`，其他管理接口需要登录。

完整步骤、交互式密码设置命令、Nginx Session 校验和自动化请求流程见
[认证部署说明](docs/authentication.md)。`deploy/deploy-auth-existing.sh` 仅适用于
脚本中列明的旧路径安装：先备份，保留密码文件和转发配置，失败回滚；只重启管理 API、
优雅重载 Nginx，不重启 V2Ray 或 sidecar。其他安装请先检查路径再升级。
认证数据、密码文件、密钥、Session 数据库及服务器备份均不得上传 GitHub。

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

辅助脚本返回 JSON，包含实际 `proxy` 地址和 `container` 名称。配置时请使用返回的
地址：同国家已有出口会直接复用，端口可能与命令里的首选端口不同。切换国家会
复用对应国家的缓存出口，或建立不同国家的出口。更改已有国家的 OpenVPN 协议时，
需要先释放其闲置出口。普通路由变更不需要重启 V2Ray/Xray。

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
http://127.0.0.1:18082/api/health
```

用 nginx 暴露界面。可以从示例配置开始：

```bash
sudo cp /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf.example /etc/nginx/conf.d/v2ray-egress-sidecar-ui.conf
sudo htpasswd -cB /etc/nginx/.htpasswd-v2ray-egress-sidecar sidecar
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
5. 点击 `Check ip.sb` 查看当前 sidecar 出口 IP、中文国家、国家代码，以及可用的城市。
   地区查询失败时，会按实测 IP 使用备用查询并标明来源，不会假定其他域名也走相同出口。
6. 点击 `Clear UI Rules` 只删除 UI 管理的规则，不删除手写规则。

应用或清理 UI 规则不会重启 V2Ray/Xray。sidecar 会热加载有效配置，UI 写入复用
出口的实际本机端口。清理规则后保留可复用容器。同国家的旧重复容器，只有在
没有路由引用和活跃 TCP 连接时才会清理。

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

统计页面的 `Top Services`、命令行的 `service` 模式和路由界面的应用列表共用
[`deploy/service-domains.json`](deploy/service-domains.json) 域名表。当前包含 38 个服务，
包括 GitHub、X/Twitter、ChatGPT、Claude、YouTube、Netflix、Binance、Telegram、Discord、
Meta、Spotify、Steam、Apple、Microsoft、Slack、Zoom、Dropbox 和 Docker 等。
例如 `github.com`、`githubusercontent.com`、`githubassets.com`、`github.io`
及其子域名统一显示为 `GitHub`。同一服务、同一出口的连接数、字节、错误和平均
存活时间合并统计；`Top Domains` 仍保留各个域名。

匹配时优先使用更具体的域名。`gvt1.com` 等 Google 共用下载域名归到 `Google`，
能够明确识别的 YouTube 和 Google Play 域名单独归类。共享第三方 CDN 和只有 IP
的连接，不能仅凭目标地址可靠识别具体应用。

域名表的每项包含 `id`、`name` 和 `domains`。可选的 `stats_domains` 用于缩小
统计匹配范围，避免把路由列表中的共享依赖误判为某个应用；可选的 `sources`
记录服务商文档链接。执行 `sudo ./deploy/install-ui.sh` 安装更新后的域名表和
Python 公共模块，安装位置为 `/usr/local/lib/v2ray-egress-sidecar/`。
修改后，下一次统计页面刷新和命令行报表会使用新的分类；路由界面的应用列表
需要重启 `v2ray-egress-sidecar-admin-api.service` 才会更新。已有转发规则保持不变，
直到在界面上重新应用对应的 profile。

分类测试：`python3 -m unittest discover -s tests -v`。

## 路由事务与失败回滚

应用路由和清除界面规则现在使用同一套事务机制：校验旧配置及候选配置，实测所选出口的 HTTPS
连通性和国家，确认同一 sidecar 进程完成热加载后才提交界面状态。发布后的校验、连通性或状态写入
失败时恢复旧配置和状态，并确认回滚加载。私有事务日志会同步到磁盘，管理 API 重启后自动恢复中断操作；
发现外部编辑冲突时保留外部修改，暂停后续变更，等待核对。

页面显示待恢复事务、配置与已提交状态是否一致、最近一次事务及上次失败原因。
所选出口验证与 `ip.sb` 的路由检测分开展示，避免手动规则导致两者国家不同而混淆。
已有连接保持原出口；候选配置加载后、后置检测失败并回滚前，新连接可能短暂使用候选出口。
V2Ray 和转发 sidecar 始终保持运行。

升级时先运行 `cargo build --release`，再运行 `sudo bash deploy/install-ui.sh`，最后只重启
`v2ray-egress-sidecar-admin-api.service`。新增的 `--check` 模式只校验配置，不监听端口；
旧服务器可将它单独安装为校验器，无需替换正在运行的 Rust 二进制。
事务步骤、恢复方法、旧版部署参数及测试说明见 [路由事务运维文档](docs/route-transactions.md)。

## 增量统计与 CPU 诊断

统计页面通过 SQLite 保存日志读取位置，每次只读取新增记录，保留最近 24 小时的统计。
已消费的日志达到 64 MiB 后自动轮转，归档保留三天；资源紧张时暂停统计刷新并保留上次结果。
轻量常驻观察器持续记录真实的进程和线程 CPU 增量，超过阈值时自动生成私有报告，页面展示诊断摘要。
观察器不会重启 V2Ray。

| 组件 | 默认行为 |
| --- | --- |
| 统计页面 | 每 60 秒刷新，每次最多读取 4 MiB 新日志 |
| 资源限制 | 渲染器：单核 5% CPU / 256 MiB；观察器：2% CPU / 32 MiB |
| 统计范围 | 最近 24 小时，按域名、服务、出口和协议分组 |
| 日志轮转 | 已消费日志达到 64 MiB 后重命名；已消费归档三天后清理 |
| 压力保护 | CPU >=80%、可用内存 <20% 或主机已建立 TCP 套接字 >=2048 时暂停 |
| CPU 事件 | 每秒采样，达到 80% 时保留历史及后续五次采样，五分钟内最多一份报告 |
| 报告保留 | 生成报告时将归档清理至最多 20 份，并删除超过三天的归档；保留最新报告，页面仅公开摘要 |

在项目根目录执行以下命令安装或升级界面和观察器：

```bash
sudo bash deploy/install-ui.sh
sudo systemctl restart v2ray-cpu-diagnose.service
sudo systemctl status v2ray-cpu-diagnose.service
sudo cat /var/log/v2ray-cpu-diagnostics/status.json
sudo cat /var/log/v2ray-cpu-diagnostics/latest.txt
```

首次 CPU 事件发生后才会生成 `latest.txt` 和 `latest.json`；没有事件时仍可通过
`status.json` 查看心跳和最新 CPU 采样。首次导入可能分为多个批次，页面通过
`catching up` 和积压字节数显示进度；压力暂停期间保留上次成功的统计结果。
安装器会停用旧诊断定时器。升级后只需重启观察器，不需要重启 V2Ray/Xray 或 Rust 转发服务。

报告会区分已测量证据和推测。高 CPU 而主机流量较低可能对应多种原因，定位具体 Go 函数仍需要
CPU profile。已结束会话的域名统计无法识别所有活动 TLS 连接，单次快照也不能证明连接泄漏。
部署、资源限制、证据边界、旧版目录适配和回滚方法见 [运维说明](docs/observability.md)。
不要提交统计数据库、原始日志、事件报告、凭据或本机服务器配置。

## 流媒体检测与出口复用

在路由页选择服务和国家，点击“检测当前服务”。支持 Netflix、HBO Max、Disney+、
Hulu、Prime Video、Paramount+、Peacock、Apple TV+、Discovery+、Crunchyroll、Twitch、
BBC/iPlayer、DAZN、Bilibili 和 YouTube，以及 TikTok、Reddit、X、Instagram、ChatGPT
和 Claude 网页探测。点击“检测常用服务”，以 TikTok、Disney+、Netflix、YouTube、
Prime Video、Reddit、ChatGPT、HBO Max、Claude 为列，展示状态、地区和方式。
Netflix 片名页、BBC 媒体选择接口、DAZN Startup、TikTok region 字段以及
OpenAI 合规/iOS 接口会使用各自的专用证据。地区旁的 `(IP)` 表示 IP 数据库国家，
而不是服务返回的国家。检测通过后台单线程队列运行，复用国家出口，
不应用转发规则。Netflix 可输入最多四个片名 ID 或 Netflix 链接，检测公开详情页
是否可见。
Netflix 检测使用服务返回的地区跳转和当前 HTML 标题作为证据；BBC/iPlayer
还检测公开媒体选择接口中的明确地区错误。

结果分别展示 IP 数据库地区、服务识别地区、首页访问、地区限制、浏览器验证和
片名页面可见性。服务专用检测成功后，会在所选 Gluetun 容器内重复参考项目的
三项 DNS 检查，方式显示为“原生”“DNS”或“未知”。这只是 DNS 解锁启发式判断，
不是 DRM 播放证明。匿名检测不能确认订阅权限、DRM、真实播放或完整地区片库；
HTTP 200 不代表能播放。若容器没有可用 DNS 工具，方式保持“未知”，不会猜测。
检测方法参考
[RegionRestrictionCheck](https://github.com/lmc999/RegionRestrictionCheck)，项目没有
捆绑其脚本或账号 Cookie。请求通过所选本机 HTTP CONNECT 出口，限速 128 KiB/s，
测试 TCP/IPv4；sidecar 目前直出的 UDP/QUIC 不在检测范围内。

UI 和命令行共用跨进程锁，防止并发创建同国家容器。复用时保留容器 ID 和实际
端口；已停止的容器直接启动。去重或缓存容量清理，只删除没有路由引用和活跃
TCP 连接的闲置容器。协议不匹配会报错，不会自动替换正在使用的出口。新建或复用的
受管出口都限制为 128 MiB 内存、单核主机约 0.2 CPU 和 128 个进程。新建前会检查预计
主机 CPU 不超过 80%，并保留至少 320 MiB 可用内存；默认最多缓存 3 个国家。可通过
环境变量调整：`SIDECAR_MAX_VPN_COUNTRIES=3`、`SIDECAR_VPN_MIN_AVAILABLE_KB=327680`、
`SIDECAR_MAX_HOST_CPU_PERCENT=80`、`SIDECAR_HOST_CPU_RESERVE_PERCENT=20`、
`SIDECAR_GLUETUN_MEMORY=128m`、`SIDECAR_GLUETUN_PIDS=128`，以及
`SIDECAR_EXIT_STATE_DIR=/var/lib/v2ray-egress-sidecar-exits`。

通过现有出口执行命令行检测：

```bash
python3 ./deploy/sidecar_streaming.py netflix --proxy http://127.0.0.1:18888 --country "United States"
python3 ./deploy/sidecar_streaming.py hbo-max --proxy http://127.0.0.1:18888
```

升级后只重启管理 API：`sudo systemctl restart v2ray-egress-sidecar-admin-api.service`。
