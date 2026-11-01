# 旧 Agent Tools 迁移盘点与独立提醒插件

日期：2026-10-29。按当前仓库代码和隔离原生 Runner 注册结果核对。

仓库中没有找到名为 `tools5` 的文件；本次以旧 LLM 实际使用的 `llm/core/llm_service.py::_build_request_tools` 为准，盘点 `llm/web_tools.py` 的 15 个定义和 `llm/rcon_tools.py` 的 1 个定义，共 16 个工具。它们原先还受各自功能开关控制，并非始终全部开放。

## 逐项结果

| 旧工具 | 目前状态 |
| --- | --- |
| `fetch_webpage` | 原生已有 `web_fetch`，但当前没有分配 `web_search` 联网服务，因此未向模型开放。旧的无需额外服务的网页抓取路径没有直接接入。 |
| `fetch_original_message` | 转译层已经接收解析消息，原生 `view_forward_message` 可展开上下文中的合并转发；按 WeFlow local/server ID 再取任意原消息的旧工具未迁入。 |
| `fetch_image_by_message_id` | 已由视觉消息输入、原生图片附件解析与 `maisaka.context.resolve_image` 承接上下文图片；按旧 WeFlow local/server ID 主动补取图片的工具不是同一入口，未原样迁移。 |
| `lookup_group_slang` | 原生黑话学习/匹配/参考注入承接相近用途；旧库显式查询工具未迁入。 |
| `lookup_similar_group_slang` | 没有独立的旧黑话相似查重工具；原生学习流程不是同一套读写接口。 |
| `lookup_group_expressions` | 原生表达学习与回复表达选择承接相近用途；没有旧的显式表达查询工具。 |
| `lookup_group_behaviors` | 未接入旧群互动行为数据库的显式查询工具；原生人设和决策提示不能视为同一功能。 |
| `lookup_group_memory` | 有原生 `query_memory / query_person_profile`。这表示能力已有对应，不代表旧记忆表已经被全量导入新库。 |
| `fetch_tieba_hot_post` | `hot-topics` 的 `get_hot_topics` 提供热榜素材，但任意指定贴吧的热帖正文/摘录接口没有原样迁移；默认贴吧数据源也没有启用。 |
| `record_learning` | 原生记忆自动化、表达和黑话学习继续工作；模型主动写旧库的同名工具没有迁入。 |
| `learn_sentence_reply_compare` | 没有独立迁移“观察句—回复—替代表达—经验”的旧配对写入工具。 |
| `lookup_group_member` | 原生 @ 已能按真实消息发送者/用户 ID 解析，微信发送端会核对成员；没有等价的模型侧昵称查微信 ID 工具。旧配置提示中提到这个名称，并不代表它已注册。 |
| `manage_ai_plugin` | 壳子 AI 文本插件管理代码仍保留；没有向原生 Planner 暴露创建/验证/更新/删除插件的管理工具。 |
| `manage_scheduled_reminder` | **本次迁移为独立插件** `wechatbot.scheduled-reminder`，保留工具名，支持创建、查询、取消。 |
| `update_persistent_state` | 没有迁移旧 interests/todos/pending_thoughts/social_pulse/attention_history 的结构化写入工具；原生思考与人设不是同一份持久状态。 |
| `minecraft_rcon_action` | 已迁为原生 SDK 插件 `wechatbot.restricted-rcon`，保留固定动作、旧白名单及 Host 侧凭据；不开放任意 RCON 命令。详见本轮沙盒/RCON/搬史安装报告。 |

盘点不把“源文件还在”当成“模型已经能调用”。原生工具来自 Builtin、WebSearch、Plugin、Agent 和 MCP Provider；启用条件、外部服务和配置还会决定实际可用集合。

## 提醒插件迁移

安装目录：`plugins/scheduled_reminder`，插件 ID：`wechatbot.scheduled-reminder`，版本 `1.0.0`。只依赖 Python 标准库和已安装的 MaiBot SDK，不启动旧 `LLMService`，也不引入第二套自主 Planner。

工具参数：

- `action=schedule`：提供 `title`、含时区的 `remind_at`。
- `action=list`：查询当前群、当前请求者的活动提醒。
- `action=cancel`：提供查询返回的 `event_id`，只能取消自己的待处理提醒。

提醒时间为未来 10 秒至 366 天，每人每群最多 20 条活动提醒。首次加载生成默认配置（启用、上海时区、5 秒巡检），未改动四个已装插件的配置。

Host 从实际 Planner 请求的触发消息提供保留身份字段，覆盖模型伪造的 `_request_context`；插件使用真实流内的已存消息再次校验，不接受模型指定的请求者、群或目标流。保留原平台账号和路由 scope，以免重启恢复时进入另一个账号的会话。此字段仅注入新提醒插件，保持其他插件的调用签名不变。

数据库位于 `data/plugins/wechatbot.scheduled-reminder/reminders.sqlite3`。到期后通过 SDK `chat.open_session` 恢复目标流，再调用 `maisaka.proactive.trigger`；最终回复和 @ 仍由 MaiBot 核心与微信转译层处理。`submitted` 表示 Host 接受，不能说成微信已送达。Host 明确拒绝时有限重试；RPC 结果未知或提交时中断记为 `uncertain`，不自动重放。工具描述区分用户提醒与 `mai-life` 的 Bot 生活/主动话题任务，避免同一请求重复建任务。

旧定时入口并非完全不存在：兼容层保留了 `handle_scheduled_event` 转译方法和回调 setter；缺的是原生注册的管理工具及运行中的到期调度器。旧库 `llm_scheduled_events` 当前只有 1 条 `completed`，没有 `pending / dispatching`，所以没有待迁移任务；已完成记录未重放，也未删除旧库。

## 验收范围

使用独立实例目录和本地模型桩，微信发送均被拦截；没有向真实群发送测试内容。真实 API 配额和真人对话里的时间理解不由桩测试保证。

验收脚本：`scripts/maibot_reminder_acceptance.py`、`scripts/maibot_bridge_acceptance.py`。前者验证存储、身份隔离、重启恢复、重试、SDK 返回格式与既有插件接口；后者通过原生 Runner 验证模型工具调用落库及到期处理。

本次结果：18 项提醒专项测试全部通过；原生桥接验收 66 项检查中 65 项通过，包含全部 4 项提醒完整链路检查和原有 53 项单测。唯一未通过项为原有 `native.tool.query_image_memory` 缺少 `image_embedding` 模型。实际发送负载包含 `native_segments` 中指向请求者的 `mention`，不是仅显示一个 @ 文本。

证据：`docs/maibot-wechat-acceptance/scheduled-reminder-unit-20261029.json`、`docs/maibot-wechat-acceptance/scheduled-reminder-bridge-20261029.json`。

生产主进程未手动重启；Host 请求上下文补丁需要用户手动重启后加载。原有目录监视器可能自动重载插件 Runner，这不等于生产主进程已经加载新的 Host 源码。
