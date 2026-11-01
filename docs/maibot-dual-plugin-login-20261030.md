# 双端插件与 NapCat 登录恢复验收（2026-10-30）

## 结果

微信与 QQ 同名的 `GDUT SIE Minecraft Public Server` 是独立群聊。实际身份使用平台、账号、连接范围及群 ID，群名仅供展示；更改昵称或群名不会改变身份。测试直接调用 WebUI 的会话转换和配置写入函数，确认同名 QQ/微信群拥有不同 session_id；对 QQ 群设置 LLM 活跃度为 0，只写入 QQ 平台的规则。原生命令管理禁用/恢复 QQ 群内壳子命令也已验证。

QQ 沿用麦麦原生适配器访问策略、聊天流及命令管理，不另加壳子 QQ 群名单，不继承旧微信群显示名规则。QQ 适配器已在配置中启用，但原生群聊/私聊策略当前仍默认拒绝，用户登录后在原生 WebUI 放行目标 QQ 群即可。插件业务权限和额外限制仍生效，登录 QQ 不会继承微信管理员授权。

## 双端插件实现

- `core/shell_platform_bridge.py`（新增）：原生 QQ 消息转为共享 Router 输入，保留真实来源；插件结果经原生 SendService/Broker 和 QQ 适配器发送。未被壳子插件消费的消息只进入一次 MaiBot 原生 LLM 核心。
- `utils/platform_target.py`（新增）：平台/账号/连接/群私聊目标键，精确匹配原生会话；拒绝歧义目标，不凭群名猜平台。
- `core/router.py`、`core/dispatcher.py`：命令、自动化、连续消息事件保留来源；QQ 绕过微信专用 LLM 入口和微信群开关。后台发送也按来源检查插件开关。
- `core/sender.py`、`core/plugin_activity.py`：现有插件发送模式选择真实平台出口，保留插件输出标记；跨群目标和不可用原生会话拒绝发送，不回落微信。
- `core/maibot_shell_registry.py`：QQ/微信插件开关及原生会话命令禁用接通；微信适配器开关不会误禁 QQ 插件。
- `src/plugin_runtime/integration.py`、`owned_plugin_tools.py`、`wechatbot_agent_tools.py`：原生入口接通插件执行；QQ 会话普通 agent 可调用兼容壳子插件并查看结果，受来源及业务权限限制。
- `auto/patpat.py`：QQ 戳一戳只匹配可信通知中指向自己的事件，普通文本和拍其他人不触发。
- `auto/picture.py`：QQ 图片后续交互直接使用原生图片字节，不查询微信图片接口。
- `plugins/bv.py`：队列保留来源，QQ 去重按会话隔离，视频转为原生 QQ video。
- `plugins/autonomous_life/{plugin,project}.py`、`plugins/mai-repost-shit/`、`plugins/hot-topics/plugin.py`、`plugins/mai-life/streams.py`、`plugins/idle_proactive_chat/plugin.py`：识别完整 QQ 目标与账号/连接身份，兼容原微信目标；QQ 项目权限不继承微信 owner。现有自主任务目标不会凭同名自动扩展为 QQ 群。
- `core/shell_plugin_config.py`、`DEV_GUIDE.md` 第 2.10 节：记录双端目标、来源、发送、命令管理和验收规范。执行插件仍属于 GSMPS，无须迁移成 MaiBot 原生插件。

原生命令匹配保留当前正文，不把引用或聊天记录中的旧命令当作新请求。发送的文本、结构化 @、图片、语音、视频和文件由当前出口能力决定。QQ 引用仍走原生引用，微信继续丢弃不可解析的引用发送字段。

## 登录恢复

`main.py` 新增 `core/napcat_boot.py` 启动检查：用户启动 bot 时复用正在运行的 NapCat，或后台启动准备好的客户端。`scripts/start_napcat.ps1` 从本地保存的账号尝试快捷登录，并运行 `scripts/napcat_login_watch.py`。

恢复助手使用当前 NapCat 4.18.41 的本机 WebUI 协议读取登录状态。初次登录/登录失败时自动打开二维码页面；正常快捷登录有约 15 秒宽限，不打断扫码已接受及初始化阶段。登录成功后保存真实 QQ 号并设置 NapCat 原生快捷登录账号；单实例锁避免反复弹页。恢复观察上限 10 分钟；可见桌面、默认浏览器及腾讯扫码确认仍需由运行用户提供。

本机只读状态实测为 `online=false, phase=waiting_qrcode, qr_available=true`。本轮未重启 bot、未操作真实 QQ 登录、未弹出测试浏览器，也未向实际群发消息。新的启动恢复将在用户下次启动时生效。操作说明见 [NapCat 登录与启用](maibot-napcat-setup-20261030.md)。

## 测试

所有自动验收使用临时实例、测试数据库、模拟模型/外部业务响应和截获发送，不触碰生产群。

| 入口 | 结果 | 验收范围 |
| --- | --- | --- |
| `scripts/maibot_dual_plugin_acceptance.py` | **115/115** | 32 个命令模块、5 个消息自动化双平台 Router/Dispatcher 匹配矩阵；2 个后台监控来源；真实 QQ 命令和图片连续交互；原生编解码及最终 OneBot 动作；普通 agent 权限；原生 WebUI 同名群与命令/LLM 控制 |
| `scripts/napcat_login_acceptance.py` | **14/14** | 本机鉴权协议、在线复用、登录失败、二维码仅弹一次、快捷登录宽限、扫码初始化保护、登录账号持久化、后台启动/禁用及 PlanOnly 无副作用 |
| `scripts/maibot_bridge_acceptance.py --plugin-source plugins --test-agent-tools --test-autonomous-life --test-web-research` | **127/127** | 真实插件 Runner、核心 Planner、工具调用、受限 RCON/沙箱、定时任务、自主生活和项目、联网工具及 QQ 原生回复闭环，内含 65 项单元测试 |
| `scripts/maibot_dual_adapter_acceptance.py` | **52/52** | 双出口、来源隔离、字段能力、@、引用、聊天记录、媒体及部分发送失败 |
| `scripts/maibot_qq_acceptance.py` | **30/30** | 本地真实 OneBot WS、配置、身份与双适配器路由 |
| `scripts/bv_short_link_acceptance.py` | **19/19** | b23 解析及现有队列兼容 |

专项报告：[dual-plugin-20261030.json](maibot-wechat-acceptance/dual-plugin-20261030.json)。最终核心报告：`temp/maibot-bridge-acceptance-/20261030-200913/acceptance.json`；登录测试日志：`temp/napcat-login-acceptance.log`。

全模块匹配矩阵替换业务处理器为记录器，证明消息结构及调度兼容，不代表每个插件依赖的外部网站、游戏服或业务服务当前可用。真实 QQ 快捷登录、二维码浏览器弹出及目标群收发仍需首次扫码后验收，不能由本地模拟测试代替。
