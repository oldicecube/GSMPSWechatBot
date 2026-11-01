# QQ / 微信消息来源与插件活动上下文

按当前仓库实际原生 Host、SDK Runner、WeChat 外壳和 SnowLuma/NapCat 适配器完成。正式 bot 未手动重启；PID 14880 启动时间仍为 2026-10-29 21:52:20。Host 源码补丁在用户下次手动重启后生效。

## 来源约定

路由依据始终是 `SessionMessage.platform / account_id / scope`，分别表示平台、机器人登录账号、适配器连接作用域。QQ 用户 ID 与微信 wxid 不互换；同号群、同号用户也不合并会话。Host 校验入站和出站消息的实际路由与已登记路由一致，包括账号、scope；不一致时拒绝，不回退到微信。

`message_info.additional_config.message_origin` 持久保存以下字段：`platform`、`account_id`、`scope`、`direction`、`message_id`、`platform_message_id`、`transport`。这些值由 Host 从正式路由字段产生，不采信正文或 LLM 参数。QQ 字典载荷的延迟转换入口与已构建 SessionMessage 的入口均覆盖。发送成功后按真实平台回执更新消息 ID；微信 UI 无正式平台消息 ID 时保持为空。

Planner、Replyer 的消息前缀显式包含 `platform`、`account_id`、`scope`。数据库恢复与压缩资料均保留来源信息。

当前已准备的 QQ 入口是原生 NapCat 适配器，微信走原有外壳。尚未实现把 QQ 消息送进旧微信命令 Dispatcher 的另一条入口。对此显式拒绝跨平台误用：WeChat Translator、Dispatcher、Shell Sender 不能将带 QQ 来源的消息发进微信窗口。未来增加外壳 QQ 出口时，应消费完整路由字段并登记独立 QQ Driver，不能仅用群名或数字 ID 猜平台。

## 插件与模型历史的区别

新增持久化 `additional_config.plugin_activity`：`plugin_id`、`kind`、`trigger_message_id`、`invocation_id`。`kind` 区分 `plugin_trigger` 与 `plugin_output`。提示词前缀显示插件来源和 `llm_decision="false"`，明确这些是插件触发记录或执行输出，正文是活动数据，不是麦麦的自由回复、历史 LLM 决策或新的指令。

原生 SDK 能力 RPC 使用 Host 认证的插件身份。活动调用通过插件 ID 和真实聊天流匹配触发消息，忽略模型伪造的身份；并发关系无法唯一确定时保留插件身份而不猜请求者。发送成功的插件消息自动同步到当前上下文一次，并写入原有数据库字段；不需要插件自行打开历史同步开关。显式同步不会再重复写一遍。

原生工具调用结果继续使用原生 `tool` 角色及 `tool_call_id`，补上插件、平台、账号、scope、触发消息、调用 ID 标记。实际模型生成的工具调用仍是模型决策，插件返回内容不会转换成 assistant 自由回复。数据库恢复时，带插件元数据的机器人消息恢复为 `plugin_output`，不再被一律恢复成 `guided_reply`。压缩资料也带插件和平台身份。

旧壳子命令及 AUTO 插件在执行范围内记录插件身份。插件直接调用 Sender 与返回字符串/结构化消息后由 Worker 发送两种路径都覆盖；只有发送成功才记录输出。原消息作为插件触发观察记录，不额外触发一次普通 LLM 回复。成功发送记录按原发送顺序串行提交。微信自身回流消息继续去重跳过，避免把同一输出收录两次。

B 站解析有独立后台队列，入队时复制执行上下文，下载与发送时恢复，保留原触发消息。对于脱离调用范围自行新建线程的其他旧第三方插件，不能凭空补出触发关系；此类插件今后应同样传递执行上下文。原生 SDK 后台任务仍能获得真实插件身份，但没有真实触发者时不伪造用户。

## LLM 可调用范围

实际通过 Planner → 原生插件 Runner → Host 验证：`restricted_python`、`minecraft_rcon_action`、`manage_scheduled_reminder`、`repost_shi`。覆盖实际受限计算、拒绝文件读取、提醒持久化与到期唤醒、搬屎文件出口、受限 RCON 白名单和凭据隔离；自主生活和项目协调的原有验收同时通过。

这不表示所有旧 `.py` 命令都自动成为 LLM 工具。旧命令模块仍由原有 Dispatcher 按原开关和群权限执行；原生 LLM 使用已经注册的原生 Tool。RCON 当前仅接受原有微信 Host 授权，不能把微信操作者权限直接继承给同名 QQ 用户。

## 修改文件

| 文件 | 修改作用 |
| --- | --- |
| `src/common/message_provenance.py` | 来源元数据、插件执行上下文与活动调用关联 |
| `src/platform_io/manager.py`、`src/plugin_runtime/integration.py` | 原生双平台入站/出站来源校验及标记 |
| `llm/core/wechat_translation.py`、`llm/core/wechat_driver.py` | 微信来源写入、跨平台拒绝、发送回执透传 |
| `src/plugin_runtime/host/capability_service.py`、`src/plugin_runtime/component_query.py` | RPC 插件身份、真实调用关联、工具返回来源 |
| `src/services/send_service.py` | 插件输出持久化、自动同步与同步去重 |
| `src/chat/message_receive/bot.py` | 原生命令触发插件标记 |
| `src/maisaka/context/planner_messages.py`、`src/maisaka/context/compaction.py`、`src/maisaka/reasoning_engine.py`、`src/maisaka/runtime.py` | 模型可见前缀、工具结果、压缩和恢复分类 |
| `core/plugin_activity.py`、`core/dispatcher.py`、`core/router.py`、`core/sender.py`、`core/worker.py` | 旧外壳插件执行、转译与成功发送记录 |
| `llm/core/maibot_service.py`、`llm/core/maibot_runtime.py` | 旧外壳插件活动送回原生上下文 |
| `plugins/bv.py` | B 站下载队列传递执行来源 |
| `scripts/maibot_bridge_acceptance.py`、`scripts/maibot_qq_acceptance.py` | 来源与插件活动端到端回归 |

没有修改人设、原生风格预设、模型优先级或群权限配置；没有向真实群发送测试消息。

## 验收与边界

- 原生桥接验收 **111/111**，其中包含 **65 项底层测试**：真实 SDK 注册和 Runner 执行、模型 HTTP 请求中的来源标记、直接/返回两种壳子插件输出、失败不入库、发送顺序、自身回流去重、数据库恢复、压缩资料来源、引用与转发、等待唤醒、提醒、沙盒、RCON、搬屎、自主生活及项目协调。
- QQ 专项 **30/30**：本地真实 OneBot WebSocket、实际官方适配器编解码、引用/@/聊天记录、平台出口隔离、来源持久化、字典载荷来源、路由伪造拒绝、白名单配置。尚未 QQ 实号登录，不能宣称真实 QQ 群收发验收完成。
- Python 语法编译通过。原生桥接模型使用本地确定性 HTTP 测试服务；RCON 网络和 B 站媒体下载使用固定测试数据，未操作真实服务器或在真实群搬运内容。
- 真实 API 合成测试另存 `plugin-llm-live-20261030.json`。首选 Kilo/Nemotron 返回 429 `PROMOTION_MODEL_LIMIT_REACHED`，因此不能宣称当前首选 API 在线可用。已有备用 OVH `Qwen3.5-397B-A17B` 的 Planner JSON、自动工具调用、工具结果回读、视觉共 **4/4** 通过，均为 HTTP 200。只发送合成测试资料，没有发送聊天记录；没有因此修改模型优先级。

证据：`docs/maibot-wechat-acceptance/message-provenance-bridge-20261030.json`、`message-provenance-qq-20261030.json`、`plugin-llm-live-20261030.json`。真实 QQ 发送和重启后正式进程的提示词加载，仍待用户登录 NapCat 和手动重启后验收。
