# 第三方组件与源码

部署包包含编译产物与必要的运行时，源码分别在以下公共仓库公开。

| 组件 | 许可证 | 对应源码 |
| --- | --- | --- |
| ASWired 网站、主控与 Agent 原创部分 | MIT | [网站](https://github.com/AyanamiReiChan/ASWired)、[主控](https://github.com/AyanamiReiChan/ASWired-Server)、[Agent](https://github.com/AyanamiReiChan/ASWired-Agent) |
| 修改版 Xray-core | MPL-2.0 | [Agent 内的对应源码](https://github.com/AyanamiReiChan/ASWired-Agent/tree/v1.0.1/third_party/xray-core)，修改文件的 MPL 许可保持不变 |
| Komari 与 komari-web 1.2.5-fix2 修改版 | MIT | [Komari fork](https://github.com/AyanamiReiChan/komari)，前端位于 frontend/ |
| Node.js 24 运行时 | MIT 及捆绑依赖许可 | [Node.js](https://github.com/nodejs/node)，完整许可位于 runtime/LICENSE |

每个部署包的 `licenses/` 保留上述项目的许可证，`BUILD-INFO.json` 记录实际源提交、Go 和 Node 版本。Go 依赖模块清单位于 `licenses/*-modules.json`，对应依赖许可收录在 `licenses/go/`；网站依赖的许可随 `web/node_modules/` 提供。

独立安装的 Komari Agent、Mihomo 和 geo 数据遵循各自许可证。Mihomo 不捆绑于默认部署包；如启用 AnyTLS/Snell，请自行部署固定版本并填写 SHA256。公开源码不改变第三方的商标、插画或其他素材权利。
