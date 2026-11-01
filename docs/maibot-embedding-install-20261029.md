# MaiBot 免费 embedding 接入与验收（2026-10-29）

已通过现有 Kilo 网关实测免费 embedding，并接入 MaiBot 原生任务。生产主进程 PID 15256（10:26:08 启动）未停止、重启或手动重载。

## 模型选择

| 模型 ID | 免费目录信息 | 本轮实际验证 | 接入状态 |
| --- | --- | --- | --- |
| `nvidia/nemotron-3.5-embed-1b:free` | 文本、图片；默认 2048 维，可请求截断维度 | 中文语义排序、重复一致性、1024 维文本、2048 维图片区分、原生服务与存储检索 | 启用 |
| `liquid/lfm-2.5-embedding-350m:free` | 文本；1024 维，512 token 上下文 | HTTP 200、非零有限向量、中文语义排序 | 备选，未加入任务回退 |
| `nvidia/llama-nemotron-embed-vl-1b-v2:free` | 文本、图片；2048 维 | 本轮正式探针验证文本 HTTP 200、向量质量、中文语义排序 | 备选，未加入任务回退 |

实际端点为 `https://api.kilo.ai/api/gateway/embeddings`，本次无需鉴权，所有直接探针响应的 `usage.cost` 均为 0。免费状态和可用性以本次测试时点为准。没有增加付费回退、注册账号或购买配额。

来源：[OpenRouter embedding 模型目录](https://openrouter.ai/api/v1/embeddings/models)、[Nemotron 3.5 模型页](https://openrouter.ai/nvidia/nemotron-3.5-embed-1b:free)、[Liquid 模型页](https://openrouter.ai/liquid/lfm-2.5-embedding-350m:free)、[Nemotron VL 模型页](https://openrouter.ai/nvidia/llama-nemotron-embed-vl-1b-v2:free)、[embedding 接口文档](https://openrouter.ai/docs/api_reference/embeddings)。目录信息和 Kilo 实际可调用性分别验证，未假定目录中所有模型都能经 Kilo 使用。

上述免费试用模型页披露了请求/向量日志留存或用于改进训练等条件；这是云端服务，并非本地离线处理。验收仅发送人工编写的合成文本与程序绘制图片，没有使用真实群聊或群图片。

## 接入配置与修复

`config/model_config.toml` 新增独立 provider `FreeEmbeddingKilo`，以及两个不同任务类型的原生模型别名：

- `embedding` → `free_nemotron_text_embed_1024`，明确请求 `dimensions=1024`。
- `image_embedding` → `free_nemotron_image_embed_2048`，用多模态 `input[].content[].image_url` 承载真实图片，2048 维。

两个任务仅使用同一已验证模型，不跨模型回退，避免同维度但不同语义空间的向量混用。名字不使用壳子的 `wechat_`/`WechatBot_` 生成前缀，已实测 `sync_native_config()` 后仍能保留。原有聊天模型与优先级未改变。

`config/bot_config.toml`：

- 开启 `expression.use_vector_expression`，恢复表达方式意图与向量召回。
- 开启关系向量化及关系回填，保留既有双向量池、图谱与稀疏融合检索。
- 段落向量回填、图片记忆、人物画像与 Episode 等原有开关保持启用；前两项现在具备可用 embedding 后端。人物画像、Episode 内容生成仍依赖聊天 LLM，不宣称本轮已修复其 API 限流。
- 记忆 `dimension_request_mode=always`：原 `explicit` 模式在普通调用中未携带维度，导致适配器探测到 2048，与任务要求的 1024 不一致。现已统一，并验证存储维度与原生任务一致。
- 文本批大小 8、并发 2、启用缓存，以减少重复编码与免费端点压力；端点不可用时保留元数据降级与恢复补齐机制。

`src/A_memorix/core/image/embedder.py`：

- 原探针使用两张 24×24 纯色图，当前检索模型将其编码为高度相似向量，错误判定图片嵌入不可用。改用 192×192 红方形与蓝圆形，保留同图稳定性、不同图片区分、零向量与固定向量拒绝检查，未降低判定阈值。
- 新增显式预处理版本 `png_rgb_v1`，已写入配置：静态 BMP/WebP 等输入统一处理方向与透明背景，转换成端点支持的 RGB PNG 后编码；原资产字节保持不变。预处理版本进入向量指纹，避免与旧空间混用。多帧输入明确拒绝；图片资产系统本身仅支持静态 PNG/JPEG/BMP/WebP，不新增动画检索能力。

向量按模型、维度和预处理指纹隔离保存；没有删除或覆盖旧模型的向量空间。配置变更前备份位于 `temp/embedding-config-backup-20261029/`。

## 验收结果

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| 免费真实端点探针 | 18/18 | [embedding-live-probe-20261029.json](maibot-wechat-acceptance/embedding-live-probe-20261029.json) |
| 原生 embedding 与依赖功能 | 27/27 | [embedding-native-20261029.json](maibot-wechat-acceptance/embedding-native-20261029.json) |
| 微信转译、原生工具及插件桥接回归 | 75/75 | [embedding-bridge-20261029.json](maibot-wechat-acceptance/embedding-bridge-20261029.json) |

原生功能验收使用独立数据库，覆盖中文近义/无关排序、文本维度、图片真实探针、段落与关系向量写入、双池语义召回、指纹落盘、模拟中断后的元数据保留和段落补齐、历史关系回填、图片后台索引、不同文件编码的同图向量检索、聊天范围隔离、10 条表达索引构建与召回、生命周期释放。

直接探针的中文近义句余弦约 0.587，无关句约 0.081；同图余弦 1.0、不同形状约 0.785。原生另一组合成文本近义约 0.720、无关约 0.063。没有仅以 HTTP 200 作为可用判据。

桥接回归继续使用受控聊天 LLM 响应与 B 站/RCON fixture，embedding 使用当前真实免费端点；物理微信发送被拦截。此前失败的 `native.tool.query_image_memory` 本轮通过。该测试证明转译与原生工具接入，不等同于向真实群发送消息的验收，也不证明聊天 LLM 已解除限流。

可复跑：

```powershell
python -X utf8 scripts/maibot_embedding_probe.py
python -X utf8 scripts/maibot_embedding_acceptance.py
python -X utf8 scripts/maibot_bridge_acceptance.py --plugin-source plugins --test-enable-life --test-agent-tools
```

## 生产生效情况

配置监视器已自动加载文本模型配置。只读检查生产活动空间 `dcac29e4ca3cc839607a0fc116f55daaa5a72d61094010cc7d59f6443d87525f`，确认模型为 `free_nemotron_text_embed_1024`，维度为 1024；检查时已有 3 条段落向量和 2 条图谱向量。此处只读取指纹和数量，没有导出记忆正文。

图片探针、格式转换属于 Python 源码修改，运行中的旧进程尚未加载；需由用户手动重启后完整生效。主进程 PID/启动时间保持不变。近期生产日志仍有聊天 LLM 的 429/超时记录，不能把 embedding 成功当作聊天 API 限流已解除。
