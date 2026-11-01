# 静默主动发言

当白名单聊天流一段时间没有收到新消息时，本插件会把“聊天流已静默”的事实写入 Maisaka 上下文，并触发 Maisaka 主动任务，让机器人结合当前人设、关系、记忆和最近上下文自然发起聊天。

## 特性

- 只监控 `targets.target_chats` 配置的白名单目标。
- 优先绑定已经存在的真实聊天流；找不到时才通过 `chat.open_session` 打开或创建，不自行计算 `session_id`。
- 收到白名单目标的真实消息时，会自动把旧空壳流迁移到真实 `session_id`。
- 使用 `chat.receive.before_process` 的观察型 Hook 记录入站活跃度，不拦截、不改写消息。
- 默认静默 2 小时后允许触发主动任务。
- 主动发言后若无人回复，会按 2 倍指数退避，最长 24 小时。
- 收到任意新的入站消息后，重置该聊天流的退避状态。
- 默认 00:00-08:00 免打扰，不主动触发。
- 不直接调用 `send.text` 或 `llm.generate`，统一交给 Maisaka 决定是否以及如何表达。

## 配置

先配置白名单目标：

```toml
[targets]
target_chats = ["qq:group:123456789", "qq:private:987654321"]
```

目标格式：

```text
平台:group/private:号码
```

常用配置项：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `true` | 是否启用插件 |
| `targets.target_chats` | `["qq:group:123456"]` | 白名单目标列表，按实际群号或用户号替换 |
| `idle.idle_threshold_seconds` | `7200` | 基础静默阈值，单位秒 |
| `idle.backoff_multiplier` | `2.0` | 无人回复时的退避倍数 |
| `idle.max_backoff_seconds` | `86400` | 最大退避间隔，单位秒 |
| `idle.failure_retry_seconds` | `600` | 主动任务触发失败后的最小重试间隔 |
| `schedule.check_interval_seconds` | `300` | 后台检查间隔 |
| `schedule.max_triggers_per_check` | `1` | 每轮最多尝试触发的聊天流数量 |
| `schedule.target_resolve_initial_delay_seconds` | `15` | 插件加载后首次解析白名单目标前的延迟，避免阻塞 Runner 就绪 |
| `schedule.target_resolve_timeout_seconds` | `20` | 单个白名单目标解析聊天流的超时时间 |
| `quiet_hours.enabled` | `true` | 是否启用免打扰 |
| `quiet_hours.start` / `quiet_hours.end` | `00:00` / `08:00` | 免打扰时间段 |
| `quiet_hours.timezone` | `Asia/Shanghai` | 免打扰时区 |
| `proactive.intent_template` | 内置模板 | Maisaka 主动任务意图模板 |

`proactive.intent_template` 可用变量：

- `{platform}`：平台名
- `{chat_type}`：`group` 或 `private`
- `{target_id}`：平台侧群号或用户号
- `{stream_id}`：MaiBot 内部真实聊天流 ID
- `{display_name}`：群名或用户昵称，若无法获取则为目标配置字符串
- `{idle_seconds}`：静默秒数
- `{idle_duration}`：中文格式静默时长

## 状态文件

插件会把运行状态保存到：

```text
plugins/idle_proactive_chat/data/state.json
```

其中包含每个聊天流的最近入站时间、最近触发时间、是否等待回复、退避次数和最近错误时间。
