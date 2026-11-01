# 麦麦生活

[![version](https://img.shields.io/badge/version-1.3.0-3b82f6?style=flat-square)](https://github.com/tsuiraku9/mai-life)
[![MaiBot](https://img.shields.io/badge/MaiBot-%E2%89%A51.3.1-8b5cf6?style=flat-square)](https://github.com/Mai-with-u/MaiBot)
[![SDK](https://img.shields.io/badge/maibot--plugin--sdk-%E2%89%A52.7.1-0ea5e9?style=flat-square)](#依赖)
[![license](https://img.shields.io/badge/license-MIT-22c55e?style=flat-square)](LICENSE)

MaiBot 第三方插件：每天生成角色生活日程和各聊天流的生活分享，聊天时把近期活动注入规划器。规划器也可以自主安排提醒、继续话题、主动询问、分享等**单次定时意图**，无需关联日程活动。

自动生活分享与自主任务共用同一套工具、按日存储和巡检调度，到期调用 `maisaka.proactive.trigger` 交给 planner 正常处理，不另建一套提醒平台，也不直接套固定模板发消息。自由分享可以结合语境决定是否提起；明确约定的提醒应把履行约定写进意图。**任务提交成功不等于消息已经发出**，插件不改变角色人设，也不承诺必定发言或恰好一次。

日程是角色全局一份，任务按聊天流隔离；白名单分别决定哪些对话能使用日程，以及哪些对话能管理和触发任务。

## 目录

- [它做什么](#它做什么)
- [v1.3.0 兼容变化](#v130-兼容变化)
- [安装](#安装)
- [快速开始](#快速开始)
- [配置](#配置)
- [命令](#命令)
- [规划器工具](#规划器工具)
- [时间状态与恢复](#时间状态与恢复)
- [跨天日程](#跨天日程)
- [旧记录与重生成](#旧记录与重生成)
- [测试](#测试)
- [隐私与发布](#隐私与发布)
- [依赖](#依赖)
- [协议](#协议)

## 它做什么

| 方面 | 生活日程 | 自动生活分享 | 自主单次任务 |
| --- | --- | --- | --- |
| 数据范围 | 全角色一份 | 每个启用流独立 | 只绑定 Host 提供的当前真实聊天流 |
| 来源 | 每日生成，可参考人设、聊天、记忆和近几日安排 | 贴合该流关系与语境，按流数量和额外提示词生成 | planner 用结构化参数创建，不调用辅助 LLM |
| 生效方式 | 聊天时注入近期活动，包含昨日仍在进行的跨天活动 | 到期提交 planner 唤醒请求 | 到期提交具体意图给 planner |
| 管理 | 日程自然语言查询/修改 | 与自主任务共用 query 和 list/add/update/delete | 与自动分享共用工具、存储和调度 |
| 静默时段 | 不适用 | 生成时避开静默时段 | 不受静默时段限制，仍受开关、白名单和冷却限制 |

每日自动生成的频率、日程提示词、按流分享条数、额外提示词和静默过滤规则保留。自主任务存在不代表该流已完成自动分享生成，不会阻止正常补生成。

## v1.3.0 兼容变化

- 保留两个 core-tool 名称：`mai_life_query`、`mai_life_modify`，不新增第二套提醒工具。
- **v1.2 的 `target="share"` 自由文本 `request` 修改必须改用结构化接口。** 新版明确拒绝用辅助模型重写整个任务列表，也不支持分享目标的 `replace_day`。`target="schedule"` 的原自然语言接口保留。
- 查询/管理任务返回 `task_id`、`run_at`、`owner_date`、`source`、`state` 等字段。任务跨日改期不搬迁原始日文档，筛选和定位的日期含义不同，见下文。
- 新增 `[tools]` 工具描述配置，直接展示简短默认文案，可编辑或留空恢复默认。保存后重载插件或重启 MaiBot 生效。
- 修复午夜后的活动查询与 Hook 注入：昨日开始、今日仍在进行的活动不会因今日记录缺失或已经生成而丢失。
- 没有新增 `ConfigUpgradeHook`，不会为了新功能重写既有人设、管理员、白名单或提示词。旧配置缺少 `[tools]` 时直接使用默认说明；已保存的旧 `config_version` 不会被配置模型强制替换。
- 最低 MaiBot 版本提高为 **1.3.1**：此版本已静态核验当前流强制绑定的调用契约，旧版 1.2.3 不再作为该安全边界的兼容承诺。

## 安装

把仓库放到 MaiBot 的 `plugins/mai-life` 目录，通过插件管理器加载；部署方式不支持单独加载时再重启 Host。Docker 部署需确认插件源码和插件运行数据分别按部署文档持久化。

启动后到 WebUI 启用插件，填管理员、日程白名单，并在分享提醒里添加要启用的聊天流。

> [!IMPORTANT]
> `config.toml` 由 Runner 根据配置模型生成和管理。公开的 `config.example.toml` 只是参考，不要覆盖已有个人配置；不要把个人配置、备份或真实日程放进提交或发布包。

## 快速开始

1. WebUI 打开插件，把 **启用插件** 打开。
2. **管理员** 填 `platform:user_id`，例如 `webui:example-user`；请替换为自己的实际平台与用户标识。名单为空时，聊天用户都不能用管理员命令。
3. **日程 → 启用的聊天流** 写上要注入、查询和修改日程的对话。
4. **分享提醒 → 启用的聊天流** 添加条目；这同时是自动分享和单次任务的白名单。自动生成的条数和额外提示词按流配置。
5. 用管理员账号发 `/mai_life_generate`，再用 `/mai_life_show` 查看今日文档；在当前对话让 planner 调用 `mai_life_query(target="share")` 查看跨日期待处理任务。
6. 若修改了 **规划器工具** 中的说明，保存后手动重载本插件，不需要自行编辑 Host 或添加元数据刷新接口。

以下标识均为合成示例，必须替换后才会匹配真实用户和聊天流：

```toml
[plugin]
enabled = true
admin_user_ids = ["webui:example-user"]

[schedule]
allowed_streams = ["session:example-session"]

[[share.stream_profiles]]
stream = "session:example-session"
count_min = 2
count_max = 4
extra_prompt = "只聊日常小事，不要提工作。"
```

## 配置

配置在 WebUI 里修改。这里只列常用项，完整字段以 WebUI 和 [`config.example.toml`](config.example.toml) 为准。

### 插件

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `false` | 总开关；关闭后停止生成、注入、任务操作和到期唤醒 |
| `plugin.config_version` | `1.3.0` | 新配置的结构版本；不是用来清空或强制改写旧配置的开关 |
| `plugin.admin_user_ids` | `[]` | 管理员命令权限，格式为 `platform:user_id` |

管理员名单为空时，聊天用户不能使用任何 `/mai_life_*` 命令；本地控制台操作员始终可用。规划器工具则按当前流及对应功能白名单检查，不以管理员名单替代聊天流权限。

### 每日生成

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `generation.time` | `01:30` | 每天生成时刻，`HH:MM` |
| `generation.timezone` | `local` | 本地时区或 `Asia/Shanghai` 这类 IANA 时区；同时决定任务壁钟时间的解释 |
| `generation.model` | `planner` | 生成用的模型任务名，空则使用 Host 默认 |
| `generation.generate_on_load_if_missing` | `true` | 启动时缺少日程或自动分享时补生成 |
| `generation.include_persona` | `true` | 是否读入 MaiBot 人设，再与补充人设拼接 |
| `generation.extra_persona` | 空 | 补充人设；关闭读入人设时只使用这一段 |
| `generation.include_history` | `true` | 生成时是否读取最近聊天 |
| `generation.include_knowledge` | `true` | 生成时是否检索记忆 |
| `generation.knowledge_window_hours` | `168` | 记忆时间窗（小时），`0` 表示不限 |

### 日程

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `schedule.enabled` | `true` | 关闭后不生成、注入、查询或修改日程 |
| `schedule.allowed_streams` | `[]` | 启用日程注入与查询/修改的流 |
| `schedule.activity_count_min` / `max` | `8` / `14` | 每天生成的活动条数范围 |
| `schedule.wake_time` / `sleep_time` | `08:00` / `01:00` | 角色通常作息，供生成模型参考 |
| `schedule.recent_days` | `3` | 参考最近几天已有日程；`0` 表示不附带 |
| `schedule.recent_inject_count` | `3` | 每次规划时注入的活动数量 |
| `schedule.inject_window_minutes` | `180` | 今日邻近活动优先使用的时间窗口 |

### 分享提醒与单次任务

自动生活分享按流生成，自主任务直接以结构化字段保存。两者共享此分组的开关、白名单、巡检和冷却。

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `share.enabled` | `true` | 关闭后不生成自动分享、不接受任务操作，也不再到期唤醒 |
| `share.stream_profiles` | `[]` | 自动分享与自主任务共同使用的流白名单 |
| `share.wake_planner` | `true` | 是否到期提交规划器唤醒请求；关闭时仍可保存任务，但操作结果带 `warning`，明确不会自动唤醒 |
| `share.patrol_interval_seconds` | `60` | 到期检查间隔，实际循环至少间隔 5 秒 |
| `share.cooldown_seconds` | `180` | 同一流两次唤醒尝试的冷却；成功接受及 RPC 结果未知后均保守计入 |
| `share.silence_start` / `silence_end` | `00:00` / `07:30` | **仅自动生活分享生成时**避开此区间；两端相同表示不设静默，不限制自主意图 |

静默不是运行时禁止所有任务的时间窗。已存在的自动分享不会因后来修改静默时段而自动重排；自主意图可在静默时段到期，但不能绕过开关、白名单、巡检或同流冷却。

`stream_profiles` 每一项：

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `stream` | 空 | 流标识，填写即启用 |
| `count_min` / `count_max` | `3` / `6` | 此流每日自动分享的数量；不限制自主任务数量 |
| `extra_prompt` | 空 | 此流自动分享的额外生成提示词；不作为自主任务的创建请求 |

### 聊天流写法

日程名单与分享名单用同一套写法；对应名单为空时，该功能不对任何流生效。

| 写法 | 示例 |
| --- | --- |
| 全部流 | `all` |
| 会话 ID | `session:<session_id>`，或直接写裸 `session_id` |
| 群 | `<platform>:group:<group_id>` |
| 私聊 | `<platform>:private:<user_id>` |
| 平台 + 用户 | `webui:example-user` |

管理员 ID 使用 `platform:user_id`，不要写成群号。**这些配置写法不是工具的跨流参数**：任务工具只绑定 Host 提供的当前真实聊天流，未注册流和伪造的目标流都会被拒绝。

### 规划器工具说明

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `tools.query_description` | 内置查询描述 | `mai_life_query` 的 planner 可见描述 |
| `tools.modify_description` | 内置日程/任务描述 | `mai_life_modify` 的 planner 可见描述 |

参考麦麦写真的配置方式，字段直接展示可编辑的默认文案，使用 2 行/4 行多行输入框。默认描述只说明用途和必要调用规则，具体参数、状态和恢复机制见下文。已有字段名保留；非空自定义内容替换整段描述，留空或只填空白则使用 [`tool_descriptions.py`](tool_descriptions.py) 的简短默认文案，不改变参数校验和权限。

**保存后重载插件或重启 MaiBot 生效。** 配置保存不会重新注册工具元数据。

### 提示词

`prompts` 分组可以修改日程生成、自动分享生成、日程自然语言修改、注入文本和唤醒模板。留空回落到内置默认，未知占位符原样保留。结构化任务管理不调用日程修改模型，日程修改也不向辅助 LLM 传入统一任务列表；兼容的 `{share_json}` 在日程修改分支固定为 `{}`，在自动分享生成分支仍表示该流分享记录。自主任务到期直接使用其具体 `intent`，不套用“可考虑分享、不一定说”的生活分享模板。

<details>
<summary>占位符</summary>

| 占位符 | 含义 |
| --- | --- |
| `{date}` `{weekday}` `{now}` `{timezone}` | 日期、星期、当前时间、时区 |
| `{bot_nickname}` `{persona}` | 角色名、人设 |
| `{history}` `{knowledge}` | 最近聊天、记忆检索结果 |
| `{yesterday_tail}` `{recent_days}` `{recent_days_schedule}` | 昨日收束、参考天数、近几日日程 |
| `{schedule_json}` `{share_json}` `{recent_schedule}` | 日程 / 分享 JSON、注入用的近期日程 |
| `{wake_time}` `{sleep_time}` | 作息 |
| `{activity_count_min}` `{activity_count_max}` | 日程条数范围 |
| `{share_count_min}` `{share_count_max}` | 当前流的自动分享条数范围 |
| `{silence_start}` `{silence_end}` | 自动分享生成静默时段 |
| `{stream_info}` `{stream_id}` `{extra_prompt}` | 当前流信息、ID、该流额外提示词 |
| `{share_item}` | 自动分享缺少独立意图时供唤醒模板使用的内容 |
| `{user_request}` `{target}` `{action}` | 日程修改请求、目标、动作 |

</details>

## 命令

仅管理员可用，本地控制台操作员例外。

| 命令 | 说明 |
| --- | --- |
| `/mai_life_help` | 帮助 |
| `/mai_life_status` | 开关、今日文档条数、未消费任务数量 |
| `/mai_life_generate` | 强制生成今日日程及可重生成的自动分享；不会覆盖自主/已接管任务或终态，legacy 保护仍生效 |
| `/mai_life_show` | 查看今日原始文档中的日程和各流任务；不是跨日期待处理列表 |
| `/mai_life_wake` | 测试用：立即请求唤醒当前流的 planner，不表示消息已发出 |

需要查跨日期任务或昨日延续的当前活动时，使用下面的规划器查询工具，而不是把 `/mai_life_show` 当成跨日期汇总。

## 规划器工具

仍只有 `mai_life_query` 和 `mai_life_modify` 两个 core-tool。当前 Host 使用 `invoke_method="plugin.invoke_action"` 强制绑定当前流，**组件类型仍为 `TOOL`**；工具没有任意目标流参数，也不执行周期、cron、脚本或任意代码。

### 查询：`mai_life_query`

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `target` | `both` | `schedule` / `share` / `both` |
| `date` | 空 | `YYYY-MM-DD`；日程与任务各自按下述规则解释 |

- 日程：不指定日期或明确指定今天，展示今日日程与昨日仍在进行的跨天活动；明确指定历史日期时查询该日期原始日程。
- 任务：不指定日期时列出当前流**跨日期的 `pending` 任务**；指定日期时按**实际到期日**筛选该日所有状态，不按原始日文档日期筛选。
- `both` 分别应用两种日期规则；各功能仍按自身开关和白名单返回可用内容，不暴露其他流的任务。

```python
mai_life_query(date="", target="both")
mai_life_query(date="", target="share")
mai_life_query(date="2099-04-16", target="share")
```

### 修改日程：`mai_life_modify(target="schedule")`

保留 `action=add/update/delete/replace_day`、非空 `request` 和可选 `date`。`request` 用自然语言或 JSON 描述日程变更；`date` 留空表示今天。此分支仍由辅助模型处理，不接受 `task_id/title/intent/run_at/delay_minutes/request_id` 等任务字段。

```python
mai_life_modify(target="schedule", action="add", date="2099-04-16", request="下午增加一段散步活动，保留其余安排。")
```

### 管理统一任务：`mai_life_modify(target="share")`

| `action` | 必需字段 | 可选字段与限制 |
| --- | --- | --- |
| `list` | 无 | 仅可传 `date`；留空跨日期列 pending，填写按实际到期日查所有状态 |
| `add` | 非空 `title`、`intent`，以及 `run_at` / `delay_minutes` 二选一 | 可传 `request_id`；不接受 `date` 或 `task_id` |
| `update` | `task_id`，以及至少一个需修改的标题、意图或时间字段 | 可传原始归属 `date`；只改标题/意图必须尚未到期，已到期的 pending 任务须提供未来新时间 |
| `delete` | `task_id` | 可传原始归属 `date`；只取消这个 pending 任务，不清空整个流列表 |

字段说明：

- `title`：简短标题。`intent`：到期交给 planner 的具体意图；明确约定写清需履行的提醒，自由分享写清可考虑提起的内容。两者不能通过空值清空。
- `run_at`：未来的 `YYYY-MM-DD HH:MM` 或 `YYYY-MM-DD HH:MM:SS`，按 `generation.timezone` 壁钟时间解释；不接收每任务时区、UTC 偏移、自然语言时刻或 chrono 表达式。
- `delay_minutes`：有限且大于 0 的数字，可为小数；从当前时刻延后，可跨日，与 `run_at` 互斥。
- `task_id`：先从 query/list 取得真实值，不根据标题或记忆猜测。旧 ID 可能跨日重复，此时还必须用查询返回的 `owner_date` 消除歧义。
- `date`：list 时是**到期日筛选**；update/delete 时是**原始归属 `owner_date` 定位**。新任务归属创建时的日文档；跨日改期后保留该归属，不能把改期后的到期日误作 owner_date。
- `request_id`：仅 add 使用的可选幂等键。同一当前流的同一键绑定初始 `intent` 与时间参数（绝对 `run_at` 或相对 `delay_minutes`），重试相同请求不会重新计算延迟或创建任务；不同意图、绝对时刻或延迟数值须用新键。`title` 不参与此幂等签名。任务随后明确改期或修改意图时，初始绑定仍保留，重试原 add 返回同一任务的**当前记录**，不会恢复原内容、撤销修改或重新激活终态。

没有 `request_id` 时，只对同一流、同一到期时刻、同一意图的 `pending/submitting/submitted` 任务作精确去重并返回已有任务，不模糊吞掉有意再次安排。若与 pending 自动分享精确重合，则接管为 `planner` 并保留原始生成签名，防止后续重生成覆盖；其他 pending 旧来源也可按同一确定意图接管。不同意图可以同刻存在，但仍受同流冷却影响。通过幂等返回已有任务也不意味着它仍 pending，须查看结果状态。

update/delete 只允许 `pending`；`submitting` 和所有终态均拒绝，不会重新激活已经提交的任务。修改自动分享后来源改为 `planner`，后续自动重生成保留它；接管缺少 `intent` 的旧分享时须同时补充具体意图。

**旧式 `target="share", request="……"` 会返回参数错误。** share 不支持 `replace_day`；应先查询，再使用上述单任务结构化字段。

下面均为合成调用示例，日期须替换为实际未来时间，任务 ID/归属日期须取自查询结果：

```python
mai_life_modify(target="share", action="add", title="稍后提醒休息", intent="履行刚才约定：到点提醒聊天对象暂停工作、起身休息。", delay_minutes=15, request_id="example-reminder-once")
mai_life_modify(target="share", action="add", title="继续刚才的话题", intent="到点询问刚才讨论的问题是否有进展，结合当时上下文自然继续交流。", run_at="2099-04-16 09:30:00")
mai_life_modify(target="share", action="list", date="")
mai_life_modify(target="share", action="update", task_id="<查询返回的task_id>", date="<查询返回的owner_date>", run_at="2099-04-17 10:00")
mai_life_modify(target="share", action="delete", task_id="<查询返回的task_id>", date="<查询返回的owner_date>")
```

查询/list 返回 `tasks` 列表，增改取消返回 `task`，均带 `success` 和可读 `content`；操作结果另有 `operation`，例如 `created/existing/updated/cancelled`。每条任务记录包含：

| 字段 | 含义 |
| --- | --- |
| `task_id` | 当前流中定位任务的身份 |
| `owner_date` | 原始归属日，改期后不变 |
| `run_at` / `timezone` | 实际到期壁钟时间与当前配置时区；返回时间可采用带 `T` 的 ISO 格式 |
| `title` / `intent` | 标题与具体意图 |
| `source` | `generated` 自动生活分享；`planner` 自主创建/明确接管；`legacy` 来源不能可靠判定的旧记录 |
| `state` / `state_reason` | 生命周期状态与原因 |
| `request_id` | 幂等键，未提供时可为空 |

只有操作 `success=true` 才能说已设置、已修改或已取消；同时检查 `operation`、状态和 `warning`。`share.wake_planner=false` 时可以保存，但必须如实告知不会自动唤醒，不能把“已保存”说成“届时保证发送”。

## 时间状态与恢复

全部任务使用 `generation.timezone`，不推断“明天”，不支持重复、周期或 cron。现有任务也按当前配置时区解释，修改时区前应核对所有待处理任务的 `run_at`。

到期后由巡检提交，实际时刻受巡检间隔、同流冷却和 Host/白名单可用性影响；内部补触发宽限为 **30 分钟**，跨午夜也按完整到期时间计算。冷却中或 Host 明确拒绝时仍可在宽限内等待，超过宽限记录 `skipped`，不会在恢复时把很久以前的任务一口气补发。不能承诺秒级准点。

| `state` | 含义 |
| --- | --- |
| `pending` | 等待处理；可能尚未到期，也可能正在宽限内等待冷却或明确拒绝后的重试 |
| `submitting` | 已认领，正在向 Host 提交；此时不允许改期或取消 |
| `submitted` | Host 明确返回接受唤醒请求；**不等于消息已发出** |
| `skipped` | 已跳过，例如超过宽限、到期时刻无效或旧记录目标不明 |
| `cancelled` | 已取消 |
| `consumed` | 已消费但结果未知：旧 `fired=true`，或 RPC 中断、超时、返回形状未知等；不能推断成功或失败 |

按日 JSON 是事实源，启动时重建跨日期任务定位索引，改期不搬文件。若重启发现 `submitting`，转为 `consumed` 并记录结果未知，**不盲目重发**，因为 Host 可能已经接受。明确返回 `success=false` 才按待处理保留宽限内重试；RPC 中断、超时或未知返回后也会保守记录同流冷却，重启恢复的未知任务则重建可用的冷却记录。该策略避免把未知结果当作失败反复提交，但不是“恰好一次”的业务保证；终态不会通过 update/delete 恢复，需要核对后另建任务。

## 跨天日程

今日 query 与 planner Hook 使用同一只读活动视图：读取今天和昨天的文档，只从昨日带入**确实跨入今天、且此刻仍进行**的活动，不复制或写入今日文件。

例如合成日程中昨日 `23:00` 至今日 `07:00` 的睡眠，在凌晨仍会显示为“昨日开始、今日结束、进行中”，即使今日文件缺失、为空或已生成。恰好 `07:00` 起采用半开区间 `[start, end)`，不再把它作为进行中的昨日延续，也不拿昨日整篇或已结束睡眠兜底。

注入先保留仍进行的昨日延续，再按现有数量与窗口选择今日邻近活动；文本明确真实起止日期、来源日和进行中/待开始/已结束。重叠活动标示来源冲突，不擅自合并或删除。Hook 支持 `items` 与 `messages` 两种 payload，日程开关、白名单和 Hook 的 SKIP 错误策略保留；显式历史日期查询仍使用原日期文档。

## 旧记录与重生成

- 旧 JSON 缺少来源、完整到期时间或状态时兼容读取，不批量迁移。旧 `HH:MM` 按源文档日期解释，旧 `fired=true` 只能视为结果未知的消费状态。
- `legacy` 来源不能可靠判定，不猜测为自动分享，也不覆盖/删除它们。若该流的当日记录含 legacy，自动分享重生成会保守跳过；**`force=True`（含 `/mai_life_generate`）也不绕过这条保护**，日志及管理员命令结果会提示跳过。日程生成是独立部分，不因此被整体禁止。
- 未绑定流的旧任务不会广播给所有白名单流；无法判定目标时记录跳过，相关自动分享流也受来源保护。次日新文档正常进入新版来源管理，不必清空历史文件。
- 重生成仅替换该流可再生成的 `generated + pending` 自动分享，保留自主/已接管任务和提交中、已提交、已取消、跳过等状态；不同流互不覆盖。保存原始自动分享签名后，接管、改期、改意图或取消不会被强制生成以**同一原时刻和意图**重新补回；也过滤与保留任务当前时刻/意图精确相同的新条目。这不是对相近措辞的模糊语义去重。
- 内部 `request_bindings` 保持幂等键的初始请求签名，`generation_key` 保持自动任务的原始生成签名；两者不作为 planner 工具参数，不需要手工编辑。
- 按流生成完成标记区分“合法生成零条”和“模型/解析失败”，避免零条反复补生成；自主任务本身不作为自动生成完成标记。
- 模型调用在存储锁外执行，写回时读取最新文档并合并本次部分；任务 CRUD 和巡检状态不会被旧生成快照全量覆盖，JSON 保存采用同目录原子替换。

## 测试

### 离线测试

测试使用合成日期、临时存储、fake LLM/Host/RPC，包含真实 SDK 的配置 schema 与组件注册检查，不调用真实模型或发送消息。请用公开源码的独立副本，不复制 `config.toml`、配置备份或真实日程。

测试包固定导入 `mailife`，所以**checkout 目录必须名为 `mailife`，并从包含它的父目录运行**。不是在部署目录里直接运行 `pytest tests`，也不要为了测试修改生产目录名或添加别名。以下用 Python 3.11+（测试包含标准库 `tomllib`）：

```bash
mkdir mai-life-check
cd mai-life-check
git clone https://github.com/tsuiraku9/mai-life.git mailife
python3 -m venv .venv
.venv/bin/python -m pip install "maibot-plugin-sdk==2.8.2" pytest
PYTHONPATH="$PWD" .venv/bin/python -B -m pytest -p no:cacheprovider mailife/tests -q
```

已有隔离副本时无需再次 clone；保证布局为 `mai-life-check/mailife/`，其中 `mailife/` 直接包含 `__init__.py`、`plugin.py` 和 `tests/`，再用父目录作为 `PYTHONPATH`。上述命令为 POSIX Shell 示例。

主要覆盖统一任务 CRUD、当前流门禁、跨日时间、幂等与精确去重、旧记录、重生成合并、并发写回、重启/未知结果、宽限与冷却、跨天活动及两种 Hook payload，以及空白/自定义说明在重新注册后的 metadata。

### 受控联调

离线测试通过不等于已经验证真实消息效果。真实模型调用、任务唤醒和生产重载应单独确认后，在受控对话进行：

1. 配好开关、管理员和两类流白名单，先核对 query/list 的数据范围。
2. 修改工具说明后重载插件，核对 planner 可见的最终 `description`，不要只查看详细描述字段。
3. 创建一个未来的单次意图，记录 `task_id/run_at/owner_date`；修改和取消只针对该任务。
4. 对照巡检、冷却和状态检查 Host 是否接受；独立核实实际对话行为，不把 `submitted` 当成发言证明。
5. 验证日程跨午夜的查询/注入与功能关闭、空白名单的门禁。

## 隐私与发布

插件 `.gitignore` 排除个人配置及备份、`config_back/`、运行 `data/`、`life-*.json`、日志、环境秘密和本地维护文档，同时保留公开例子。忽略规则不能清除已经跟踪的秘密，也不能代替提交审核。

- 仅按经过审查的公开文件白名单暂存：公开 `.py` 源码、`_manifest.json`、`config.example.toml`、`.gitignore`、`pytest.ini`、公开测试、`README.md`、`LICENSE`。不要使用 `git add .` 把整个运行目录纳入提交。
- 不提交个人 `config.toml`、任何配置备份、`CLAUDE.md`/`AGENTS.md`、真实日程、日志、`.env`、账号标识、订阅/密钥或机器部署信息。提交前检查 staged 文件清单及内容，隐私扫描只报告问题类型，不回显秘密。
- 发布包只来自**已经审核的 Git commit**，使用 GitHub 自动源码包或 `git archive`，不能压缩整个生产目录。若 HEAD 已经审查，可用下面命令；这不是自动提交或发布流程。

```bash
git diff --cached --name-only
git diff --cached --check
git archive --format=tar.gz --output=mai-life-v1.3.0.tar.gz HEAD
```

正式发布前再次核对远端版本和 Tag；manifest、三段式 Tag 与非草稿 Release 版本应一致。若版本已被发布/收录，应递增版本，不能覆盖历史 Tag。代码修改、离线测试、生产重载和外部发布分别确认，不因文档写有版本号就视为已发布。

## 依赖

- **MaiBot >= 1.3.1**：已静态核验 `plugin.invoke_action` 强制当前流绑定契约；旧 1.2.3 不作为此安全契约的验证范围。
- **maibot-plugin-sdk >= 2.7.1**：manifest 最低版本保留，离线 schema/组件注册测试使用 **2.8.2**。
- 离线测试建议 Python **3.11+**，测试依赖 SDK 与 `pytest`。

## 协议

[MIT](LICENSE)
