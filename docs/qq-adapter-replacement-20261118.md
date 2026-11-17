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
