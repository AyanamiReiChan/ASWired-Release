# ASWired Release

ASWired **v1.0.0** 部署仓库，包含网站、Go 主控、管理 Agent，以及我们修改的 **Komari 1.2.5-fix2-aswired.1.0.0**。这里保存部署脚本、配置和文档；编译好的程序在 [Releases](https://github.com/AyanamiReiChan/ASWired-Release/releases) 下载，无需在服务器安装 Go、npm 或编译源码。

**没有默认管理员账户或密码。首次访问 ASWired 网页，由你自己设置用户名和密码。** 安装脚本只生成内部服务密钥，不创建用户。首次设置还需要服务器本地的 `setup-token`，用于防止他人抢先初始化。

源码全部公开：[网站](https://github.com/AyanamiReiChan/ASWired) · [主控](https://github.com/AyanamiReiChan/ASWired-Server) · [Agent](https://github.com/AyanamiReiChan/ASWired-Agent) · [Komari fork](https://github.com/AyanamiReiChan/komari)。具体源提交见 [SOURCES.json](SOURCES.json) 和每个部署包的 `BUILD-INFO.json`。

## 1. 部署结构与要求

默认在同一台主控服务器部署 ASWired 和 Komari。两者通过回环网络通信，监控数据不绕行公网。

| 组件 | 本机监听 | 用途 |
| --- | --- | --- |
| ASWired 网站 | 127.0.0.1:3000 | 登录与工作区，默认毛玻璃主题 |
| ASWired 主控 | 127.0.0.1:12889 | 账户、订阅、任务、管理 API、Agent 通信 |
| 修改版 Komari | 127.0.0.1:25774 | 主机监控、历史数据、Komari 后台 |
| Nginx | 80 / 443 | 两个域名的 HTTPS 与 WebSocket 反向代理 |

推荐全新的 Debian 12/13 或 Ubuntu 22.04/24.04，Linux amd64（x86_64）或 arm64（aarch64），运行 systemd，至少 2 GB 内存和 3 GB 可用磁盘。部署包携带 Node.js 24 运行时，Komari 静态编译 SQLite。Alpine、OpenWrt、Windows、macOS 不适用本组合安装器。源码可支持的其他平台不等同于本发行版已经提供安装包。

准备两个不同域名，例如 `panel.example.com` 与 `probe.example.com`。将 A 记录指向主控 IPv4；只有服务器实际有可用 IPv6 时才添加 AAAA。开放主控 TCP 80、443 和你的 SSH 端口，**不要开放 3000、12889、25774**。使用 CDN 时必须支持 WebSocket，禁止缓存 `/api/`、`/auth/`、`/mcp` 和订阅响应；首次安装建议先使用直接 DNS。

在主控服务器安装基础工具：

```bash
sudo apt update
sudo apt install -y ca-certificates curl tar openssl python3 nginx certbot python3-certbot-nginx libstdc++6 libgcc-s1
sudo systemctl enable --now nginx
```

如果已有 ASWired、Komari、同名 systemd 服务或数据目录，安装器会拒绝覆盖。现有部署请先阅读 [升级、备份与迁移](docs/UPGRADE.md)。

## 2. 下载与校验部署包

以下命令需要普通 shell，不要在浏览器控制台运行。所有下载固定到明确版本，先校验再执行。

```bash
version=v1.0.0
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo '不支持的架构'; exit 1 ;;
esac
mkdir -p "$HOME/aswired-install-$version"
cd "$HOME/aswired-install-$version"
base="https://github.com/AyanamiReiChan/ASWired-Release/releases/download/$version"
asset="aswired_${version}_linux_${arch}.tar.gz"
curl -fL --proto '=https' --proto-redir '=https' "$base/$asset" -o "$asset"
curl -fL --proto '=https' --proto-redir '=https' "$base/SHA256SUMS" -o SHA256SUMS
awk -v file="$asset" '$2 == file {print}' SHA256SUMS > SELECTED-SHA256SUMS
test "$(wc -l < SELECTED-SHA256SUMS)" -eq 1
sha256sum -c SELECTED-SHA256SUMS
tar -xzf "$asset"
cd aswired
```

校验必须显示 `OK`。下载失败、清单没有对应文件或校验不一致时停止，不使用空文件或其他版本的摘要。完整包含两种架构的 ASWired Agent，主控可以给两种服务器生成安装命令。

Release 中另有主控、Komari、Agent、测速端和维护工具的单独制品，供高级运维使用。普通首次安装下载上面的 `aswired_...tar.gz` 即可。GitHub 自动生成的 “Source code” 压缩包不是部署包。

## 3. 安装服务与 HTTPS

先查看脚本，然后把下面的域名换成自己的真实域名：

```bash
less install.sh
sudo bash install.sh panel.example.com probe.example.com
sudo certbot --nginx -d panel.example.com -d probe.example.com
```

按 Certbot 提示提供你自己的邮箱并完成 HTTPS 配置。公网 DNS 与 TCP 80 必须可达；若使用 DNS 验证或已有证书，也可自行给生成的两个 Nginx server 配置证书、443 和 HTTP 跳转。**完成有效 HTTPS 后再输入账户密码。**

检查服务：

```bash
sudo systemctl status aswired-server aswired-web komari nginx --no-pager
curl -fsS http://127.0.0.1:12889/healthz
curl -fsS http://127.0.0.1:25774/api/version
sudo nginx -t
sudo certbot renew --dry-run
```

主控健康检查应返回 `status=ok`、`version=v1.0.0`；Komari 版本应为 `1.2.5-fix2-aswired.1.0.0`。安装器不会修改系统防火墙、覆盖已有数据或生成管理员账户。

## 4. 首次设置管理员

1. 在服务器本地读取初始化令牌：

   ```bash
   sudo cat /var/lib/aswired/setup-token
   ```

2. 打开 `https://panel.example.com`。空数据库会显示初始化界面。
3. 自行填写管理员用户名、密码和确认密码，并填入上一步的初始化令牌。密码要求 **12–72 个 UTF-8 字节**，请使用密码管理器生成和保存。
4. 完成后登录工作区。初始化只能成功一次，重复初始化会被拒绝。
5. 在账户安全中配置 TOTP 或 Passkey，并保存恢复码。

`setup-token` 不是默认密码，不放进公开文档或截图。部署文件不需要填写 `ASWIRED_ADMIN_USERNAMES`；如以后使用这个可选白名单，它只能限制已存在的管理员，不能把普通成员变成管理员。

## 5. 配置 Komari 统一登录和监控

安装器已在主机本地生成并配置相同的随机桥接密钥：

| 文件 | 配置 |
| --- | --- |
| `/etc/aswired/server.env` | ASWired 公网地址、Komari 公网地址、桥接密钥 |
| `/etc/aswired/komari.env` | 本机身份服务地址、ASWired 登录地址、Komari 公网地址、相同桥接密钥 |
| `/etc/aswired/web.env` | 网站 ORIGIN 和本机监听 |

文件权限为 root-only。整合模式关闭 Komari 独立密码、SSO 和本地账户创建，不会在日志里生成一套默认管理员密码。

首次进入 Komari 后台：

1. 用刚创建的 ASWired 管理员打开「用户管理 → 新增用户」。
2. 「账户系统」选择 **Komari 探针**，自行设置该账户的用户名和密码。它在 Komari 内拥有管理权限，但不拥有 ASWired 管理权限。
3. 使用另一个浏览器配置文件或无痕窗口，访问 ASWired 登录页，用 Komari 类型账户登录；系统通过一次性票据跳转到 `https://probe.example.com/admin`。
4. 在 Komari 的设置/API 密钥页面创建一个供主控读取数据的 API key，按密码保管。
5. 回到 ASWired 管理员工作区，在「设置 → 探针监控」填写 Komari 地址 **`http://127.0.0.1:25774`** 和该 API key，保存后测试连接/同步。
6. 在 Komari 新建监控服务器并按页面生成的命令安装 Komari Agent。等待节点在线。
7. 在 ASWired 对应服务器的探针绑定设置中，选择该 Komari 节点 UUID。主控才会把 CPU、内存、网卡流量与监控历史显示到这台服务器。

Komari API key 与统一登录的桥接密钥是两个不同的凭据，不要混填。Nginx 默认阻止公网访问 `/api/internal/komari/`；身份服务在同机回环地址上使用。

**ASWired Agent 与 Komari Agent 都要安装，职责不同。** 前者负责 Xray 配置、账户、计量和运维任务；后者负责主机监控。我们移除了 ASWired 自有主机指标采集，不用 Komari 的网卡流量代替套餐计费。

## 6. 接入代理服务器

1. 在 ASWired「服务器」页面新增服务器，填写名称和节点的 IP/域名。
2. 选择连接模式。默认优先 WebSocket；也支持 Pull 轮询、HTTP 直连和自动回退。
3. 复制页面生成的 Linux 安装命令，到对应节点以 root 执行。命令含节点凭据和短时安装票据，**不要公开分享**。
4. 安装脚本根据 CPU 架构选择本发行版 Agent、验证 SHA256 和配置，再安装服务。返回主控确认 Agent 实际在线。
5. 在该服务器的 Xray 配置弹窗创建入站，配置协议、端口、TLS/REALITY 等参数，并发布。检查任务完成状态。
6. 创建套餐、用户和套餐订阅，确认节点属于套餐范围，再获取客户端订阅或在「生成订阅」创建所需格式。

WebSocket/Pull 只需要节点主动连接主控 HTTPS；无需对公网开放管理监听。HTTP/自动模式的 Agent 管理端口默认 23889，应只允许主控 IP 访问。代理入站端口按你创建的 TCP/UDP 协议单独放行。

可管理协议：VLESS、VMess、Trojan、传统 Shadowsocks、Hysteria2、SOCKS5、HTTP，以及配置固定版本 Mihomo 后的 AnyTLS/Snell。协议可导入不代表可创建相应服务端。TLS 协议需要先把证书和私钥部署到节点。Snell 每端口最多一个活跃订阅；AnyTLS/Snell 当前仅 TCP，不能执行原客户端 IP 限制；SOCKS 管理入站不开放未认证 UDP。TUIC、Hysteria1、SS2022 动态用户和按用户 WireGuard 配置不属于本版可管理范围。完整限制见 [协议说明](https://github.com/AyanamiReiChan/ASWired-Server/blob/main/docs/managed-protocols.md)。

## 7. 流量与日志原则

- Agent 日志仅在点击同步/拉取按钮后读取，默认 **100 行**，不自动下载整个日志文件。
- 版本检查由管理员手动触发，主控读取 GitHub 的小型 Release 元数据和校验清单；不让 Agent 定时查询 GitHub。
- 只有下发升级时，对应 Agent 才下载所选架构的程序。任务成功与升级后重新认证的最终状态分别展示。
- Komari 数据通过同机回环读取，历史保存在 Komari 数据库。监控 Agent 的上报周期和历史保留在 Komari 内设置；管理 Agent 仍有必要的心跳、状态、计量和任务通信。
- 日志与任务失败信息以实际结果为准。应用业务日志在数据目录中，systemd 启动/退出信息可通过 journalctl 排查。

## 8. 升级、备份与常见问题

升级使用 [docs/UPGRADE.md](docs/UPGRADE.md)。主控、网站与修改版 Komari 随组合包一起更新；已有节点的 Agent 在页面手动升级，避免无意中断全部代理连接。

| 现象 | 检查 |
| --- | --- |
| 网页 502 | `systemctl status aswired-web aswired-server`，再看 `journalctl -u aswired-web -u aswired-server -n 100 --no-pager` |
| Komari 跳转失败 | 两个域名 HTTPS、两份桥接密钥一致、Komari 账户类型正确；检查两个服务日志 |
| 首次页面变成普通登录 | 数据库里已有账户，不是新安装；不要删除数据来重置密码 |
| setup-token 不存在 | 主控是否成功启动、数据目录权限、环境文件路径 |
| Agent 离线 | 节点时钟、DNS、主控证书、WebSocket 代理；`journalctl -u aswired-agent -n 100 --no-pager` |
| 看不到 CPU/内存 | Komari Agent 在线、主控 API key 有效、UUID 绑定正确；仅 ASWired Agent 在线不产生主机指标 |
| GitHub 版本检查失败 | 主控访问 `api.github.com` 的网络及匿名请求配额；无需把 GitHub token 写入前端 |
| 新节点提示没有安装包 | `/var/lib/aswired/agent-releases/linux-amd64/aswired-agent` 与 arm64 对应文件是否存在且主控可读 |
| TLS 证书签发失败 | DNS/CAA、AAAA、80 端口和 ACME 限额；节点入站证书与网站 HTTPS 证书分开管理 |

不要将包含口令、订阅令牌或服务器安装命令的完整日志直接贴进公开 issue。完整第三方许可和对应源码说明见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
