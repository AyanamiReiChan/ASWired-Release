# 备份、升级与迁移

## 文件位置

| 路径 | 内容 |
| --- | --- |
| `/opt/aswired/releases/v1.0.2` | 只读版本目录，程序、网站、Node 运行时和脚本 |
| `/opt/aswired/current` | 当前版本软链接 |
| `/etc/aswired/*.env` | 主控、网站、Komari 配置及服务密钥，root-only |
| `/var/lib/aswired` | 主控数据库、加密密钥、身份密钥、日志、备份与 Agent 安装包 |
| `/var/lib/komari/data` | Komari 数据库、主题和监控历史 |
| `/var/lib/aswired-agent` | **节点上**的管理 Agent 状态、Xray 配置和日志 |

主控 `data-encryption.key`、`master-identity.key`、`jwt.key` 必须和数据库一同保留。数据库迁移功能还会使用 `database-config.key` 与 `database-active.enc`。丢失密钥不能靠重新安装恢复密文或 Agent 信任关系。

## 日常备份

优先在「设置 → 备份与恢复」创建并下载应用备份，离机加密保存。该备份含业务数据和恢复所需密钥，按密码材料保护，不上传 GitHub。另备份 Komari 数据、Nginx/证书和 `/etc/aswired`。

使用 SQLite 时，可停机做完整冷备份：

```bash
sudo systemctl stop aswired-web komari aswired-server
sudo install -d -m 0700 /var/backups/aswired/manual
sudo tar -czf /var/backups/aswired/manual/data.tar.gz -C /var/lib aswired komari
sudo tar -czf /var/backups/aswired/manual/config.tar.gz -C /etc aswired nginx letsencrypt
sudo systemctl start aswired-server komari aswired-web
```

不要仅复制正在写入的 `aswired.db` 而漏掉 WAL。备份只留在同一磁盘不能抵御磁盘损坏。安排保留策略并实际演练恢复。

## 从一个正式组合版本升级

在设置中手动检查版本并阅读发行说明。选择明确版本，不自动覆盖运行目录：

```bash
# 将 v1.0.2 换成实际已发布且准备升级到的版本。
sudo bash /opt/aswired/current/update.sh v1.0.2
```

升级前请确认目标版本已经正式发布。脚本从 **ASWired-Release** 下载固定版本的完整包和 SHA256SUMS，校验、解包并检查程序可启动后，停止服务生成一致备份，再切换版本目录并重新启动。原环境文件、账户、密钥与 HTTPS 配置保留。备份位于 `/var/backups/aswired/<UTC时间>/`。

更新脚本的自动数据备份排除主控的 `agent-releases/`、`backups/` 与 `logs/`，避免重复打包构建产物和历史备份；数据库、密钥与 Komari 数据包含在内。如需保留业务日志，另行备份。安装用 Agent 二进制更新为新版本，**不会自动升级已经运行的远端 Agent**。

从 v1.0.1 起，Komari 使用同一个 ASWired 管理员账户。将 `/etc/aswired/komari.env` 的 `ASWIRED_LOGIN_URL` 更新为主站的 `https://你的主站域名/komari`，重启 Komari，再从 ASWired 侧栏「Komari 管理」进入。旧独立 Komari 账户保留历史记录，但不再授予后台权限，也不会自动提升为管理员。

在「服务器 → 升级 Agent」选择一致架构的节点，点击“读取最新稳定版与校验值”，核对版本后下发。Agent 下载固定版本并校验 SHA256，升级监督进程等待新版本重新认证；90 秒内未成功认证时回退。手动刷新查看最终状态，不把任务受理视为升级成功。

不要用原版 Komari 的安装脚本、`latest` 镜像或上游二进制覆盖整合版。单独使用主控 CLI `upgrade` 只替换主控，不会同步网站和 Komari，组合部署应使用上面的 `update.sh`。

## PostgreSQL 升级

主控可以在「设置 → 数据库」从 SQLite 迁移到 PostgreSQL；Komari 仍使用自己的 SQLite。开始前阅读主控 [数据库说明](https://github.com/AyanamiReiChan/ASWired-Server/blob/main/docs/deployment.md)。

文件冷备份不能备份外部 PostgreSQL。先停止写入，使用受保护的 `PGPASSFILE` / pg_service.conf 和 `pg_dump --format=custom` 备份实际使用的数据库，验证 `pg_restore --list`，然后执行：

```bash
sudo bash /opt/aswired/current/update.sh v1.0.2 --database-backup /secure/path/aswired.dump
```

脚本会保留你指定的备份文件，但不会验证它是否来自正确数据库；管理员必须核对目标、时间与可恢复性。启用加密数据库配置时不要删除 `database-active.enc` 强行回 SQLite，否则会切回旧数据。

## 升级失败与回退

先停止服务。保留失败后的数据副本和日志，不把旧程序直接指向经过新版本迁移、兼容性未知的数据库。

1. 找到升级前备份，读取 `previous-release` 确认旧版本目录。
2. 将当前 `/var/lib/aswired`、`/var/lib/komari` 和 `/etc/aswired` 改名保留。
3. 将备份中的 `data.tar.gz` 解压回 `/var/lib`，`config.tar.gz` 解压回 `/etc`；还原服务账户所有权和秘密文件权限。
4. PostgreSQL 另恢复经过验证的数据库备份，再保留与之对应的加密连接配置。
5. 把 `/opt/aswired/current` 指向已确认的旧版本，重新安装该版本 `deploy/systemd/*.service`，执行 `systemctl daemon-reload`。
6. 从旧版本的 `agent-releases/` 恢复主控安装包目录，启动并检查登录、Komari、订阅和 Agent 通信。

还原旧数据库会丢弃备份后的变更，因此不要在故障后持续向系统写入。不要删除失败版本和备份，直到确认恢复完成。

## 迁移已有开发部署

本安装器只处理干净主机，避免误覆盖旧的独立服务。推荐新主机或隔离实例：

1. 在旧主控创建完整应用备份，冷备份旧 Komari、环境、证书和服务文件。
2. 在新主机按 README 安装，暂不切换正式 DNS。
3. 在隔离地址初始化管理员，从设置恢复应用备份；恢复后用备份中的账户登录。保留旧数据密钥和主控身份，否则旧 Agent 不信任新主控。
4. 停止新 Komari，把经过备份且版本相容的旧 Komari 数据复制到 `/var/lib/komari/data`，设为 komari 所有。只迁移数据，不覆盖整合版程序。
5. 使用已有 ASWired 管理员通过侧栏「Komari 管理」验证共享登录。独立 Komari 的本地密码账户和旧中央 Komari 类型账户不会自动变成管理员。
6. 核对两份桥接密钥、两个公网地址、主控监听和 Nginx。验证新主控、Komari、真实 Agent 和客户端订阅，再切换 DNS/反向代理。
7. 保留旧部署的停机快照供回退，确认稳定后再清理。

恢复或数据库迁移都需要独占主控，不支持多个进程共用同一 SQLite。不要通过删库来“解决”账户或密钥错误。

## 忘记管理员密码

在拥有主控数据目录权限的服务器本机操作，使用自己之前设置的用户名：

```bash
sudo systemctl stop aswired-server
read -r -s -p '新密码：' ASWIRED_NEW_PASSWORD
printf '\n'
printf '%s' "$ASWIRED_NEW_PASSWORD" | sudo -u aswired /opt/aswired/current/bin/aswired-server reset-password --data-dir /var/lib/aswired --username YOUR_USERNAME --password-stdin
unset ASWIRED_NEW_PASSWORD
sudo systemctl start aswired-server
```

密码不放进命令参数或日志。该操作只重置已有账户，并撤销旧会话，不创建默认管理员。若使用手动配置 PostgreSQL，还需为命令提供与服务相同的数据库环境；通过页面迁移保存的加密配置则由数据目录自动加载。同时遗失 TOTP/Passkey 时可显式增加 `--clear-mfa`，登录后重新配置。

## v1.0.2 行为变化

- Agent 首次启动时自动创建没有入站监听的基础 Xray 配置并启动核心；已有配置保持原样。升级 Agent 后，缺失配置的节点也会自动初始化。
- 「探针监控」直接打开 `ASWIRED_KOMARI_PUBLIC_URL` 指定的 Komari 首页；公开地址未配置时会显示提示，不使用内部采集地址。
- Agent 升级页支持全选符合条件的在线节点，并显示无法选择的原因。未启用 `-supervise` 的旧服务仍需先完成启动方式迁移。
- 新生成的 Komari Agent 安装命令默认使用 5 秒上报间隔。已有 Komari Agent 的运行参数不会随主控升级自动修改。
