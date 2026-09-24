# 默认限速与代理 IPv6 策略

v1.0.10 提供宽松、均衡、严格三套行为限速预设，并默认屏蔽受管代理业务的 IPv6 目标。两项设置分别位于「系统设置 → 行为限速」和「系统设置 → 代理网络」。

## 均衡默认与可选预设

全局行为限速未设置或为 null 时，使用均衡规则。已有明确配置对象保持原样，包括自定义规则、明确关闭、空对象和空规则；成员覆盖套餐，套餐覆盖全局。选择预设只显示预览，点击应用后才替换当前编辑器，保存后生效。

| 预设 | 连续下载达到或超过 | 连续时长 | 触发后下载上限 | 限速时长 |
| --- | ---: | ---: | ---: | ---: |
| 均衡（默认） | 200 Mbps | 2 分钟 | 50 Mbps | 10 分钟 |
| 均衡（默认） | 80 Mbps | 10 分钟 | 30 Mbps | 10 分钟 |
| 宽松 | 300 Mbps | 3 分钟 | 100 Mbps | 5 分钟 |
| 宽松 | 150 Mbps | 15 分钟 | 80 Mbps | 10 分钟 |
| 严格 | 100 Mbps | 2 分钟 | 30 Mbps | 10 分钟 |
| 严格 | 50 Mbps | 10 分钟 | 20 Mbps | 15 分钟 |

速度按同一成员在同一物理 Agent 上的有效下载采样计算，不是全站出口总速率。短时高速不会触发；低于阈值或采样间隔超过 15 秒后重新计时，处罚到期自动解除。长期规则优先级高于短期高速规则，其他套餐速度限制仍然生效，行为限速不会提高原本更低的上限。

三套预设均不使用窗口命中规则，默认不发送通知。高级编辑仍支持 burst，其 hits 代表有效采样命中次数，不是独立下载次数。可以选择「明确关闭」停用本层行为限速。

## 代理业务 IPv6

`blockProxyIPv6` 默认 true，只有明确保存 false 才关闭。保护针对客户端通过受管代理访问的目标，不修改服务器 sysctl、防火墙、SSH、主控通信或节点监听地址。

服务端保护在新版 Agent 中按入站和本次业务目标执行：拒绝 IPv6 字面目标，域名只选择 IPv4；仅有 AAAA 的业务域名会失败，不回退到 IPv6。中转节点的传输连接与用户访问目标分开处理，保留服务端 IPv6 中转和内部 DNS 连接。

完整 Clash/Mihomo、sing-box、Egern 配置在模板、规则覆写和脚本执行后应用 IPv6 限制，避免模板意外重新打开。订阅内的节点地址不被删除或改写。客户端禁用 AAAA 后，仅有 AAAA 记录的节点域名可能无法解析，应使用可解析的节点地址。

v2ray、Shadowrocket 的 Base64 URI 订阅只能携带节点参数，不能携带全局 DNS 或路由开关，依赖受管服务端保护。外部节点不由本主控控制，其服务端行为不能由该开关保证；完整客户端配置提供客户端侧约束。Stash、Surge、Loon 等格式仍遵循原有节点协议兼容性限制，本次没有新增客户端协议支持。

## 已有部署的生效顺序

1. 自行升级主控组合版本至 v1.0.10。
2. 将受管节点的管理 Agent 升级至 v1.0.10 或具备 `proxy_ipv6_guard` 能力的更新版本，确认重新连接成功。
3. 对已有节点重新应用配置，等待任务成功；旧 Agent 缺少能力时拒绝受保护配置下发，不能把已排队当作已生效。
4. 更新客户端订阅。新连接使用新的策略。

主控自更新不会自动升级远端 Agent，也不会在启动时覆盖其已有配置。原始配置下发及历史配置恢复同样遵循本次明确的 IPv6 开关。行为限速预设沿用既有执行接口，无需为限速单独升级 Agent。

## 客户端配置依据

- [Mihomo 全局 IPv6](https://wiki.metacubex.one/en/config/general/#ipv6)、[DNS IPv6](https://wiki.metacubex.one/en/config/dns/#ipv6)、[路由规则](https://wiki.metacubex.one/en/config/rules/)。
- [sing-box DNS](https://sing-box.sagernet.org/configuration/dns/)、[DNS 规则动作](https://sing-box.sagernet.org/configuration/dns/rule_action/)、[路由规则动作](https://sing-box.sagernet.org/configuration/route/rule_action/)。
- [Egern 配置示例](https://egernapp.com/docs/configuration/example/)、[DNS](https://egernapp.com/docs/configuration/dns/)、[规则](https://egernapp.com/docs/configuration/rules/)。
