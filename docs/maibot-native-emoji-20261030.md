# 表情包管理与微信发送链路统一至 MaiBot（2026-10-30）

## 实际情况

MaiBot 使用图片文件保存表情包，由 `src/emoji_system/emoji_manager.py` 管理，目录是实例根目录的 `data/emoji`，数据库为 `data/MaiBot.db` 的 `images` 表。当前原生记录类型为 SQLAlchemy 枚举 `EMOJI`，以 SHA256 `image_hash` 标识文件，`full_path` 保存真实路径。管理界面、注册、封禁、描述、使用记录及自动收集均属于 MaiBot。

现有微信入站已将 localType=47 转为 `EmojiComponent`，由原生识别、图片缓存和学习流程处理。微信物理出口继续使用原有 wxSend/UI 发送器，它只负责发送目标和文件，不管理图库。`/emoji` 文本 Emoji 注入插件与图片表情包管理无关，保留。

此前有两处残留：`llm/security/emoji_index.py` 仍会扫描旧配置目录、按文件名和“或”分隔词建立独立索引；微信驱动会把原生 EmojiComponent 的图片另存到临时出口缓存。另有 16 条旧小写 `emoji` 数据行，与原生行的 SHA256 全部重复，其中 2 条仍引用不存在的 `.jpg`，对应原生文件已是 `.jpeg`。

## 修改

- `llm/core/maibot_emoji.py`：统一查询原生图库。兼容字段只允许选择已注册、未封禁、文件可用的原生记录，优先使用 hash，兼容无歧义的真实文件名或完整 stem；不猜测扩展名、不再按旧同义词拆分、不扫描旧目录。
- `llm/security/emoji_index.py`：旧导入接口保留为原生图库薄封装；废除独立索引和旧 emoji_dir 扫描。后续查询直接反映原生数据库变化。
- `core/worker.py`、`core/dispatcher.py`：原有 animation 字段的取图改为原生图库查询，最终 `file_path` 指向 MaiBot 记录中的绝对路径。
- `llm/core/wechat_driver.py`：原生 EmojiComponent 通过 MaiBot 自身的 `ensure_emoji_saved()` 取得实际存储文件，不再为表情包生成出口副本；未知插件表情也由原生缓存保存，保持未注册状态，不绕过审核自动纳入候选库。验证文件内容与 hash、图片格式和封禁状态，避免空文件、错图及不完整的分段发送。
- 原生发送链路为 `send_emoji / attach_emoji -> EmojiComponent -> WechatShellDriver -> core.sender(mode=wechat_file, file_path=原生实际图片路径) -> wxSend(file=同一路径)`。图片字节不重新编码，动态 GIF 保留帧信息。
- `src/webui/routers/emoji/routes.py`：原生管理 API 更新后，在事务提交完成后刷新原生内存候选库；修复原先只移除封禁项、解封和修改描述却不能及时恢复/更新 Planner 候选的问题。
- `scripts/maibot_emoji_migrate.py`：默认只预览。仅在 SHA256 匹配、唯一原生记录的实际文件验证通过且无外键引用时，清理旧重复记录；保留原生注册、封禁、描述和使用数据，删除前归档受影响行。没有通过验证的行会跳过。

没有切换 MaiBot 的 `emoji.use_new_send_logic`：该开关用于 MaiBot 内部两种原生发送方式，当前 false 对应原生 `send_emoji`，与仓库旧版文件名解析系统不同。

## 生产数据处理

已运行 `python -X utf8 scripts/maibot_emoji_migrate.py --apply`，删除旧重复行 ID 1–16。原生行 ID 17–32 及其元数据保持不变，未删除图片。再次预览待处理项为 0。

归档：`data/backups/legacy-emoji-rows-20261030-020415-706363.json`。

现场审计：原生图片记录 253，PNG 127、JPEG 42、GIF 83、WebP 1；缺失文件 0，SHA256 不一致 0，可用且已注册 61。审计报告：`docs/maibot-wechat-acceptance/emoji-production-audit-20261030.json`。

当前临时 WebUI 的 `/api/webui/emoji/list` 实测 HTTP 200、total=253，返回原生图片路径。

## 验收

`python -X utf8 scripts/maibot_emoji_acceptance.py`：**54/54 通过**。

覆盖原生注册/封禁/缺失过滤、忽略旧目录、不猜测 .jpg、CWD 独立、原生 hash 查询、四种格式的内联/按 hash 发送、真实 sender 到 wxSend 文件字段的转换、图片字节一致性、GIF 双帧保留、插件新图缓存与未注册状态、错误文件禁止部分发送、原生 WebUI 列表和动态图原图、WebUI 封禁/解封及 Planner 描述同步、旧数据归档与幂等清理。

报告：`docs/maibot-wechat-acceptance/emoji-native-20261030.json`。

`python -X utf8 scripts/maibot_bridge_acceptance.py`：**74 项桥接检查通过，包含的 65 项单元测试通过**。真实原生工具 `send_emoji` 在隔离实例和本地模型 fixture 下完成选图、发送、回执和历史链路。报告：`temp/maibot-bridge-acceptance-/20261030-020013/acceptance.json`。

所有发送验收在 wxSend 请求出口拦截，未向实际微信群发送测试消息，因此未验收微信客户端最终显示效果。

## 生效

未重启 bot，主进程仍为 PID 14880（2026-10-29 21:52:20 启动）。生产数据库重复项清理已生效；代码变更在用户下一次手动重启后加载。当前临时 WebUI 仍只读，完整管理界面也在该次手动重启后由主 bot 接管。
