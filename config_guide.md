# 配置参考（config.json）

项目启动时读取根目录的 config.json。请复制 sample_config.json 为 config.json 后填写真实值。样例只保留通用、可运行的最小配置；高级调参和可选模块见本文。

**安全提示**：config.json 可包含微信解密信息、RCON 密码和 API Key，不能提交或公开。改完配置后必须重启 Bot；修改 sample_config.json 不会影响正在运行的实例。

## 1. 配置约定

- 路径可使用相对路径（相对项目根目录）或绝对路径。Windows 反斜杠在 JSON 字符串中要写成双反斜杠。
- 未列出的普通字段会被 Python 主程序忽略；以下划线开头的字段也只是人工备注，并不是真正的 JSON 注释。
- 布尔值写 true / false；数组写 []；对象写 {}。本文件的“默认值”指字段省略时程序采用的值。
- 标记为“必填”的字段，必须存在，或必须在对应功能启用时提供。

## 2. 顶层基础设置

| 路径 | 类型 / 可选值 | 默认值 | 作用 |
|---|---|---:|---|
| token | string，必填 | 无 | WeFlow HTTP 接口令牌。建议使用随机长字符串；LLM 读取原始微信消息时也优先用它鉴权。 |
| target_group | string 或 string[] | [] | 允许 Bot 处理、记录上下文和学习风格的精确群名白名单。空数组拒绝所有群聊。 |
| prefix | string 或 string[] | [] | 严格模式下触发 LLM 的前缀，例如 ["@机器人"]。路由时会去除前缀。 |
| prefix_mode | "strict"、"mixed"；旧值 "only" 等同 strict | "strict" | strict 仅在前缀/@触发时调用 LLM；mixed 还启用普通消息状态机和自动回复。 |
| worker_num | 整数，建议 1–4 | 必填 | 消息工作线程数量。LLM 实际并发仍由 llm.max_concurrent_requests 控制；一般 2 即可。 |
| time_slots | object[] | [] | 每日不响应时段。每项为 {"start":"HH:MM","end":"HH:MM"}，24 小时制；结束早于开始代表跨午夜。空数组表示全天响应，LLM API 池在每日 00:00 重置禁用状态；配置时段后则在不响应期开始重置。 |
| onboardingDone | boolean | 可省略 | 安装/配置向导写入的完成标记。Python 主运行流程不依赖它；无需手动添加或修改。 |

时段示例：

    "time_slots": [
      { "start": "01:00", "end": "08:00" }
    ]

## 2. 语音发送设备（voice_sender）

语音消息会先切换到目标聊天，再将音频播放到指定的虚拟音频设备，并由微信录音。此配置只改变音频注入设备，不改变发送请求体或微信消息路由。

| 路径 | 类型 / 可选值 | 默认值 | 作用 |
|---|---|---|---|
| voice_sender.playback_device_name | string，设备名或不区分大小写的名称片段 | "CABLE Input" | 音频注入端的 Windows 播放设备名。VB-CABLE 通常填写 CABLE Input；不要填写微信录音设备 CABLE Output。找不到时发送失败，不会回退到真实扬声器。 |
| voice_sender.capture_device_name | string，设备名或不区分大小写的名称片段 | "CABLE Output" | 仅用于虚拟音频回路诊断和状态查询的采集设备名；微信实际使用的录音设备仍需在微信设置中选择。 |

设备名匹配规则：程序优先按设备名片段进行不区分大小写匹配，并要求注入设备具有输出通道。可以填系统显示的完整名称，也可以填稳定片段，例如 CABLE Input 或自定义声卡名称。修改后重启 Bot；config.json 中的值优先于 WECHAT_VOICE_PLAYBACK_DEVICE / WECHAT_VOICE_CAPTURE_DEVICE 环境变量。程序只向配置匹配到的设备播放，不会将语音发送到默认扬声器。

VB-CABLE 的方向是：Bot 播放到 **CABLE Input**，微信录音输入选择 **CABLE Output**。如果使用其他虚拟声卡，请将 playback_device_name 改为该虚拟声卡的播放/注入端名称。

voice_sender 可省略，省略时维持 VB-CABLE 默认名称；不需要回路诊断时也可以省略 capture_device_name。

### group_features (per-group feature switches)

`group_features` is an object keyed by the exact group names already listed in `target_group`. An omitted group entry keeps the legacy behavior: all three features are enabled. `default` may be supplied as a fallback object for groups without an explicit entry.

```json
"group_features": {
  "Example Group": {
    "plugins_enabled": true,
    "auto_plugins_enabled": true,
    "llm_enabled": true
  }
}
```

| Path | Type | Default | Effect |
|---|---|---:|---|
| group_features.<group>.plugins_enabled | boolean | true | Enables explicit command plugins in `plugins/`, including command-triggered replies and their background `start()` broadcasts to this group. |
| group_features.<group>.auto_plugins_enabled | boolean | true | Enables `auto/` handlers, raw-message interception, fallback auto handlers, and their background broadcasts for this group. |
| group_features.<group>.llm_enabled | boolean | true | Enables direct LLM replies, mixed-mode proactive processing, timer-driven batch replies, and cycle curation for this group. |

The short aliases `plugins`, `auto_plugins`, and `llm` are also accepted, but the `*_enabled` names used by `sample_config.json` are the recommended form. These switches are evaluated in `Dispatcher`; they do not alter the Router's parsed-message object or the WeFlow message payload.

## 3. WeFlow（weflow）

该段会传给随项目启动的 WeFlow。weflow.dbPath、weflow.decryptKey、weflow.myWxid 是主程序启动检查的必填项；其余字段用于接口和多账号/媒体处理。

| 路径 | 类型 / 可选值 | 默认值 | 作用 |
|---|---|---:|---|
| weflow.dbPath | string，必填 | 无 | 微信文件目录，例如 D:\Documents\xwechat_files。 |
| weflow.decryptKey | 64 位十六进制 string，必填 | 无 | 当前微信账号数据库解密密钥。 |
| weflow.myWxid | string，必填 | 无 | 当前 Bot 所在微信账号 wxid，用来识别 Bot 自己发出的消息。 |
| weflow.wxidConfigs | object，按 wxid 建索引 | {} | 多账号或 WeFlow 向导写入的账号配置。每个子项可有 decryptKey、imageXorKey（整数）、imageAesKey（string）、updatedAt（毫秒时间戳）。保留向导生成的值即可。 |
| weflow.imageXorKey | integer | WeFlow 决定 | 当前账号图片解码 XOR 参数；无媒体图片需求可省略。 |
| weflow.imageAesKey | string | WeFlow 决定 | 当前账号图片解码 AES 参数；无媒体图片需求可省略。 |
| weflow.apiHost | string | "127.0.0.1" | WeFlow API 监听主机。仅本机使用时不要暴露到公网。 |
| weflow.apiPort | integer | 5031 | WeFlow API 监听端口。 |
| weflow.apiBase | URL string | 由 apiHost/apiPort 拼成 | 覆盖 Bot 访问 WeFlow 的地址，例如 "http://127.0.0.1:5031"。 |
| weflow.apiToken | string | 回退到顶层 token | 仅当顶层 token 为空时作为读取原消息接口令牌；通常只维护顶层 token。 |
| weflow.resourcesPath | path string | WeFlow 决定 | WeFlow 资源目录。仓库内通常写 "./weflow-core/resources"。 |
| weflow.messagePushEnabled | boolean | WeFlow 决定 | 是否启用 WeFlow 消息推送；正常运行应为 true。 |
| weflow.messagePushFilterMode | 当前 WeFlow 版本定义的 string | WeFlow 决定 | WeFlow 推送过滤模式；Python 侧原样透传，不解释枚举。 |
| weflow.messagePushFilterList | array | WeFlow 决定 | 与过滤模式配合的列表；Python 侧原样透传。 |

## 音乐源（music）

`/song` 和 `/music` 仍使用网易云搜索第一首结果取得歌曲 ID；自定义音源通过 **Node.js 执行洛雪脚本**（`music-source/` 目录下的 `.js` 文件）解析播放 URL，行为与洛雪桌面版一致。解析和下载会按 `source_order` 依次尝试，网易云默认作为最后降级源。

| 路径 | 类型 / 可选值 | 默认值 | 作用 |
|---|---|---:|---|
| `music.source_order` | string[]：`flower`、`exclusive`、`grass`、`netease` | `["flower", "exclusive", "grass", "netease"]` | 音源尝试顺序；不能有未知或重复 ID。 |
| `music.script_dir` | path string | `music-source` | 洛雪自定义源脚本目录（相对项目根目录）；启动时自动扫描其中全部 `*.js`。 |
| `music.node_executable` | string | `node` | Node.js 可执行文件；需在 PATH 中可用。 |
| `music.source_order` | string[] | 见 `source_order.txt` 或文件名排序 | 可选。省略时读 `music-source/source_order.txt`（每行一个脚本文件名或 source_id）；若文件不存在则按**文件名**排序，最后接 `netease`。 |
| `music.platform_order` | string[] | `["wy","kg","kw","tx","mg"]` | 单个脚本内按平台依次尝试解析 URL 的顺序；会 intersect 脚本 init 声明支持的平台。 |
| `music.quality` | `128k`、`320k`、`flac`、`flac24bit` | `128k` | 传给脚本的音质；微信语音通常使用 `128k`。 |
| `music.resolve_timeout_seconds` | number，>0 | `20` | 搜索和音源解析请求超时。 |
| `music.download_timeout_seconds` | number，>0 | `60` | 音频文件下载超时。 |
| `music.allow_netease_fallback` | boolean | `true` | 是否允许所有自定义音源失败后使用网易云音源。 |
| `music.auto_update_scripts` | boolean | `true` | 是否自动处理脚本 `updateAlert`（从 `updateUrl` 下载并热重载）。无需安装洛雪桌面版。 |
| `music.update_min_interval_hours` | number，≥0 | `24` | 同一脚本两次自动更新的最小间隔（小时）。 |
| `music.sources.<id>.script` | filename string | — | **通常无需配置**。仅在使用 `mode: python` 或手动 HTTP 适配器时需要 `base_url` / `api_key`。 |
| `music.mode` | `lx_script`（默认）或 `python` | 省略 | 设为 `python` 时回退到旧的 HTTP 适配器，需配置 `base_url` / `api_key`。 |

默认脚本映射：

| 音源 ID | 默认脚本 |
|---|---|
| `flower` | `野花音源.js` |
| `exclusive` | `[独家音源] v4.0.js` |
| `grass` | `野草音源.js` |

将洛雪格式的自定义源 `.js` 文件放到 `music-source/` 即可。顺序不写死在代码里，按以下优先级决定：

1. `config.json` 的 `music.source_order`（若配置）
2. `music-source/source_order.txt`（每行一个脚本文件名或 `source_id`，`#` 开头为注释）
3. 按脚本**文件名**字母序

最后可降级 `netease`。`config.json` 里仍可用旧别名 `flower` / `exclusive` / `grass` 指代对应脚本文件。

旧版 `config.json` 可以不增加 `music` 段，程序会使用上述默认链路。若仍使用 Python HTTP 适配器，在对应源下填写 `base_url`（及独家的 `api_key`），并设置 `"mode": "python"`。

## 4. 限流和发送节奏

### rate_limit

所有字段都是每分钟整数计数。带 global_ 前缀的是所有群合计，其余是单群限制。

| 字段 | 默认值 | 作用 |
|---|---:|---|
| messages_per_minute | 10 | 单群全部入站消息上限。 |
| global_messages_per_minute | 20 | 全局全部入站消息上限。 |
| llm_messages_per_minute | 10 | 单群 LLM 请求上限。 |
| global_llm_messages_per_minute | 20 | 全局 LLM 请求上限。 |
| prefix_llm_messages_per_minute | 10 | 单群前缀/@强制 LLM 请求上限。 |
| global_prefix_llm_messages_per_minute | 20 | 全局前缀/@强制 LLM 请求上限。 |
| plugin_messages_per_minute | 10 | 单群命令插件请求上限。 |
| global_plugin_messages_per_minute | 20 | 全局命令插件请求上限。 |
| auto_blacklist_enabled | false | 是否对反复超过限流的来源启用自动黑名单。 |
| auto_blacklist_threshold | 5 | 触发自动黑名单前的累计超限次数。 |

### send_delay

| 路径 | 类型 | 默认值 | 作用 |
|---|---|---:|---|
| enabled | boolean | false | 是否在每条出站消息前随机等待。 |
| min_seconds | number，≥0 | 0 | 随机延迟下界。 |
| max_seconds | number，≥ min_seconds | 0 | 随机延迟上界；小于下界时按下界处理。 |

发送延迟只影响发送时机，不改变文字、图片或语音内容；语音注入仍按实际音频时长执行。

## 5. LLM 主设置（llm）

### 5.1 运行、上下文与工具

| 路径 | 类型 / 范围 | 默认值 | 作用 |
|---|---|---:|---|
| enabled | boolean，必填 | 无 | 是否启用 LLM。false 时无需配置 API。 |
| history_expire_ms | integer，必填 | 无 | 短期群消息历史过期时间（毫秒）；600000 为 10 分钟；≤0 表示不按时间过期。 |
| max_history_chars | integer，≥0 | 5000 | 每个群保留的短期历史字符预算。 |
| group_message_limit_chars | integer，≥0 | 2000 | 单条或单次群消息收集的字符限制；0 表示此层不额外截断。 |
| context_window_tokens | integer，最小 10000 | 200000 | LLM 上下文总预算估计值，应不大于实际模型窗口。 |
| context_compression_target_tokens | integer，最小 5000 | 160000 | 超出窗口后的压缩目标 token 数。 |
| cache_prefix_tokens | integer，≥0 | 24000 | 聊天历史稳定缓存前缀的 token 预算；0 表示不拆分缓存断点。 |
| max_concurrent_requests | integer，1–4 | 1 | Worker 和自动回复共用的 LLM 最大并发。 |
| direct_request_wait_seconds | number，0–10 | 3 | 前缀/@直接请求等待并发槽的最长时间。 |
| request_timeout_seconds | number，10–120 | 45 | 未在单个 API 项指定时的请求超时秒数。 |
| tool_loop_timeout_seconds | number，10–120 | 45 | 一轮工具调用循环的总超时秒数。 |
| web_fetch_enabled | boolean | true | 是否向 LLM 暴露网页抓取工具。 |
| web_fetch_max_calls | integer | 3 | 一轮请求最多抓取网页次数。 |
| web_fetch_timeout_seconds | number | 15 | 单次网页抓取超时秒数。 |
| web_fetch_max_chars | integer | 24000 | 单次网页内容最大字符数。 |
| original_message_enabled | boolean | true | 是否允许 LLM 读取微信原始消息。 |
| original_message_max_calls | integer，1–4 | 2 | 一轮请求最多读取原消息次数。 |
| original_message_timeout_seconds | number | 8 | 原消息接口超时秒数。 |
| original_message_max_chars | integer | 16000 | 单次原消息读取最大字符数。 |
| image_tool_enabled | boolean | true | 是否启用“按消息 ID 获取图片”的 LLM 工具；关闭后不向模型暴露该工具。 |
| image_tool_max_calls | integer，1–3 | 1 | 单轮工具循环最多获取的图片数量。 |
| image_tool_max_bytes | integer，256 KiB–16 MiB | 8388608 | 单张图片下载大小上限；图片会先下载并转为 data URL，再作为多模态输入发送。 |
| intercept_auto_plugins | string 或 string[] | [] | 指定需要参与 LLM 放行判断的 auto 插件名。插件必须声明 `INTERCEPT_LLM = True` 并实现 `allow_llm(context)`；任一指定插件拒绝时，本条消息不转发给 LLM。该字段不负责注册或触发 auto 插件。 |
| prefix_bypass_wxids | string 或 string[] | [] | 这些 wxid 无前缀也可直达 LLM；只填受信任账号。 |
| admin_wxids | string 或 string[] | [] | 管理命令授权 wxid，供 ban、echo、points、rcon、prompt 等插件使用。 |
| bot_wxids | string 或 string[] | [] | 额外识别为 Bot 的 wxid；weflow.myWxid 会自动加入。 |
| emoji_dir | path string | 空 | 本地表情索引目录；为空不加载。 |
| assistant_nickname | string | "LLM" | 识别 Bot 自己的历史消息时使用的昵称补充。 |

### 5.2 API 池（llm.apis）

apis 是数组。每次请求按 priority 从小到大尝试；当前 API 失败后会在同一请求中立即尝试下一路。某 API 连续请求失败 3 次后，仍保留在 API 池中，但会临时调整到队末 30 分钟；该 API 任一次请求成功后清零连续失败计数并恢复原优先级；不再按不响应期边界自动重置。

图片工具调用链：模型只能使用当前上下文中已经出现的消息 ID，并且只能查询当前群聊/会话。工具先向 WeFlow 查询原始消息；如果原始消息没有媒体字段，则回退到 `/api/v1/messages?media=1&image=1`，按 `localId`、`serverId`、`svrid` 或 `rawid` 匹配。找到 `mediaUrl` 后下载并限制大小，随后在本地工具循环中作为 `image_url` 多模态消息转发给模型。内部 base64 data URL 不会作为普通工具文本暴露给模型。

`supports_images` 是每个 API 的安全开关，不是模型能力探测：设置为 `false` 时不会向该端点发送图片工具；API 池在一次请求中切换到不支持图片的备用端点时，会移除图片块并保留说明文本，避免备用模型因收到不支持的内容而报错。

| 字段 | 类型 / 可选值 | 默认值 | 作用 |
|---|---|---:|---|
| name | string | "apiN" | 日志显示名称。 |
| protocol | "openai"、"responses"、"anthropic" | "openai" | openai 为 Chat Completions 兼容格式；responses 为 OpenAI Responses 格式；anthropic 为 Messages 格式。未知值按 openai 处理。 |
| model | string，必填 | 无 | 服务端模型标识。 |
| api_key | string，必填 | 无 | 该路 API 密钥。 |
| base_url | URL string，必填 | 无 | API 基地址，末尾斜杠会被删除；接受旧别名 api_base。 |
| priority | 正整数 | 数组位置 + 1 | 数字越小越先调用；相同优先级按数组顺序。 |
| timeout_seconds | number，10–120 | 继承 llm.request_timeout_seconds | 本 API 请求超时。 |
| max_tokens | 正整数或 0 | 0 | 输出 token 上限；0 表示由客户端/服务端决定。 |
| supports_images | boolean | false | 当前模型是否支持视觉/多模态输入。仅为 true 的 API 会接收图片内容并在当前优先端点上暴露 `fetch_image_by_message_id`；未知能力时保持 false。 |
| cache | boolean | true | 是否允许协议客户端使用提示词缓存。 |
| cache_scope | "full"、"system"、"none" 或空 | 客户端决定 | 可缓存范围；none 明确关闭缓存。 |
| prompt_cache_key | string | 空 | Responses 的可选固定路由键。通常留空：客户端会自动生成按“群 + 回复流”隔离、且不含原始群名/用户/消息内容的运行时键。填写后会覆盖运行时键；仅服务商文档明确要求固定键时使用，且不要放入敏感数据。 |
| responses_explicit_cache_breakpoint | boolean | false | 仅 Responses。true 时在稳定历史边界写 `input_text.prompt_cache_breakpoint`。默认 false，优先让服务端自动寻找最长公共前缀；只有服务商文档明确要求显式断点时才开启。 |
| cache_mode | "auto"、"manual"、"off" | "auto" | Anthropic Messages 专用。manual 写 cache_control 断点；auto 请求 auto_cached:true；off 关闭缓存。网关不支持 auto_cached 时用 manual。 |
| cache_ttl | 空 string 或 "1h" | 空（通常服务端默认 5 分钟） | Anthropic 缓存 TTL；当前客户端将 "1h" 解释为一小时。其他值取决于网关兼容性，不建议猜测。 |

最小示例：

    "apis": [
      {
        "name": "primary",
        "protocol": "responses",
        "model": "your-model-name",
        "api_key": "your-api-key",
        "base_url": "https://api.example.com/v1",
        "priority": 1
      }
    ]

**旧单 API 兼容项**：当 apis 不存在或没有有效项时，程序仍支持 llm.model、llm.api_key、llm.api_base（或 llm.base_url），并以 OpenAI Chat Completions 调用。新配置不要同时写两套；apis 优先。llm.provider 仅为旧配置兼容保留，不再决定协议或 API 选择。

### 5.3 按群聊覆盖人设与回复积极度（llm.group_profiles）

`group_profiles` 的键必须是精确群聊名称。配置后，该群会在全局 `llm` 配置基础上覆盖自己的身份、人设提示和自动回复/主动回复参数；未配置的群继续使用全局配置。该覆盖只发生在 LLM 内部，不改变 Router 解析结果、发送请求或 WeFlow 消息体结构。

支持的字段：

| 字段 | 类型 | 作用 |
|---|---|---|
| `identity` | object | 覆盖群聊专属名称、角色、风格和规则；支持 `name`、`role`、`style`、`rules`。 |
| `reply_style` | string | 回复表达偏好，例如简短、调侃、正式或技术导向。 |
| `behavior_style` | string | 判断是否插话、是否跟进、如何处理不同话题的行为指导。 |
| `group_prompt` | string | 群聊语境提示。 |
| `private_prompt` | string | 私聊语境提示。 |
| `prefixes` | string 或 string[] | 覆盖只写入该群 LLM 提示词的 Bot 标记列表；不会改动 Dispatcher 的全局前缀判定。可设为 `[]`，避免不同群的人设提示带入无关前缀。 |
| `prompt` | object | 对全局 `llm.prompt` 的局部字段覆盖。 |
| `auto_reply` | object | 该群独立的自动回复状态机参数，如 `enabled`、触发阈值、空闲窗口、工作/关注周期、会话租约和批处理参数。 |
| `proactive` | object | 该群独立的主动回复参数，如 `enabled`、`aggressiveness`、最小间隔和每日上限。 |

示例：

```json
{
  "llm": {
    "group_profiles": {
      "Your Target Group Name": {
        "identity": {
          "name": "Group Bot",
          "role": "A regular group member",
          "style": "Natural and concise",
          "rules": []
        },
        "reply_style": "Short and context-aware",
        "behavior_style": "Join relevant conversation; do not force replies",
        "group_prompt": "Participate naturally in this group",
        "private_prompt": "Reply naturally in private chat",
        "auto_reply": {
          "enabled": false,
          "reply_trigger_mode": "conversation_pulse",
          "idle_min_seconds": 600,
          "idle_max_seconds": 1800
        },
        "proactive": {
          "enabled": false,
          "aggressiveness": 0.25
        }
      }
    }
  }
}
```

群聊专属提示会和原有核心 Persona、任务要求、上下文及安全规则一起参与构建；`group_profiles` 不是新的全局 Persona。

### 5.4 自动回复状态机（llm.auto_reply）

该段仅在 prefix_mode 为 mixed 时有效；strict 会完全关闭自动回复。allowed_groups 非空时还会限制自动回复群，不能绕过顶层 target_group。

| 字段 | 类型 / 范围 | 默认值 | 作用 |
|---|---|---:|---|
| enabled | boolean | mixed 模式为 true | 总开关。 |
| allowed_groups | string 或 string[] | [] | 自动回复允许群；空表示使用顶层目标群。 |
| reply_trigger_mode | "frequency"、"reply_necessity"、"conversation_pulse" | "conversation_pulse" | 普通消息转交 LLM 的本地判定策略，推荐 conversation_pulse。 |
| reply_necessity_threshold | integer，0–200 | 35 | 必要性评分阈值。 |
| pulse_plan_threshold | integer，0–200 | 28 | conversation_pulse 的计划阈值。 |
| pulse_plan_cooldown_seconds | integer，0–3600 | 45 | 同群普通计划请求冷却时间。 |
| pulse_topic_min_messages | integer，2–30 | 4 | 认定话题持续所需的近期开口数。 |
| pulse_topic_overlap | number，0.01–1 | 0.16 | 话题词重叠阈值。 |
| fragmented_chat_min_messages_per_minute | integer，1–60 | 3 | 零散聊天检测的最低密度。 |
| fragmented_chat_min_silence_seconds | integer，30–86400 | 360 | 零散聊天触发前需要的静默时间。 |
| fragmented_chat_min_interval_seconds | integer，30–86400 | 360 | 零散聊天计划之间最小间隔。 |
| fragmented_chat_plan_probability | number，0–1 | 0.12 | 满足零散聊天条件后实际计划的概率。 |
| idle_quiet_min_seconds / idle_quiet_max_seconds | integer, >=1 | 600 / 1800 | Random idle wait window (10-30 minutes); after timeout the manager enters working state. Legacy aliases idle_min_seconds and idle_max_seconds are accepted. |
| conversation_lease_seconds | integer/number, >=15 | 90 | Short lease after an ordinary reply for the same sender; it only bypasses local frequency suppression and does not force a reply. |
| conversation_lease_direct_seconds | integer/number, >=15 | 180 | Lease after explicit @/prefix/reply-to-bot or uncertain follow-up signals. |
| density_window_seconds | integer，≥10 | 60 | 计算消息密度的窗口。 |
| density_upper_messages_per_minute | integer，≥1 | 8 | 达到该密度时进入关注逻辑。旧别名 density_upper_min_per_minute 可回退。 |
| density_attention_check_interval_seconds | integer，≥5 | 30 | 关注期检查间隔。 |
| density_attention_half_life_seconds | number，≥1 | 300 | 密度影响衰减半衰期。 |
| density_attention_curve_power | number，≥0.1 | 1.7 | 密度到关注概率的曲线指数。 |
| adaptive_idle_content_enabled | boolean | true | Enables earned idle-content candidate windows. The gate uses completed-cycle member activity, cadence, two-person dominance and prior feedback; it is not a daily budget and the model may still remain silent. |
| proactive_web_min_seconds / proactive_web_max_seconds / proactive_web_reset_after_bot_messages | legacy integer fields | ignored | Retained only so older configs load. They no longer schedule a global repost timer. |
| work_min_seconds / work_max_seconds | integer | 120 / 300 | 工作期持续范围。 |
| batch_debounce_seconds | number | 15 | 消息批处理防抖时间。旧别名 batch_interval_seconds 可回退。 |
| batch_max_messages | integer，2–100 | 20 | 单批最多普通消息数。 |
| attention_min_seconds / attention_max_seconds | integer | 120 / 300 | 关注期持续范围。 |
| attention_message_limit | integer | 10 | 关注期保留的最近消息数。 |
| attention_no_reply_limit | integer | 3 | 连续无回复检查次数上限。 |
| attention_nonsense_probability | number，0–1 | 0.12 | 关注期触发轻量情绪/黑话回应的概率。 |
| attention_slang_emotional_step | number | 0.1 | 连续未触发时的概率增量。 |
| work_extend_threshold_seconds / work_extend_seconds | integer | 180 / 180 | 工作期临近结束仍活跃时的延长规则。 |
| exclude_commands | boolean | false | 是否忽略斜杠命令。 |
| exclude_media | boolean | false | 是否忽略媒体消息。 |
| attention_context_token_budget | integer | 1000 | 关注期发送给 LLM 的上下文 token 预算。 |
| reply_timing_learning_enabled | boolean | true | 是否用历史结果抑制过度活跃时的检查概率。 |
| reply_timing_min_windows | integer，≥1 | 20 | 启用学习门控前的最小观察窗口。 |
| reply_timing_target_reply_rate | number，0.01–1 | 0.28 | 目标回复比例。 |
| reply_timing_probability_floor | number，0–1 | 0.15 | 学习门控下的最低检查概率。 |

### 5.5 身份、提示词、记忆和学习

#### llm.identity

| 字段 | 类型 | 默认值 | 作用 |
|---|---|---:|---|
| name | string | "LLM" | 人设名称。 |
| role | string | "微信群聊助手" | 人设角色说明。 |
| style | string | "自然、简短、像真人微信聊天" | 说话风格说明。 |
| rules | string[] | [] | 额外稳定行为规则。避免与 prompt.txt 重复。 |

#### llm.prompt

| 字段 | 类型 | 默认值 | 作用 |
|---|---|---:|---|
| max_messages | integer | 3 | 单次结构化回复最多允许的文本消息数。 |
| max_emoji_items | integer（最终最多 24） | 50 | 候选表情读取数量；最终提示词最多注入 24。 |
| allow_animation | boolean | true | 是否允许模型选择本地动画/表情。 |
| prefer_short_reply | boolean | true | 系统提示词中偏好短回复。 |
| forbid_markdown | boolean | true | 系统提示词中要求不要 Markdown。 |
| forbid_explanation | boolean | true | 系统提示词中要求不要解释内部推理/格式。 |
| emoji_hint_text | string | "喏" | 表情字段提示文字。 |
| fallback_message | string | "我在" | 强制回复但模型没有产出时的兜底文本。 |
| special_rules | string[] | [] | 追加到系统提示词的固定规则，最多取 20 条。 |
| topic_redirect_rules | string[] | [] | 追加的话题引导规则，最多取 20 条。 |

#### llm.memory

| 字段 | 类型 / 范围 | 默认值 | 作用 |
|---|---|---:|---|
| enabled | boolean | true | 是否启用本地 SQLite 长中短期记忆。 |
| db_path | path string | "data/memory.sqlite3" | 记忆数据库位置。 |
| context_max_chars | integer，≥0 | 0 | 取回记忆的字符预算；0 由调用场景决定。 |
| person_fact_limit | integer，≥1 | 8 | 每次注入的人物事实数。 |
| group_knowledge_limit | integer，≥1 | 10 | 每次注入的群知识数。 |
| candidate_batch_size | integer，≥10 | 30 | 记忆候选检索批量。 |
| short_memory_max_tokens | integer，≥50 | 1000 | 短期记忆 token 上限；超过后优先压缩较早内容并下沉到中长期记忆。 |

#### llm.learning

| 字段 | 类型 / 范围 | 默认值 | 作用 |
|---|---|---:|---|
| enabled | boolean | true | 是否记录、学习群聊风格和高置信度表达。 |
| db_path | path string | "data/llm_learning.sqlite3" | 学习数据库位置。 |
| queue_max | integer，≥100 | 2000 | 兼容字段。消息观察会同步去重并持久化到 SQLite，因此内存队列已满不会静默丢失学习证据。 |
| min_term_count | integer，≥2 | 2 | 确定性统计的最低词项出现次数。 |
| prompt_max_chars | integer，≥400 | 1800 | 注入学习结果的字符预算。 |
| style_card_max_chars | integer，≥600 | 1800 | 风格卡最大字符数。 |
| bot_names | string 或 string[] | [] | 需要识别为 Bot 的额外昵称；wxid 会从 WeFlow 自动合并。 |
| expression_recall_scan_limit | integer，≥200 | 2000 | 召回句式时扫描的候选上限。 |
| expression_pool_size | integer，4–24 | 12 | 句式候选池大小。 |
| expression_selector_enabled | boolean | false | 是否额外调用 LLM 从句式候选中选择表达。默认 `false`：本地排序更快，也不会增加一次额外请求。 |
| expression_prompt_max_items | integer，1–4 | 3 | 每次回复最多注入的本地排序高置信度句式数。它们仅作为可选表达参考，不强制复用。 |
| expression_selector_max_items | integer，1–8 | 4 | 本轮最多选择的候选句式数。 |
| expression_selector_max_chars | integer，400–4000 | 1400 | 候选句式注入字符预算。 |
| expression_eval_enabled | boolean | false | 是否启用句式效果评估。 |
| expression_eval_max_items | integer，1–12 | 6 | 单轮评估的句式数。 |
| slang_emotional_pool_rotation | boolean | true | 是否轮换情绪黑话候选池。 |
| style_switch_enabled | boolean | true | 是否允许一轮主动回复中选定风格/黑话/句式。 |
| style_switch_cooldown_seconds | integer，≥0 | 120 | 风格切换冷却秒数。 |
| idle_content_cache_max_age_seconds | integer，≥900 | 43200 | 闲时内容候选的最大新鲜度（秒）。超过期限的候选不会进入 Planner。 |
| idle_content_prefetch_min_interval_seconds | integer，≥300 | 1800 | 后台刷新内容源缓存的最小间隔（秒）。该参数只影响抓取成本，不构成跨群发送计时器或每日发送预算。 |
| idle_content_prefetch_min_items | integer，1–12 | 6 | 刷新时希望维持的新鲜去重候选数。抓取在群聊周期完成后异步执行，本身不会发送消息。 |
| idle_content_shadow_mode | boolean | true | 闲时内容 Planner 的安全开关。为 `true` 时只记录模型选中的已缓存候选并阻止发送，且不消耗候选；确认群聊节奏与内容质量正常后才改为 `false`。 |

### 5.6 上下文压缩成本参数

| 路径 | 类型 / 范围 | 默认值 | 作用 |
|---|---|---:|---|
| cache_cost_ratio | number，≥1 | 5.428 | 未命中与命中缓存的价格比估计，用于判断压缩是否值得。 |
| cache_hit_rate | number，0–0.99 | 0.85 | 预估缓存命中率。 |
| cache_break_even_horizon | integer，≥1 | 40 | 预计多少次后续请求回收一次压缩成本。 |

## 6. 可选功能设置

这些段可全部省略，所以不会出现在简化样例中。

### mc：Minecraft 状态查询

| 字段 | 类型 | 默认值 | 作用 |
|---|---|---:|---|
| host | host 或 host:port string | 项目内默认服务器 | MC Java 服务端地址，也接受旧别名 server。 |
| port | integer | 空 | 单独指定端口；为空时由 host/默认查询决定。 |
| fake_name | string | "Anonymous Player" | 玩家列表示例中应排除的占位名称。该字段现已实际生效。 |

### rcon：RCON 命令

| 字段 | 类型 | 默认值 | 作用 |
|---|---|---:|---|
| host | string | 无 | RCON 主机。 |
| port | integer | 25575 | RCON 端口。 |
| password | string | 无 | RCON 密码；仅在使用 RCON 插件或锦标赛读取时填写。 |

### comfy：ComfyUI 绘图

| 字段 | 类型 | 默认值 | 作用 |
|---|---|---:|---|
| url | URL string | "http://127.0.0.1:8188" | ComfyUI 服务地址。 |
| output_dir | path string | 空 | ComfyUI 输出目录。 |

### tournament：锦标赛存档读取

| 字段 | 类型 | 默认值 | 作用 |
|---|---|---:|---|
| enabled | boolean | true | 是否运行锦标赛监控。没有相关玩法时显式设为 false。 |
| min_matches_for_ranking | integer | 5 | 进入正式排名所需最少对局数。 |
| leaderboard_top | integer | 10 | 排行榜输出的前 N 名。 |
| rcon_timeout | integer，≥1 | 10 | RCON 读取存档的超时秒数。 |
| usercache_path | path string | 空 | Minecraft usercache.json 路径，用于 UUID 与玩家名映射。 |
| games | object | 内置两种游戏 | 键为游戏展示名称，值为 {"storage":"命名空间","enabled":true/false}；只有同时启用且填写 storage 的项会被读取。 |

## 7. 修改后的检查步骤

1. 检查 JSON 语法：

       python -m json.tool config.json > $null

2. 确认 llm.apis 每一路均有 model、api_key、base_url，且 protocol 正确。
3. 首次运行建议保持 prefix_mode 为 strict，确认 Bot 能收发后再切换 mixed。
4. 不要用真实群消息探测未知配置；先看日志中的 [LLM POOL] 已加载，确认优先级、模型名和协议符合预期。
