# 免费渠道筛选与 patpat 修复

## 当前结果

patpat 已修复，专项与消息桥接回归通过。API 部分完成了渠道调查、可达性验证、优先级配置和接入脚本，但缺少个人密钥，**没有新增成功通过认证调用的 Gemini／DeepSeek 路由**，不能把禁用的预配置称为已经可用。生产 bot 未手动重启，未向实际群发送验收消息。

## 免费渠道调查

| 渠道 | 官方条件与本次证据 | 处理 |
| --- | --- | --- |
| [Google AI Studio](https://ai.google.dev/gemini-api/docs/pricing) | Gemini 3.8 Flash 有免费层，需个人密钥；官方 [OpenAI 接口](https://ai.google.dev/gemini-api/docs/openai) 可复用现有客户端。无认证实测 400，提示缺少有效 Authorization | 预配置优先级 1，等待免费项目密钥 |
| [NavyAI](https://api.navy/) | 免费账号标示 150K tokens/day、20 RPM；[模型接口](https://api.navy/docs/models) 实测 200，DeepSeek V4.1 Flash、Gemini 3 Flash Preview 均标记 premium=false，且支持视觉与工具。无认证聊天实测 401 | 两条路由预配置优先级 2、3，等待密钥；第三方服务的可靠性尚未经过认证或持续调用验证 |
| [Token Harbor](https://tokenharbor.ai/faq) | 提供动态变化的 :free 模型，需个人密钥；无认证实测 401；目前未验证账号内免费模型清单 | 不猜测免费模型 ID，不启用 |
| [Electron Hub](https://www.electronhub.ai/pricing) | 免费账号每周 $0.25 额度；每日免费模型请求需广告奖励。公开目录有 DeepSeek V3.2 :free，标记无视觉。聊天实测 403 | 未启用，缺密钥且网络请求受阻 |
| [Api.Airforce](https://api.airforce/docs/api/models/) | 免费变体必须以当前目录为准；目录可访问，本次未找到 Gemini／DeepSeek 的 :free 变体。无认证聊天 401 | 未启用，不能把普通收费模型误当免费模型 |
| [A4F](https://www.a4f.co/docs/quickstart) | 需账号密钥，免费目录请求未取得可用 JSON；聊天实测 521 | 未启用，当前不满足稳定接入条件 |
| [Infron](https://infron.ai/docs/overview/free-models) | 目录有 DeepSeek V4.1 Flash :free，但标准免费模型要求账户至少 $5 余额，1000 次/天共享；无认证聊天 401 | 有充值门槛，未替用户充值或启用 |
| [LLM7](https://docs.llm7.io/quickstart) | 当前文档要求个人 token；公开目录中本次查到的 Gemini／DeepSeek 均为 pro/usage-based 路由 | 不按过时的匿名免费接口配置 |
| [Routeway](https://docs.routeway.ai/getting-started/billing) | 当前计费文档中免费请求与订阅计划相关；聊天实测 403 | 未启用 |
| Kilo / OpenRouter | 重查公开目录，Gemini／DeepSeek 条目均为非零价格 | 保留已经验收的 Kilo 视觉模型为低优先级后备，不调用收费条目 |
| NVIDIA | 用户明确反馈不可用 | 禁用并降至优先级 900 |

无认证测试只能证明网络和鉴权行为，不能证明测试载荷中的候选模型可用。相应 JSON 已明确标记 model_validated=false。

## 配置与接入工具

- `config.json`：新增 Google Gemini 3.8 Flash、Navy DeepSeek V4.1 Flash、Navy Gemini 3 Flash Preview 的禁用预配置，优先级 1–3；其他模型保持 100 以后。NVIDIA 禁用。既有可用路由保留，避免把无密钥路由当作已验证服务。
- `scripts/configure_preferred_free_apis.py`：无参数只预配置；指定 provider 与本地密钥文件后，先验收 Planner JSON、函数调用、工具结果回传、图片识别，全部通过才写配置并同步原生配置。失败保持原文件；Navy 会再次检查模型仍对免费计划开放并支持视觉和工具。
- Google 密钥应来自免费层项目；付费项目密钥不能当成已确认免费的额度。脚本不创建账号、不充值、不使用网上共享密钥。
- MaiBot 原生客户端已有工具 extra_content 传递逻辑；实际 Gemini 思考签名和完整 Host 调用验收仍须有效密钥验证。

```powershell
python scripts/configure_preferred_free_apis.py --provider google --key-file C:\private\gemini.env
python scripts/configure_preferred_free_apis.py --provider navy --key-file C:\private\navy.env
```

文件可包含原始密钥或对应的 `GEMINI_API_KEY=...` / `NAVY_API_KEY=...`。也可用同名进程环境变量。完成认证验收后继续跑原生 Host/SDK 测试，最后由用户手动重启。

## patpat 根因与修改

1. `auto/patpat.py` 的 `_match_prefix` 原来定义在 `handle_auto()` 内，`allow_llm()` 在作用域外调用会触发 NameError。已提升为共享函数。
2. 实际近期消息 localType 为 `62 << 32 | 49`（266287972401）；部分显示正文仅为“拍一拍”，真正的目标在原始 XML `appmsg/patinfo/pattedusername` 中。旧插件忽略该字段。
3. 新增 `utils/wechat_pat.py`，优先解析原始 type 62 功能字段，核对本机真实 wxid；忽略 bot 自发拍一拍，避免自回复。限制 XML 大小，拒绝 DTD／实体声明，不把普通文本消息里粘贴的 XML 当系统事件。
4. `core/weflow_client.py` 恢复拍一拍标题和真实发起者，保留原始 XML，将其标记为系统事件、不学习、不作为正常提示词正文；既有数据结构兼容。
5. 文本兼容保留，但仅匹配第一次“拍了拍”后的直接目标。允许引号闭合后的相邻自定义后缀；拒绝更长名字、旁观者或后缀中第二个“拍了拍”造成的误触发。
6. `core/router.py` 修复严格前缀模式提前返回，使声明 MATCH_RAW_MESSAGE 的插件可以接收无命令系统事件；该分支不标记主动 LLM 候选，正常聊天仍遵循前缀模式。群功能开关与允许列表继续生效。

## 测试与限制

- `scripts/test_patpat_contract.py`：10/10，通过 XML ID、昵称变化、正文缩略、实体/无关 XML、后缀误匹配、自回复排除、严格模式、群开关与 native observe_only 防重复回复。
- 真实近期原始消息回放：拍其他 bot 正确不触发；同一真实 XML 格式仅替换目标为本机的合成回放可正确触发。没有冒称已在线向群友发送测试消息。
- `scripts/test_business_prompt_contract.py`：12/12。
- 完整消息桥接验收：94/94，内含既有 53 项消息转译单元测试，以及 Host/Runner、沙箱、主动生活、项目进度和群消息结构验收。报告为 `docs/maibot-wechat-acceptance/free-api-patpat-bridge-20261029.json`。
- 接入脚本隔离测试：4/4（验收失败不改配置、通过后优先启用、人设保留、拒绝收费 Navy 模型），并完成预配置幂等及优先级检查。
- 最后一次 XML 消息类型限制和 text 字段一致性修改后，重新运行 patpat 专项 10 项通过；桥接验收验证的是该小修改前的完整链路。
- 近期运行日志仍可见“请求过于频繁”错误。运行进程尚未由用户重启；禁用预配置不会自动消除当前 API 配额问题。
- 缺少账号密钥：尚未验证新渠道的真实认证调用、实际免费额度、延迟、连续稳定性及完整原生 Host 模型兼容性。
