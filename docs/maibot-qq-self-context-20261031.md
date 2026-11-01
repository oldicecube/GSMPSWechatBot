# QQ 自身身份与群禁言事件上下文修复

## 日志证据

生产 `data/MaiBot.db` 的 QQ 历史显示，bot 出站消息使用 `user_nickname="GSMPS Bot"`，没有本群群名片；同一群的 @/拍一拍通知却使用 `まひるん`。例如 2026-10-31 01:59:52 的 bot 出站消息保存为 `GSMPS Bot`，而 2026-10-31 00:43:50 的通知目标是 bot QQ 号 3294652115，文本为“拍了拍まひるん”。这不是单纯别名列表缺失，而是出站历史没有携带适配器解析出的本群身份。

## 修改

- `src/platform_io/bot_identity.py`：新增按平台、账号、scope、群号隔离的短期 bot 身份注册表。QQ 群名片只作为展示别名，QQ 号仍是唯一身份。
- `plugins/MaiBot-SnowLuma-Adapter/runtime/router.py`：QQ 登录后按禁言群白名单预热 bot 的群昵称/群名片；不必等第一次 @ 才知道自己是谁。
- `plugins/MaiBot-SnowLuma-Adapter/codecs/inbound/message_codec.py`：普通入站和真实 @ 都刷新身份；断连清理；查询失败不影响结构化消息。
- `src/services/send_service.py`：QQ 出站消息携带 `platform_bot_identity`，发送者使用当前群名片（例如 `まひるん`），不再只写全局 `GSMPS Bot`。
- `src/maisaka/context/planner_messages.py`、`src/maisaka/runtime.py`、`src/maisaka/reasoning_engine.py`：按 `user_id == account_id` 或插件输出明确标注 `is_self_message=true`，避免把 bot 自己的历史当成普通群友发言。
- `plugins/MaiBot-SnowLuma-Adapter/codecs/notice/renderer.py`：禁言事件若执行者或目标是 bot，文本明确使用“你（机器人）”。
- `plugins/MaiBot-SnowLuma-Adapter/codecs/notice/message_codec.py`：新增 `event_relation`，包含事件类型、执行者 ID、目标 ID、时长、`operator_is_bot`、`target_is_bot` 和来源。
- `src/maisaka/context/planner_messages.py`、`src/maisaka/chat_loop_service.py`：将禁言关系作为结构化上下文提供给模型，避免从显示名猜测谁禁了谁。

配置中的 `nickname = "GSMPS Bot"` 和 `alias_names` 仍保留；人格文本已有“QQ群内为 `まひるん`”的设定，不删除或改写。修复重点是把适配器真实群名片和事件关系送进上下文，而不是继续堆别名。

## 验收

- `scripts/qq_self_context_acceptance.py`：身份注册、提示词本群群名片、自身历史标记、禁言执行者/目标关系共 5 项通过。
- `scripts/maibot_qq_acceptance.py`：QQ/NapCat 编解码及路由验收通过。
- `scripts/maibot_dual_plugin_acceptance.py`：双端插件、QQ 原生发送和现有管理策略验收通过。
- 全部修改文件 Python 编译通过。

主 bot 当前 PID 13748 没有被 agent 重启；这些修复需要你手动重启后加载。当前运行实例仍可能继续显示旧的 `GSMPS Bot` 出站历史，历史记录不会被批量改写；重启后新消息会按 `まひるん` 和结构化事件关系记录。
