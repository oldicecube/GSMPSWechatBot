# QQ 协议端替换调研与适配（2026-11-18）

## 结论

- NapCat、SnowLuma、Lagrange.OneBot、OpenShamrock 这批 **OneBot 11 协议端**已经停服或删库。
- 仍在维护的三家实现（**LLBot**、**NapukettoQQ**、**Lagrange.Milky**）原生协议都换成了 **Milky**；
  但前两家同时提供 **OneBot 11 正向 WebSocket**，可以**直接替换**，我们现有转译层不需要改协议。
- 本次已把适配器从"只认 napcat/snowluma"改成"任意 OneBot 11 实现都能连"，因此换协议端只需要改配置里的
  地址 / 端口 / token，不用改代码。

## 核查证据（2026-11-18 实测）

| 项目 | 状态 | 依据 |
| --- | --- | --- |
| `NapCatQQ/NapCatQQ` | ❌ 已删库 | GitHub 404 |
| `NapNeko/NapCatQQ` | ⚠️ 存活但终止 | README 首行 `## Ending 到这里告一段落。`，最后提交 2026-11-12，12.9k★ |
| `SnowLuma/SnowLuma` | ❌ 已删库 | GitHub 404；`Derpyu520/qq-bridge` 公告"上游 SnowLuma 已停止公开服务"，项目将于 2026-11-18 归档 |
| `LagrangeDev/Lagrange.OneBot` | ❌ 已删库 | GitHub 404 |
| `whitechi73/OpenShamrock` | ❌ 已删库 | GitHub 404 |
| `AQiaoYo/NapCatQQ-Desktop`、`DandanLLab/SnowCatQQ` | ⚠️ 已归档 | GitHub archived=true |
| `Mrs4s/go-cqhttp` | ⚠️ 半废弃 | 10.6k★，最后提交 2026-10-20，老架构对新版 QQ 客户端支持差 |
| **`LuckyLilliaDev/LuckyLilliaBot`（LLBot，原 LLOneBot）** | ✅ 活跃 | v8.0.0，3.8k★，最后提交 2026-11-17；核心为 Milky |
| **`LuckyLilliaDev/lucky-lillia-plugin-onebot11`** | ✅ 活跃 | v1.0.1，"支持 HTTP API、HTTP SSE、正向 WebSocket、反向 WebSocket 和 HTTP POST 事件上报" |
| **`NapukettoDev/NapukettoQQ`** | ✅ 活跃 | MIT，最后提交 2026-11-17；Milky 1.3 / Satori / **OneBot11** / KuroBot，无需安装 QQ 客户端 |
| `LagrangeDev/Lagrange.Core`（V2）+ `Lagrange.Milky` | ✅ 活跃 | 3.0k★，最后提交 2026-11-17；**仅 Milky**，无 OneBot 层 |

Milky 官方 "Awesome" 页列出的协议实现只有三个：`Lagrange.Milky`、`Yogurt`、`LuckyLilliaBot`。

## 推荐方案

### 方案 A（首选）：LLBot + OneBot 11 插件

- 社区体量最大、维护最勤（原 LLOneBot 的延续），有官方 OneBot 11 插件。
- 插件提供 `type: 'ws'` 正向 WebSocket：默认 `127.0.0.1:3001`，`token` 必填，`messageFormat=array`。
- 与我们的适配器画像完全对齐（标准 `send_group_msg` / `send_private_msg`、数组消息段）。

### 方案 B（备选/兜底）：NapukettoQQ

- MIT 协议、单进程、**不需要安装 QQ 客户端**，OneBot 11 是其主适配器，文档完整（含每个传输形态的字段说明）。
- 反向 WS（第三方连入）默认端口 **3001**，鉴权支持 `Authorization: Bearer`、裸 token、`?access_token=`，
  与我们传输层"双通道携带 token"的实现天然兼容。

### 不建议

- `kagangtuya-star/bot-adapter-milky-onebotv11` 这类 Milky→OneBot 转换桥（1★，2026-03 后无更新），
  多一层转发就多一层消息字段丢失风险。

## 迁移步骤

### A. LLBot

1. Node.js 24+，安装并启动 LLBot，WebUI 扫码登录目标 QQ。
2. 在 LLBot 插件市场安装 `@luckylillia/plugin-onebot11`，新建 `ws` 连接：
   `host=127.0.0.1`、`port=3001`、`token=<随机长串>`、`messageFormat=array`、`reportSelfMessage=false`。
3. 在 MaiBot WebUI → 适配器 → QQ 适配器设置里填：
   `client_type=auto`、`server=127.0.0.1`、`port=3001`、`token=<同上>`。
4. 手动重启 bot。

### B. NapukettoQQ

1. Node.js 24+：`pnpm create napukettoqq` → `pnpm start`，按提示扫码登录。
2. `napuketto.toml`：

```toml
[accounts.onebot11]
token = "change-me"
messagePostFormat = "array"
[[accounts.onebot11.wsServers]]
enabled = true
host = "127.0.0.1"
port = 3001
```

3. MaiBot 适配器配置同上（端口 3001、同一 token）。
4. 手动重启 bot。

## 本次代码改动

| 文件 | 改动 |
| --- | --- |
| `plugins/MaiBot-SnowLuma-Adapter/profile.py` | 新增 `ONEBOT11_PROFILE`（标准分目标动作、数组消息、无 QZone/无 silk 转码）；`detect_client_type_from_app_name` 对未识别实现回退到 `onebot11`，不再直接报错；`probe_client_profile` 在对端不返回 `app_name` 时同样回退并告警 |
| `plugins/MaiBot-SnowLuma-Adapter/config.py` | `client_type` 取值新增 `onebot11`（Literal + 校验器 + 描述） |
| `plugins/MaiBot-SnowLuma-Adapter/config.toml` | 现网 `client_type` 由 `napcat` 改为 `auto`（换任意协议端都能自动判定） |
| `plugins/MaiBot-SnowLuma-Adapter/README.md` | 说明支持任意 OneBot 11 实现 |
| `scripts/qq_adapter_onebot11_compat_acceptance.py` | 新增兼容性验收（7 项） |

## 验收

- `scripts/qq_adapter_onebot11_compat_acceptance.py`：7/7 通过
  （napcat/snowluma 画像不变、NapukettoQQ/LLBot 等回退 onebot11、空 `app_name` 仍可连、
  对端无响应仍如实报错、`client_type=onebot11` 可显式指定、非法值回退 auto）。
- `scripts/maibot_qq_acceptance.py` 回归通过（本地真实 OneBot WS + 原生编解码 + 路由，无 QQ 登录、无真实发送）。

## 风险与长期方案

- 这批协议端都是 NTQQ 逆向实现，受腾讯风控与法务影响大（SnowLuma 停服、NapCat 终止即此背景），
  换哪个都不能保证长期稳定。建议协议端与 bot 分机部署、token 用随机长串、不要高频批量操作。
- 若追求长期合规稳定，应改用**腾讯官方 QQ 开放平台机器人**（`qq-botpy` / 官方 HTTP API）：
  不会被风控，但只能以官方 Bot 身份入群，不能控制个人号，群管能力以平台开放范围为准。
- 若将来 Milky 成为事实标准，我们只需给适配器再加一个 Milky 传输层（事件走 WS/SSE、API 走 `POST /api/:api`），
  画像层与编解码层可复用，不需要重写。

---

# 附：NapukettoQQ 实测安装与能力矩阵（2026-11-18 夜间）

## 1. 安装位置与环境（与现网隔离）

| 项目 | 值 |
| --- | --- |
| 程序目录 | `C:\qq-adapter-lab\qqbot`（D 盘只剩 2.5G，H 盘是网络共享装不了 pnpm 符号链接） |
| 版本 | `@napuketto/cli 0.6.12` + `@napuketto/adapter 0.16.4` / `kernel 0.12.3` / `loader 0.9.3` / `network 0.3.2` |
| 数据目录 | `C:\qq-adapter-lab\qqbot\.napuketto\3294652115` |
| 自带 QQ 原生文件 | `.napuketto\qq-files\9.9.37-54395`（首次启动自动下载 313MB 官方安装包并解出 `wrapper.node`，**不依赖也不修改**现网 `depends-data/napcat/qq-client`） |
| OneBot HTTP | `127.0.0.1:13010` |
| OneBot WS（反向，MaiBot 连入） | `127.0.0.1:13011` |
| token | `a9e8cd3159dcae6bda820f1db1c30946`（与现网 13001 的 token 不同，互不干扰） |
| 消息格式 | `messagePostFormat = "array"`、`reportSelfMessage = false` |

启动：`scripts\start_qq_napuketto_lab.bat`（内部 `cd /d C:\qq-adapter-lab\qqbot && pnpm start`）。
注意 PowerShell 执行策略禁止 `pnpm.ps1`，必须走 `cmd`。

首次启动会打印终端二维码并把图片存到
`.napuketto\3294652115\cache\qrcode.png`，同时日志给出 `https://txz.qq.com/p?k=...` 链接。

> ⚠️ 扫码登录会让同一 QQ 号的现网会话下线。因此**切换前不要扫码**，先按 §5 改适配器配置，再扫码。

## 2. 实测结论（本地模拟对端，未登录 QQ、未发真实消息）

脚本：`scripts/qq_napuketto_compat_acceptance.py`，报告：
`docs/maibot-wechat-acceptance/qq-napuketto-compat-20261118.json`（20/20 通过）。

覆盖到的链路：

- `get_version_info` 返回 `app_name = "napuketto-qq"` → 适配器判定为通用 `onebot11` 画像（不是 NapCat/SnowLuma）。
- 入站群消息：`reply`/`at`/`text`/`image` 段结构、Unicode 与换行、QQ 身份字段（`platform=qq`、`account_id`、`group_id`）全部保真。
- 引用消息：通过 `get_msg` 解析出被引用人、原文与昵称/群名片。
- 图片：真实 HTTP 下载链路成功，`is_picture = true`。
- 拍一拍：`notice(notify/poke)` 能被放行并渲染成「X 戳了戳 Y（这是QQ的一个功能…）」，且被作用人被正确标为 `target_id`。
- 禁言/撤回/精华：`group_ban` 的 operator/target/duration 关系、`group_recall` 文案、`group_essence` → `essence` 归一化均可渲染。
- 出站：`send_group_msg` 的 `reply`/`at`/`text` 段与回执 `message_id` 正常。
- 合并转发：使用标准 `messages` 参数键。
- 未实现动作（`send_poke`）返回 `retcode=1404`，适配器抛错而非静默成功。

## 3. 相比 NapCat 可能缺失/降级的功能

NapukettoQQ OneBot 11 共实现 **81 个动作**（源码 `defineOb11Action` 枚举）。按我们实际用到与可能用到的能力对比：

| 能力 | NapCat | NapukettoQQ | 影响 |
| --- | --- | --- | --- |
| 发消息 / 引用 / @ / 图片 / 语音 / 视频 | ✅ | ✅ | 无 |
| 合并转发（`send_group_forward_msg`） | ✅（`message`） | ✅（**只接受 `messages`**） | 已在通用画像里改成 `messages` |
| 撤回消息 / 撤回自己的消息 | ✅ | ✅ `delete_msg` | 无 |
| 单人禁言 / 全体禁言 / 踢人 / 管理员 / 名片 / 群名 / 退群 | ✅ | ✅ | 无 |
| 群精华 / 群文件（列表/上传/删除/移动/重命名/转存） | ✅ | ✅ | 无 |
| 表情回应 `set_msg_emoji_like` | ✅ | ✅ | 无 |
| 好友点赞 `send_like` | ✅ | ✅ | 无 |
| **主动拍一拍 `send_poke` / `friend_poke` / `group_poke`** | ✅ | ❌ **未实现** | patpat 只能「被动接收拍一拍再回话」，**不能主动回拍或主动戳人**；调用会拿到 1404 错误 |
| **群公告 `get_group_notice` / `_send_group_notice`** | ✅ | ❌ | 读取/发送群公告不可用 |
| **群头衔 `set_group_special_title`** | ✅ | ❌ | 设置专属头衔不可用 |
| 群文件直链 `get_group_file_url` | ✅ | ❌（有 `download_file` / `get_group_file_download_url`） | 部分下载路径需改走 `download_file` |
| 统一合并转发 `send_forward_msg` | ✅ | ❌（有 group/private 分目标版本） | 适配器本来就走分目标版本，无影响 |
| 最近会话 `get_recent_contact`、点赞列表 `get_profile_like`、在线设备 `get_online_clients`、表情回应列表 `get_emoji_likes`、群打卡 `set_group_sign`、群头像 `set_group_portrait`、群备注 `set_group_remark`、`get_group_info_ex`、`mark_all_as_read`、`ocr_image`、`get_mini_app_ark`、AI 语音 `send_group_ai_record` | ✅ | ❌ | 我们当前功能未依赖，属潜在缺口 |
| NapCat 私有 `nc_*` 扩展 | ✅ | ❌ | 本来就没用 |
| 群名片变更通知 | ✅ `group_card` | ✅ `group_card` | 无 |
| 群名变更通知 | ✅ `notify.group_name` | ❌（无该子类型） | 群改名不会产生事件 |
| 精华通知事件名 | `essence` | `group_essence` | 已加别名归一化 |
| 拍一拍事件 | `notice(notify/poke)` | `notice(notify/poke)`，**源码标注「待真实事件校准」**，每次都会打 `ob11: poke grayTip raw` warn 日志 | 拍一拍可能取不到目标（`target_id` 依赖 `aioOpGrayTipElement.peerUid`），需登录后实测；日志会偏吵 |
| 语音发送格式 | wav 可直接收 | 通用画像按 wav 处理，未实测 | 语音消息可能需转 silk，待实测 |
| 群文件消息（入站） | `file` 段 | 适配器入站不认识 `file` 段，回落成 xml 文本 | 收到群文件会显示成占位文本 |

> 结论：**日常聊天、引用、@、图片、撤回、禁言、合并转发、表情回应都可用；「主动拍一拍」「群公告」「群头衔」三项确定缺失。** 其中「主动拍一拍」是唯一会明显影响现有玩法的缺口（patpat 插件的回拍/主动戳人）。

## 4. 本次为兼容新协议端做的代码改动

| 文件 | 改动 |
| --- | --- |
| `plugins/MaiBot-SnowLuma-Adapter/profile.py` | 通用 `onebot11` 画像的 `forward_param_key` 由 `message` 改为 `messages`（OneBot 11 规范；NapukettoQQ/LLBot 只接受 `messages`） |
| `plugins/MaiBot-SnowLuma-Adapter/codecs/notice/helpers.py` | 新增 `normalize_notice_payload` 与 `NOTICE_TYPE_ALIASES`（`group_essence` → `essence`，并补 `sender_id` → `user_id`）、新增 `resolve_event_target_user_id` |
| `plugins/MaiBot-SnowLuma-Adapter/codecs/notice/message_codec.py` | 通知入口归一化事件名；拍一拍的被作用人改用 `target_id`（此前误用 `user_id`，会把「谁戳谁」记反） |
| `plugins/MaiBot-SnowLuma-Adapter/filters.py` | 通知开关判定前先做事件名别名归一化 |
| `plugins/MaiBot-SnowLuma-Adapter/config.toml` | 去掉 UTF-8 BOM（BOM 会让 `tomllib` 报 `Invalid statement (at line 1, column 1)`，导致**整个 QQ 适配器配置加载失败、QQ 侧完全掉线**） |
| `scripts/qq_napuketto_compat_acceptance.py` | 新增 NapukettoQQ 兼容验收（20 项，本地模拟对端） |
| `scripts/qq_adapter_onebot11_compat_acceptance.py` | 更新通用画像对合并转发参数键的断言 |
| `scripts/start_qq_napuketto_lab.bat` | 实验室一键启动脚本 |

## 5. 切换到 NapukettoQQ 的步骤

1. 停掉现网 bot（手动）。
2. 运行 `scripts\start_qq_napuketto_lab.bat`，用手机 QQ 扫码登录（会顶掉 NapCat 的会话）。
3. 登录后确认 13010/13011 已监听：
   `Get-NetTCPConnection -State Listen | ? {$_.LocalPort -in 13010,13011}`
4. 改 `plugins/MaiBot-SnowLuma-Adapter/config.toml`：

```toml
[client]
client_type = "onebot11"      # 也可保持 auto，探测会回落到通用画像
server = "127.0.0.1"
port = 13011
token = "a9e8cd3159dcae6bda820f1db1c30946"
```

5. 启动 bot，看日志出现 `适配器已连接: ws://127.0.0.1:13011/...` 与画像探测结果。
6. 想回退：把 `port` 改回 `13001`、`token` 换回 NapCat 的值即可，代码无需回滚。

## 6. 仍未验证的部分（需要真实登录）

- 拍一拍事件的 `target_id` 是否真的能取到（源码自己标注未校准）。
- 语音发送格式、群文件上传/下载链路。
- 登录后 OneBot 服务是否在 13010/13011 正常起监听（未登录时不会起）。

## 7. 启动期自动拉起（已接入 bot 生命周期）

`core/qq_adapter_boot.py` 负责在 bot 启动时拉起外部协议端，配置在
`depends-data/qq-adapter/launch.json`（该目录整体被 `.gitignore` 忽略，属于本机配置）：

| 字段 | 含义 |
| --- | --- |
| `enabled` | 总开关，`false` 时完全不处理 |
| `autostart` | 是否在 bot 启动时自动拉起进程 |
| `replace_napcat` | 为 `true` 时 `main.py` **不再**调用 `core/napcat_boot.ensure_napcat`，避免两套协议端抢同一个 QQ 账号 |
| `workdir` | 协议端工作目录（`pnpm start` 的执行目录） |
| `command` | 启动命令，默认 `pnpm start`；PowerShell 下必须走 `cmd.exe /c`，否则会命中 `pnpm.ps1` 执行策略 |
| `uin` | 登录 QQ 号，用于定位 `.napuketto/<uin>/cache/qrcode.png` 与日志 |
| `http_port` / `ws_port` | 反向 HTTP / 反向 WS 端口（当前 13010 / 13011），端口已被占用即视为"协议端在跑"，不重复拉起 |
| `qr_watch_seconds` | 等待扫码的最长时间（默认 900s） |

启动流程：

1. `main.py` 先读 `launch.json` 判断 `replace_napcat`；为真则跳过 NapCat 自启（
   `depends-data/napcat/config/maibot-launch.json` 的 `autostart` 也已置为 `false`）。
2. 调 `ensure_qq_adapter()`：端口未占用且记录的 pid 已不存活时，用
   `cmd.exe /c <command>` 在 `workdir` 后台启动（`CREATE_NO_WINDOW`，输出重定向到
   `depends-data/qq-adapter/adapter.log`），pid 写入 `adapter.pid`，状态写入 `status.json`。
3. 后台线程 `_watch_login` 轮询二维码：把 `.napuketto/<uin>/cache/qrcode.png` 复制到
   `depends-data/qq-adapter/qrcode.png`，并从 `.napuketto/<uin>/logs/napuketto.log`
   提取 `https://txz.qq.com/p?k=...` 登录链接，打印到 bot 控制台。
4. 判定登录成功的信号：`ws_port` 开始监听，或日志出现 `"state":"success"`。

控制台会看到（`[QQ ADAPTER]` 前缀）：

```
[QQ ADAPTER] 启动协议端: pnpm start (cwd=C:\qq-adapter-lab\qqbot)
[QQ ADAPTER] 需要扫码登录 QQ 3294652115，二维码 D:\Wechat Bot\depends-data\qq-adapter\qrcode.png
[QQ ADAPTER] 登录链接（手机 QQ 打开）: https://txz.qq.com/p?k=...
[QQ ADAPTER] 扫码已登录，OneBot WS 端口 13011 就绪
```

> 注意：NapukettoQQ 登录会顶掉 NapCat 的会话。切换时不要两套同时在线。

安全旁路：`MAIBOT_INTERCEPT_SEND=1` 或非 Windows 平台时 `ensure_qq_adapter()` 直接返回 `False`，
不会真的拉起进程（与 `core/napcat_boot` 行为一致），便于测试与离线运行。

**二维码过期自动换新**：若记录的 pid 仍存活但 `ws_port` 没在监听（说明上次扫码没完成、二维码已过期），
启动时会先 `taskkill /T /F` 结束旧进程树再重新拉起，保证每次重启拿到的都是新二维码，
而不是复用一张永远扫不进去的旧图。

**登录链接截断**：协议端日志里的 URL 后面常跟着中文括号（`…&f=1600001604）`），
提取时按 URL 合法字符集截断，控制台与 `status.json` 里都是可直接点开的干净链接。

### 7.1 实测记录（2026-11-18）

在本机用真实 `launch.json` 跑通了一次完整拉起：

```
[QQ ADAPTER] 启动协议端: pnpm start (cwd=C:\qq-adapter-lab\qqbot)
[QQ ADAPTER] 需要扫码登录 QQ 3294652115：二维码 D:\Wechat Bot\depends-data\qq-adapter\qrcode.png
[QQ ADAPTER] 登录链接（手机 QQ 打开）: https://txz.qq.com/p?k=1eAVLuEfDF3KB7V3rOwnB*3jshvPkN91&f=1600001604
state: waiting_scan
```

- `depends-data/qq-adapter/qrcode.png` 已生成（147×147 PNG，肉眼可扫）。
- 测试结束后已结束该进程并清掉 `adapter.pid` / `status.json` / `qrcode.png`，保持干净状态等手动重启。

## 8. 与官方文档对齐后的兼容处理

依据 NapukettoQQ `docs/protocols/onebot11.md`：

### 8.1 错误码

| 场景 | 返回 |
| --- | --- |
| 参数校验失败（zod） | HTTP 入口 `400` / WS 入口 **`1400`** |
| 未知或缺失 action | `404` |
| 业务错误 | `SEND_FAILED=100`、`PERMISSION_DENIED=101`、`NOT_FOUND=102`、`TIMEOUT=103`、`NOT_LOGIN=104`、`INVALID_PARAM=105`、`INVALID_STATE=106`、`UNKNOWN=999` |
| WS 鉴权失败 | 以 **`4401`** 关闭连接（token 可用 `Authorization: Bearer`、裸 token、`?access_token=`） |

### 8.2 数值 ID 强转

对端用 zod 严格校验，`group_id` / `user_id` / `target_id` 传字符串会直接 `1400`。
`profile.py` 的通用 `onebot11` 画像声明 `coerce_numeric_ids=True`，
`services/action_service.py` 会把 `group_id`、`user_id`、`target_id`、`duration`、`times`、
`emoji_id`、`emoji_type`、`count`、`file_count` 的纯数字字符串转成 `int`。
NapCat / SnowLuma 画像保持原样，不受影响。

### 8.3 合并转发降级

官方文档明确：`send_group_forward_msg` 的节点只按 `data.id` 反查已有消息，
**自造节点（`name`/`uin`/`content`）会被静默跳过**。若照旧发送，会得到"返回成功但群里什么都没有"。
通用画像因此声明 `forward_supports_synthetic_nodes=False`，
`codecs/outbound/message_codec.py` 会把合并转发降级为一条文本消息：

```
[昵称] 内容
[昵称] 内容
```

节点构造失败或内容为空时也不再抛错（`outbound_forward_never_raises_without_content`）。

### 8.4 已知缺口（无法通过适配器绕过）

- `group_recall` / `friend_recall` 的 `message_id` 取自 grayTip `msgSeq`，与消息事件的
  `MessageUnique` 不是同一空间，**不能反查原消息**。
- `group_title` / `group_sign` 未接线（无事件）。
- `msg_emoji_like` 无推送回调。
- 通用 `send_msg` 只覆盖 text/at/face/image/voice/reply/json；`file` / `forward` / `xml` 不走通用发送。

## 9. WebUI 展示与验收

- 适配器在 MaiBot WebUI → 插件管理 / 适配器列表中显示为 **「QQ 适配器（OneBot 11）」**
  （`_manifest.json` 的 `name`，`plugin_type=adapter`，`id=maibot-team.snowluma-adapter`，
  版本 `1.1.11`）。改名前叫「SnowLuma 适配器」，容易被误认为只支持已删库的 SnowLuma。
- 验收脚本：
  - `python scripts/qq_napuketto_compat_acceptance.py`（23 项，本地模拟 zod 严格校验对端）
  - `python scripts/qq_adapter_onebot11_compat_acceptance.py`（7 项）
  - `python scripts/maibot_qq_acceptance.py`（双端路由 / 身份 / 引用 @ 全链路）
  - `python -m pytest tests -q`（111 项，含新增 `tests/test_qq_adapter_boot.py` 10 项）
