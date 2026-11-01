# MaiBot WebUI 内网免密访问（2026-10-30）

访问地址：`http://192.168.31.75:8001`；本机也可使用 `http://127.0.0.1:8001`。

## 检查结果与修复

检查时 8001 没有监听。`config/bot_config.toml` 已设置 WebUI enabled=true，但微信壳子的 `NativeMaiBotRuntime._bootstrap()` 没有启动原生 WebUI。已补充原生独立线程 WebUI 的启动和关闭流程，使用 MaiBot 原有 dashboard 和路由。

`config/bot_config.toml` 的 `[webui]` 当前配置：

```toml
enabled = true
auth_enabled = false
allowed_networks = ["127.0.0.0/8", "::1/128", "192.168.31.0/24"]
host = ["127.0.0.1", "::1", "192.168.31.75"]
port = 8001
```

`192.168.31.1` 按 /24 所在网段解释为 `192.168.31.0/24`。当前 WLAN 地址为 `192.168.31.75/24`。

新增 `src/webui/middleware/network_access.py`：HTTP 与 WebSocket 均校验实际 TCP 来源；不信任转发头；免密且白名单为空时拒绝访问。原来的 allowed_ips 只是防爬虫豁免名单，不能替代访问控制。Uvicorn 已关闭 proxy_headers，避免本地转发头改变实际来源。

新增字段在 `src/config/official_configs.py` 中声明，默认保持需要密码。当前实例显式关闭认证。更新 `src/webui/core/auth.py`、`routes.py`、插件认证、缩略图和各 WebSocket 认证入口，避免主页免密但部分接口仍要求 Cookie。原访问令牌数据保留，重新开启 auth_enabled 可恢复认证。

已添加 Windows 入站规则 `MaiBot-WebUI-LAN-8001`，限定本机地址 192.168.31.75、TCP 8001、来源 192.168.31.0/24。检查时系统三个防火墙配置文件均处于关闭状态，未修改其开关；当前实际访问隔离由应用层白名单执行。

## 当前运行方式和下一次手动重启

遵守用户“不手动重启 bot”的要求，主进程 PID 14880、创建时间 1793281940.3377273 保持不变。

当前通过 `scripts/maibot_webui_only.py` 启动临时只读 WebUI，PID 15216，监听上述三个地址的 8001。它只加载原生页面及查询接口，不启动第二个聊天核心或插件 Runner。页面有临时只读提示，写操作返回 503，WebSocket 返回 1013，不将独立进程内状态冒充正在运行的聊天核心。

临时进程监视原 bot PID 和创建时间。原 bot 退出后，临时 WebUI 自动关闭并释放端口。用户手动重启 bot 后，修改过的原生嵌入启动流程会启动完整 WebUI，继续保持内网免密。

局限：当前临时界面仅适合查看配置和已持久化历史数据，运行时管理、实时交互和配置修改要在用户手动重启后使用。内网地址按当前 WLAN 地址绑定，机器地址改变时应同步修改 host 和防火墙规则。

## 验证

`python -X utf8 scripts/maibot_webui_acceptance.py`：**44/44 通过**。

- 原生 API 注册、页面、配置 schema、无 Cookie 认证状态与设置状态。
- 插件独立认证、缩略图认证、一次性 WS 票据、原生统一 WS 无 Cookie 握手。
- 回环、目标网段和 IPv4 映射地址允许访问；其他网段 HTTP/WS 拒绝；伪造 XFF 无法绕过。
- 空白/错误白名单拒绝访问；恢复密码模式后未登录请求被拒绝。
- 临时只读提示、写操作和重启操作拦截。
- 独立临时服务跟随测试用进程退出，随后真正的 ThreadedWebUIServer 成功接管同一端口并提供 API、WS 票据。该测试使用独立实例和测试用进程，不操作生产 bot。

报告：`docs/maibot-wechat-acceptance/webui-lan-20261030.json`。

生产现场经本机分别请求 127.0.0.1 和 192.168.31.75 的首页、health、auth/check、setup/status、config/schema/bot、service-mode，共 12 次 HTTP 200。监听进程为 15216；主 bot 创建时间未改变。尚未从另一台局域网设备验证连接。

另外调整 `scripts/maibot_bridge_acceptance.py` 的独立实例设置，明确禁用 WebUI，避免后续桥接验收占用生产 8001 端口。
