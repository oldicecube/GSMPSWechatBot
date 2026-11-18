# 主动聊天按群恢复（2026-11-18）

## 背景

之前为了"只保留被动 @ 回复"，把 `chat.reply_timing.reply_only_when_at` 置为 `true`，
它是一条**全局**硬门控：普通接话、`enqueue_proactive_task`（插件主动任务）、空闲续聊
在这条开关下全部停用，且没有按群覆盖的能力。

现在需要只对 QQ / 微信两侧的 `GDUT SIE Minecraft Public Server` 恢复主动聊天，
其余群聊保持"只在真实 @ 时回复"。

## 实现

### 1. 新增配置项

`src/config/official_configs.py` 的 `ChatReplyTimingConfig` 增加：

```toml
[chat.reply_timing]
reply_only_when_at = true
proactive_chat_groups = ["qq:1020993873", "wechat:47447788043@chatroom"]
```

- 每项格式 `platform:group_id`（推荐，群号不受改群名影响）或 `platform:群名`。
- 匹配依据是聊天流的 `platform` + `group_id`（或 `group_name`），私聊永远不匹配。
- 默认空数组，等价于原来的纯全局 @-only 行为。

### 2. 策略层

`src/maisaka/at_reply_policy.py`：

```python
def _proactive_entry(runtime) -> str:      # 命中返回条目原文，否则 ''
def enabled(runtime) -> bool:              # 命中白名单时不进入 @-only 模式
```

影响面（都在 `enabled()` 上）：

- `runtime._get_effective_reply_frequency()`：白名单群聊回落到 `talk_value` 频率，而不是 0/1。
- `runtime.enqueue_proactive_task()`：白名单群聊放行插件主动任务；其他群聊继续返回
  `已开启仅 @ 回复，主动聊天暂时停用`。
- `policy.reply_allowed()`：白名单群聊的 Planner 可以直接发消息。

### 3. @ 仍然必回

非 @-only 模式下 `@` 不走频率，而走强制触发：
`runtime._update_message_trigger_state()` 里
`message.is_at and reply_timing.inevitable_at_reply` → `_arm_forced_turn()`，
调度器在 `_has_forced_turn_trigger()` 处直接放行。所以白名单群里**既恢复主动聊天，@ 也照样必回**。

### 4. 频率

`talk_value` 由 `0.1` 提到 `0.3`。因为其他群聊都被 @-only 拦住、根本读不到这个值，
所以它现在**只影响白名单群聊**的主动接话频率，WebUI「回复频率 → 群聊主动说话频率」可随时调。

## 同时恢复的主动插件（都只作用于这两个群）

| 插件 | 键 | 说明 |
| --- | --- | --- |
| 静默主动发言 `idle_proactive_chat` | `[plugin] enabled` | 冷场 20 分钟后主动找话题，`target_chats` 正是这两个群 |
| 麦麦自主念头与项目生活 `autonomous_life` | `[plugin] enabled` | 自主探索/创作/分享，`life.groups` 正是这两个群 |
| 麦麦生活 `mai-life` | `[schedule] enabled`、`[share] enabled`、`[share] wake_planner` | 生活日程注入 + 日程分享唤醒 Planner，`allowed_streams` 正是这两个群 |

`mai-repost-shit`（搬屎）、`tieba_share`、`forward_messages_auto` 这次**没有**打开，
它们属于内容搬运而不是主动聊天；需要时把对应 `[plugin] enabled` 改成 `true` 即可。

## 验收

- `python scripts/at_only_reply_acceptance.py` — 14 项通过，新增：
  - 白名单群聊 `enabled()=False`、频率回落到 `talk_value`、@ 仍走强制触发；
  - 其他群聊与私聊仍是 @-only；
  - 同名群但不同平台不能互相借道；
  - 配置默认值与当前白名单内容。
- `python -m pytest tests -q` — 全绿。
