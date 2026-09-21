# v1.0.0

- ASWired 网站、主控和管理 Agent 的首个公开组合发行版，Linux amd64 / arm64。
- 配套 Komari 1.2.5-fix2-aswired.1.0.0，统一身份库；首次管理员由部署者在网页自行设置。
- 默认毛玻璃主题；服务器、节点、用户、套餐、订阅生成与管理、模板、证书、转发及数据库管理。
- 管理 Agent 支持 WebSocket / HTTP / Pull / 自动回退，内嵌 Xray；主机监控仅由 Komari 提供。
- Agent 日志默认手动读取 100 行；GitHub Release 版本检查、固定版本下载、SHA256 校验和 Agent 回退监督。
- 仓库以审核后的干净历史首次公开；内部部署记录、真实部署地址及私有运行数据不进入发行版。

协议和部署限制见 README；源代码测试、Linux 构建、首次初始化和服务安装检查的结果以对应 GitHub Actions run 为准。
