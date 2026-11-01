# QQ 自身群昵称、@ 前缀与默认状态修复

2026-10-31。未重启主 bot，未向实际群发送消息。代码与隔离验收完成；线上复验因 NapCat 13001 未监听而未完成。

## 根因

原 QQ codec 已按 QQ 号识别结构化 @，并查询目标昵称、群名片。`core/shell_platform_bridge.py` 转为 Router 消息时却只拼接 TextComponent，丢弃了 AtComponent；仅 @ 时又回退到展示文本。每群 Router 仅持有壳子 config 的固定 prefix，无法用真实 QQ 群名片匹配。因此斜杠命令正常，单独 @群昵称没有进入 `/default_status` 分支。

WebUI 只读确认 `gsmps.command.default_status` 和 `maibot-team.snowluma-adapter` 均已启用且加载成功；原生 `inevitable_at_reply`、`mentioned_bot_reply` 均为 true，默认状态命令未被全局禁用。本轮未改变这些配置。

## 修改

- QQ codec 自动查询当前 bot QQ 号在本群的 nickname/card，并携带受账号/群号约束的 `platform_bot_identity`。普通消息也能补齐身份，无须先有人 @。成功缓存 60 秒、无资料/失败缓存 15 秒，最多 512 组，断连清空；真实 @ 会刷新展示名。
- 查询展示名超时/失败时保留结构化 @ ID 及自身 @ 标记，避免可选昵称请求导致整条消息失效。
- 转译保留 @ 的顺序与 QQ 号；实际自身 @ 用内部 ID 前缀准确清洗，再交给原有 Router/Dispatcher。动态加入该群的真实昵称/群名片，每次更新替换旧别名，不写入壳子 config 或全局原生人设。
- 同名的其他群友仍按其 QQ 号识别，不能误触发自身前缀；@ 后的图片/语音/其他附件保留占位，不能误当成“仅 @”。
- 原生昵称说明同时读取经账号/群号核对的身份 metadata，告诉模型“你在本群的昵称是……”。结构化 @ 的底层检测也优先按本消息 account_id，而非平台上任意已配置 bot 的 ID。
- 实际 @ 保留 `is_at`；纯文本输入本群 `@昵称` 记作 `is_mentioned`，分别服从原生必回/提及策略，不强行覆盖设置。

## 预期行为与验收

| 输入 | 结果 |
|---|---|
| 真实 @まひるん，后面只有空白 | `/default_status`，通过 QQ 原生发送路径回复一条服务器状态 |
| 真实 @まひるん + `/ping` | 清洗自身前缀后执行原有 `/ping` |
| 真实 @まひるん + 聊天正文 | 交给 MaiBot；`inevitable_at_reply=true` 可激活强制轮次 |
| 真实 @其他人，但对方也叫まひるん | 不认作 bot 自身，不触发默认状态 |
| 真实 @まひるん + 图片 | 保留媒体，交给正常处理，不触发空前缀状态 |
| 状态插件被管理员禁用后真实 @ | 不绕过管理开关；保留自身 @ 供原生处理 |
| 改本群昵称 | 新消息刷新别名，移除旧名；不同群不共享群名片 |

成功的状态插件回复本身已响应本次 @，沿用现有插件消息归属，不再额外启动第二次 LLM 回复。需要聊天的 @正文和未被插件消费的 @ 会进入原生必回流程。

## 文件

- `core/shell_platform_bridge.py`：结构化 @、媒体占位、每群动态前缀与原生提及标记。
- `plugins/MaiBot-SnowLuma-Adapter/codecs/inbound/message_codec.py`：自动自身昵称查询、缓存、失败恢复。
- `src/maisaka/chat_loop_service.py`：实际自身昵称/群名片说明。
- `src/chat/utils/utils.py`：按当前适配器账号识别结构化 @。
- `scripts/maibot_dual_plugin_acceptance.py`：扩充真实 Router/Dispatcher/状态 handler/原生发送（截获）的完整链路。
- `scripts/maibot_qq_acceptance.py`：扩充 OneBot 协议、自身昵称刷新/缓存/改名/失败/跨群测试。
- `scripts/qq_live_acceptance.py`：只读报告附带实际自身昵称与群名片。
- `DEV_GUIDE.md`：群内展示身份传递、前缀与单次响应规范。

## 结果与限制

- 双端插件：136 项通过。
- QQ/NapCat 编解码：39 项通过。
- 原生核心与插件桥接：118 项通过（`temp/maibot-bridge-acceptance-/20261031-032453/acceptance.json`）。
- 新增测试覆盖必回轮次实际激活、自身 @ 无上游 flags 的兜底识别、群名片在原生系统提示中的实际拼接、同名其他成员、另一 bot 账号、异群身份注入拒绝及已禁用状态插件的回退。

在线检查时 `127.0.0.1:13001` 拒绝连接，无监听；主运行日志同样持续记录适配器连接失败与重连。所以本轮无法重新查询当前群名片或进行真实 QQ 收发验收，不能把“まひるん”称为本轮在线确认的实际名字。主进程仍是 PID 13748（2026-10-30 23:58:45 启动），核心/壳子更新需要用户手动重启生效；本轮没有擅自重启或启动 NapCat。
