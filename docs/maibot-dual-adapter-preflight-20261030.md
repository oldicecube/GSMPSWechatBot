# 微信 / QQ 双适配器接入前验收（2026-10-30）

## 结论及实际运行状态

代码级消息来源、原生/转译接口和发送路由已验收，并修复本次发现的字段丢失、宽泛绑定身份绕过、无效媒体误报成功等问题。微信继续经 GSMPS Wechat Weflow 转译；QQ 经 MaiBot 原生 SnowLuma Adapter 的 NapCat 画像进入同一个核心。后续双端插件适配允许 QQ 复用壳子的 Router/Dispatcher 插件执行，但不会进入微信 LLM 转译链；未消费消息仍只进入一次原生核心。

本次没有手动重启。后续检查时原 bot 主进程已不在运行，NapCat 仍在等待扫码；Host 核心及双端插件修复需用户下一次手动启动后生效。当前状态与新验收见 [双端插件与登录恢复](maibot-dual-plugin-login-20261030.md)。

QQ 尚未完成真实账号接入。`config/adapter_policy.toml` 的 QQ 群聊、私聊仍为 `default_action="block"`，本次没有扩大权限。所有验收使用独立实例、独立数据库、本地模型响应和截获发送，未向真实微信/QQ 联系人发消息。

## 来源及路由约定

- 正式路由：`platform + account_id + scope`。QQ 的 account_id 来自已确认的登录 self_id，scope 来自 connection_id；微信 account_id 为 WeFlow 的自身 wxid。普通消息、通知不能自行更改已登录连接身份。
- Host 生成 `message_origin`，覆盖不可信附加字段；平台、账号、连接范围进入会话 ID、数据库和 LLM 上下文。微信 wxid 与 QQ 号、同号群、不同账号/连接均隔离。
- 出站先检查消息正式来源与选定路由，再检查驱动声明身份；宽泛绑定不能绕过具体账号和 scope。未知来源不猜测账号，不回退到另一个平台。
- QQ 网关再次核对当前登录连接，拒绝微信目标 ID、冲突群号、非法 @ 目标。入站先校验来源，再消耗去重键；去重键包含驱动、平台、账号、连接和外部消息身份。
- 会话 Planner 每轮获得当前适配器能力提示；这是传输限制，不改变人设或原生回复风格。

## 消息字段实际处理

| 字段/能力 | 微信 GSMPS Wechat Weflow | QQ 原生 OneBot / NapCat |
| --- | --- | --- |
| 文本、换行、Unicode | 保留正文 | 保留正文 |
| 用户/群身份 | wxid / chatroom ID；最终 UI 查找使用显示名 | QQ 号 / 群号；独立 group/private API |
| 入站引用、聊天记录 | 现有转译保留作者、内容和来源 | 结构化 Reply/Forward；查询引用和转发内容，保留作者 |
| 出站引用 | 直接丢弃 Reply 控制段及其引用正文，不转成正文或 @；纯引用回执 DROPPED | 可定位的真实平台数字消息 ID 转为原生 reply，支持负 ID；内部/无效 ID 丢弃，纯无效引用 DROPPED |
| 群 @ | 稳定 wxid 交给壳子群成员解析；不以引用中的 @ 生成提醒 | 结构化 QQ 号；内部全体 @ 别名规范为 OneBot `all`，仍受实际群权限约束 |
| 私聊 @ / 微信全体 @ | 明确拒绝 | 私聊 @ 拒绝；群全体 @ 失败由 API 如实回报 |
| 图片、表情包 | 图片文件；表情包从 MaiBot 原生素材位置读取 | 原生 image；普通图与动画表情保留 sub_type 区别，二进制正确编码 |
| 语音 | 壳子语音出口 | 原生 record；按画像请求语音格式，现有转码链处理 SILK。识别文字还取决于 ASR 服务配置 |
| 文件 | 本地文件、下载 URL 或 base64 转文件 | 入站保留 name/size/file_id/url；出站兼容原生 FileComponent 的 base64。NapCat 内联 file；SnowLuma 按画像拆 upload_*_file |
| 视频 | 已有 Dict 视频扩展转文件发送 | 保留视频 URL/文件标识等扩展信息，出站 native video；入站明确 `analysis_available=false`，不声称已看懂视频 |
| 合并转发发送 | 明确拒绝，且不先发同条中的文本 | 原生节点保留作者及正文；NapCat `message` 与 SnowLuma `messages` 参数按画像区分；拒绝空节点 |
| QQ 戳一戳、撤回、禁言等通知 | 不伪装成微信事件 | 原生 notice 编解码及对应配置/工具处理；先验证 self_id 和连接，未启用功能按现有配置过滤 |
| 未知组件、缺失出站媒体 | 整条消息发送前拒绝，不误报成功 | 整条消息发送前拒绝，不再发送 `[image]` / `[voice]` / `[unsupported:...]` 占位文本冒充完成 |

收到但无法获取的图片/语音仍可能保留明确的占位说明；这表示收到该类型，不表示媒体已解析。文件只有 file_id、没有可访问 URL 或字节时也不表示已下载正文。

多段发送不能跨平台原子回滚：QQ 若第二个动作失败，会返回 FAILED、`completed_actions`、`partial_delivery` 和 `automatic_retry_safe=false`，停止后续动作；微信现有失败回执说明已送达段数。调用方不能把失败当作全部成功，也不应盲目重发整条。

## 插件边界

原生工具由现有平台/群私聊/授权条件筛选。后续已在 `core/shell_platform_bridge.py` 与 `src/plugin_runtime/owned_plugin_tools.py` 实现 QQ 对壳子插件的调用和返回，实际实现仍在 GSMPS 模块中。调用保留平台、账号、连接和会话身份，QQ 输出经过原生 SendService/Broker，不调用微信客户端。原生 QQ 管理工具及壳子 RCON 等仍需对应平台的独立授权，普通 agent 身份不会获得操作者权限。详见后续专项验收。

插件输出沿用 Host 的 plugin_activity 上下文标记，与自由 LLM 回复区分。来源字段和这个标记分别保留，不能用插件身份替代平台身份。

## 修改文件

- `src/platform_io/manager.py`：绑定解析身份复核，入站预校验，分来源去重。
- `src/platform_io/drivers/plugin_driver.py`：直接调用防错路由，支持 DROPPED 回执。
- `src/platform_io/message_capabilities.py`（新增）：当前两个出口的能力及 Planner 提示。
- `src/common/message_provenance.py`：Host 派生能力字段，与来源一起持久化。
- `src/maisaka/reasoning_engine.py`：在实际 Planner 拼接中加入当前出口约束。
- `llm/core/wechat_driver.py`：直接调用来源复核、全体 @ 别名拒绝、视频扩展转文件。
- `plugins/MaiBot-SnowLuma-Adapter/plugin.py`：登录连接复核、纯无效引用 DROPPED、多段失败回执。
- 同插件 `runtime/router.py`：消息和通知校验登录账号/连接。
- 同插件 `codecs/inbound/message_codec.py`、`text.py`：文件、视频字段保留，表情标识修复和可读文件描述。
- 同插件 `codecs/outbound/message_codec.py`、`segment_encoder.py`：字段校验、@/引用规范化、缺失媒体拒绝、base64 文件、目标一致性、转发校验。
- `scripts/maibot_dual_adapter_acceptance.py`（新增）：52 项专项测试。
- `scripts/maibot_bridge_acceptance.py`：增加真实原生核心处理 QQ 的完整验收。

## 测试记录

1. `python -X utf8 scripts/maibot_dual_adapter_acceptance.py`：**52/52 通过**。真实 Broker、驱动、序列化、网关、编解码运行，仅截获最终发送；覆盖来源伪造、宽泛路由、去重污染、平台/账号/连接隔离、引用、@、群私聊/临时会话、媒体、转发和部分失败。报告：[dual-adapter-20261030.json](maibot-wechat-acceptance/dual-adapter-20261030.json)。
2. `python -X utf8 scripts/maibot_qq_acceptance.py`：**30/30 通过**。本地真实 OneBot WebSocket 请求/回包、token、引用/聊天记录、双路由及独立接入配置。报告保留现有脚本路径：[qq-napcat-20261029.json](maibot-wechat-acceptance/qq-napcat-20261029.json)（报告文件名为历史命名，本次已重新执行）。
3. `python -X utf8 scripts/maibot_bridge_acceptance.py --plugin-source plugins --test-web-research`：**97/97 检查通过**，其中单元测试项包含 **65/65 测试**。QQ 入站经 Host 原生分发、Maisaka Planner、回复器及 Broker，最终命中 `send_group_msg`，正确群号及结构化 @，微信出口调用数不变；实际 LLM 请求包含 QQ 来源和能力，数据库保存 QQ 来源。本地模型固定响应，不能据此证明上游免费模型当前可用。最终报告：`temp/maibot-bridge-acceptance-/20261030-174947/acceptance.json`。

NapCat 上游接口参考：[API](https://napneko.github.io/onebot/api)、[兼容请求](https://napneko.github.io/develop/api)、[文件框架](https://napneko.github.io/develop/file)。以仓库当前画像和实际生成的请求为最终验收对象，没有升级或替换适配器。

## 真实接入剩余步骤

登录 NapCat 后确认 self_id、连接 ID，按实际 QQ 群号/私聊对象设置最小允许名单，用户手动重启后验收真实文本、引用、@、图片、语音、文件、通知、断线重连和发送权限失败。当前结果证明本地接口契约及路由闭环，不包含真实 QQ 客户端/账号的可用性或群管理权限。微信 UI 显示名重名造成的物理窗口定位歧义也仍是现有 UI 传输限制。
