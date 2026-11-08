# Anima / ComfyUI 实际部署审阅

核对日期：2026-11-08（Asia/Shanghai）。只读检查用户指定的 `\\OLDICECUBE_LAPT\oldicecube的笔记本D盘\Anima`，未改动或重启笔记本服务，未下载新模型，未提交生成请求。

## 实际工作流与链路

当前 bot 真正提交的是本仓库 `anima/workflow.json`，并不是共享目录内随便一个 UI 工作流。`plugins/anima.py` 调用 `AnimaService`：模型池转换提示词 → 覆盖 JSON 节点 → POST `/prompt` → 轮询 `/history/{prompt_id}` → 扫本地配置的输出目录 → 发图 / 扣分。`DeepSeekProvider` 是兼容类名，实际模型选择由现有原生桥接负责，不能按名称断定调用了哪个模型。

| 节点 | 当前内容 |
| --- | --- |
| 44 UNETLoader | `anima-base-v1.0.safetensors`，weight_dtype=default |
| 45 CLIPLoader | `qwen_3_06b_base.safetensors`，type=stable_diffusion |
| 15 VAELoader | `qwen_image_vae.safetensors` |
| 11 / 12 | 正、负提示词编码 |
| 28 EmptyLatentImage | 文件为 1024×720、batch=1；实际被提示词转换结果覆盖 |
| 19 KSampler | 文件为 24 steps、CFG 5、er_sde、simple；实际 steps/CFG/sampler 被覆盖，scheduler 不被覆盖 |
| 8 → 47 | VAE 解码 → SaveImage |

提示词转换器实际要求默认 32 steps、CFG 4.3、dpmpp_2m_sde_gpu，常用 768×768、768×1024 或 1024×768；因此只改工作流文件的步数并不能可靠改变实际调用。共享目录的三个文件都读取了 safetensors 文件头：均为 BF16；扩散权重约 4.18 GB、文本编码器约 1.19 GB、VAE 约 0.25 GB。实际显存还包含运行时张量，不能用文件总和判断显存是否足够。

笔记本 `comfyui_version.py` 为 **0.22.0**。官方示例 PNG 的实际嵌入图也使用 Qwen3 编码器、`CLIPLoader.type=stable_diffusion` 和 EmptyLatentImage，因此不能仅因名字看起来像 SD 就误改为 Flux / Qwen 类型。官方比较工作流也保留同样的编码器组合。

## 已核实的更新

官方 Hugging Face 模型目录审阅 revision：`f973fc41ec7545364ac9776c2440285f43ff2a30`。

| 权重 | 官方目录上传日期 UTC | 定位与建议 |
| --- | --- | --- |
| Anima Base v1.0（已装） | 2026-05-14 | 保留作为回退，偏风格灵活性与多样性 |
| Anima Aesthetic v1.1（未装） | 2026-07-13 | 优先比较画面稳定性；切换时去掉 score_*，不要沿用 Base 的全部前后缀 |
| Anima Turbo v1.1（未装） | 2026-08-24 | 最值得先试的群聊速度档；官方 Turbo 指引为 CFG=1、8–12 步 |

两个 v1.1 扩散文件均为 4,182,230,656 字节；是同一模型族的不同用途分支，不能简单把 Base 换成 Turbo 文件却仍跑 CFG 4.3 / 32 步。Aesthetic 与 Turbo 可复用现有编码器 / VAE，但实际加载及视觉质量仍需本机 A/B 验证。官方 README 的部分说明仍写 v1.0，v1.1 文件存在与日期另以文件树和提交信息确认；未假定新版本一定全面更好。

官方 ComfyUI release 当前为 **v0.44.0，2026-11-04 发布**。建议先复制现有环境 / 记录依赖并检查自定义节点，再升级测试；当前环境已有原生 Anima 支持，没必要为新权重盲目整包覆盖。

## 优先优化项

1. **先修任务归属，避免错图。** 现有 `wait_for_generation_polling()` 到 60 秒仍返回成功，`generate()` 又无条件扫描最新 PNG，不验证当前 prompt_id，也不检查 history 的执行失败状态。忙时可能把旧图当本次结果发送并删除。应只接受当前 history 中成功 SaveImage 节点的输出，经 `/view` 下载到 bot 临时目录；超时应保持待确认，不读“最新图”替代。这个问题独立于换模型。
2. **打通实际服务地址。** 当前 `config.json` 没有 `comfy` 节，服务会落到本机 `127.0.0.1:8188`，且 output_dir 为空。此次本机及笔记本解析地址的 8188 HTTP 都不可达，故无法确认实际 GPU、显存、采样器枚举或生成耗时。SMB 可读不代表 ComfyUI API 在线。
3. **将采样参数从 LLM 输出中分离。** LLM 只输出正负画面描述和受控画幅；Base / Aesthetic / Turbo 由服务端固定档位决定模型、CFG、步数、采样器、scheduler，验证有限数值、画幅与最大像素、seed 范围及真实节点枚举。现在 JSON 只做类型转换，未限幅，也容许模型随意给 sampler 名，容易生成 400 或资源异常。
4. **群聊速度档先试 Turbo。** 固定同批 prompt、seed、尺寸和 batch=1，以 10 steps / CFG=1 / euler / simple 为初始候选；euler 来自官方说明的兼容推荐，最终以本机枚举为准。Base 用 32 steps / CFG≈4.3 / er_sde 或现有 dpmpp 作为对照。记录排队、文本编码、采样、VAE 和总时间，不能从步数比例直接宣称真实提速倍数。
5. **质量档试 Aesthetic。** 先保留 768×1024 / 1024×768，与 1024×1024 比较；不要为了“高质量”直接加倍长宽。当前强制全 tag 可以放宽为 tag + 具体自然语言布局描述；文本本身、多人关系等描述比反复堆质量词更有价值。模型版本与提示词前缀应一一对应。
6. **避免频繁冷启动。** 笔记本 watchdog 用 600 秒空闲阈值，启动 ComfyUI 默认丢弃 stdout/stderr；重新加载数 GB 权重会影响偶发调用延迟，也不利于排查 OOM。先记录冷 / 热启动耗时，再决定延长空闲保留或仅释放模型；无显存 / 系统负载数据时不直接开启高显存模式或强制量化。
7. **积分与结果一致。** 当前插件不仅失败也扣 8 分，而且发送函数返回 False 时仍可能报生成成功并删文件。应把“生成完成”“发送成功”“扣分成功”分开记录，再按明确业务规则结算。本轮用户仅要求研究 Anima，所以没有擅自调整其收费政策或远端模型。

## 可执行验收顺序

恢复笔记本 API → 只读 `/system_stats` / `/object_info` 核对设备与节点 → 备份环境 → 下载并校验单个候选权重 → 手动 A/B（相同 prompt/seed/尺寸）→ 验证超时、失败、错误 prompt_id 不回旧图 → 验证双端发送与扣费 → 再决定默认档位。当前没有真实生成性能结果，不能声称模型升级或加速已验收。

## 原始来源

- 官方模型卡：`https://huggingface.co/circlestone-labs/Anima`（读取 README 与文件树 API）
- 官方示例：该仓库 `example.png` 的 prompt 元数据、`anima_comparison.json`
- 官方更新：`https://api.github.com/repos/Comfy-Org/ComfyUI/releases?per_page=5`
- 本机证据：`anima/workflow.json`、`anima/anima_service.py`、`plugins/anima.py`
- 共享目录证据：ComfyUI 版本文件、模型文件头、watchdog 启动逻辑；未发布任何凭据或用户提示词。
