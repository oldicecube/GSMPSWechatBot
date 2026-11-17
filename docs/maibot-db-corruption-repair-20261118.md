# MaiBot 聊天数据库损坏导致「插件执行异常」修复（2026-11-18）

## 现象

壳子日志里 `/sign`、`/anima` 等命令稳定返回 `插件执行异常`，并且大量消息"处理失败"。
堆栈并不在插件内部，而是在插件执行**之前**：

```
core/dispatcher.py -> llm_service.ingest_context(..., observe_only=True)
llm/core/maibot_runtime.py -> MessageUtils.store_message_to_db_async
sqlite3.DatabaseError: database disk image is malformed   (另有 file is not a database)
```

即：每条入站消息都要先写一条 `mai_messages` 历史记录，写库失败后整条命令链被 `except` 兜住，
最终以 `插件执行异常` 的形式回给用户。插件本身没有执行过。

## 根因

1. `data/MaiBot.db` 文件已损坏（`PRAGMA integrity_check` 报多处 `2nd reference to page`、
   `Rowid out of order`、`invalid page number`，严重时直接 `file is not a database`）。
2. 触发条件高度指向**磁盘写满**：修复前 D 盘仅剩 **0.03 GB**，
   SQLite 在写入过程中磁盘耗尽极易产生页级损坏。清理前 `temp/` 占用约 2.6 GB，
   其中 `temp/maibot-bridge-acceptance-` 单个目录就有 1.68 GB 历史验收残留。

日志统计：`malformed / not a database` 命中 **207** 次，时间跨度 `11-17 23:27:59` → `11-18 00:55:01`。

## 本次改动

### 1. 插件执行不再被"观察写库"拖垮（`core/dispatcher.py`）

新增 `Dispatcher._observe_plugin_context(context)`，把插件执行前的上下文观察写入包在 try/except 里：

- 写库失败只记录 `gsmps_plugin_execution` 错误日志，**继续执行原插件**；
- 不重试插件动作、不伪造持久化成功、不伪造发送成功；
- 返回结构与 `ingest_context` 的"未启用"分支一致
  （`messages=[] / should_reply=False / native_delivery_owned=True / observation_failed=True`），
  下游不会因此重复发送。

替换了 3 处调用：普通消息 observation、auto command 已消费分支、命令插件执行前 observation。

未改动：普通聊天真正的 LLM 准入路径仍然会在数据库损坏时**如实报错**（不吞异常、不假装已回复）。

### 2. 新增数据库修复脚本（`scripts/repair_maibot_db.py`）

默认 dry-run，必须显式 `--apply` 才会动生产库。流程：

1. `PRAGMA integrity_check` 只读探测；已健康则直接退出。
2. 检测 bot 进程与数据库文件占用（`psutil`），运行中拒绝 `--apply`（返回码 2）。
3. 检查剩余磁盘空间，不足（库体积 + 250 MB）直接拒绝（返回码 4）。
4. 备份 `MaiBot.db` / `-wal` / `-shm` 到 `temp/db-repair-<时间戳>/damaged/`。
5. 对**备份副本**先 `PRAGMA wal_checkpoint(TRUNCATE)`，把 WAL 里的最新消息折进主库，再 `sqlite3 .recover` 重建。
6. 校验重建库：`integrity_check=ok`、20 张必需表齐全、最新消息时间不早于原库。
7. `os.replace` 原子替换，并清掉属于旧库的 `-wal`/`-shm`（否则旧 WAL 会污染新库）。
8. 替换后再次校验；失败则用备份回滚。

### 3. 磁盘清理

删除历史验收残留 `temp/maibot-bridge-acceptance-`（1.68 GB）与本次中间产物
（`recovered.sql`、`with-wal`、`base-only` 等重复副本），D 盘可用空间从 **0.03 GB → 2.71 GB**。

## 验证

- `scripts/plugin_execution_db_resilience_acceptance.py`：5 项隔离用例全部通过
  （命令插件在写库失败时仍执行且只执行一次、auto command 不被替换成错误文案、
  普通消息不会误触发插件、非观察路径仍如实抛错、兜底返回值保持 native 投递归属）。
  本地证据（`.gitignore` 忽略）：`docs/maibot-wechat-acceptance/plugin-execution-db-resilience-20261118.json`。
- `scripts/repair_maibot_db.py` 端到端自测：用损坏库快照副本跑 `--apply`，
  结果 `repaired`，`integrity_check=ok`，表 19→20，消息 24417→24409（损坏页内 8 条无法恢复）。
  本地证据（`.gitignore` 忽略）：`docs/maibot-wechat-acceptance/db-repair-selftest-20261118.json`。

## 实际修复结果（2026-11-18 01:37）

用户停止 bot 后执行 `--apply`，结果 `repaired`：

| 项目 | 修复前 | 修复后 |
| --- | --- | --- |
| `integrity_check` | 大量 `2nd reference to page` / `Rowid out of order` / `invalid page number` | `ok` |
| 表数量 | 19（`sqlite_master` 已读不全） | 20（必需表齐全） |
| `mai_messages` | 24417（含损坏页） | 24409 |
| `journal_mode` | wal | wal |

替换后独立复检：`PRAGMA integrity_check = ok`，20 张表，24409 条消息；
用 SQLAlchemy 走 bot 自己的连接做过 INSERT + rollback 写入探测，写入正常且无残留表。
`scripts/patpat_punishment_acceptance.py` 在修复前因 `file is not a database` 直接崩溃，
修复后对**真实生产库**运行 **PASS**。

原始损坏库保留在 `temp/db-repair-20261118-013616/damaged/`。

修复过程中还修掉了一个守护脚本的误判：进程检测原先用子串匹配 `webui.py`，
把 `scripts/napcat_lan_webui.py` 当成了 bot 入口，导致一直拒绝执行；
现改为比对 `.py` 参数的 basename。

## 需要用户执行

```powershell
# 1. 手动停止 bot（本流程不会代为重启）
# 2. 停止后执行：
cd "D:\Wechat Bot"
python scripts\repair_maibot_db.py --report temp\db-repair-report.json          # 预检
python scripts\repair_maibot_db.py --apply --report temp\db-repair-report.json # 修复
# 3. 手动启动 bot
```

修复后建议长期保留至少 2 GB 空闲，避免再次因磁盘写满损坏数据库。

## 遗留

- 损坏页内的 8 条消息无法恢复（已在自测中确认）。
- 生产环境最终修复必须基于"停止后"的数据库重新 `.recover`，不能复用运行期快照。
- 日志中 `SnowLuma 适配器` 反复连接 `ws://127.0.0.1:13001` 失败，与本问题无关，属独立待办。

