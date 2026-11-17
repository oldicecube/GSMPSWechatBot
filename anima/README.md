# Anima 多模式使用说明

默认继续使用 Anima Base。格式为第一行画面描述，回车后只识别功能选择器；未知内容和执行参数丢弃。默认文生图。多个选择器时使用第一个；包含停用选择器时提示已停用。

```
/anima 初音未来在雨夜街头，水彩风格
```

```
/anima 把图片中的外套改成红色
图生图
```

可用选择器：文生图、图生图、画师混合、放大、超分。局部修正、局部重绘、色彩参考、控制重绘均已停用。画师混合需要安装 Anima Artist Mixer；超分需要配置放大模型。单画师风格直接使用文生图。

图片模式随后发送图片或动画表情；动画仅取第一帧，输出静态图片。参考图按会话和用户隔离，默认等待120秒；新命令或非图片的下一条消息取消等待。输入 /anima 取消 可取消等待。

用户不配置执行参数。LLM根据内容选择30–40步、CFG 4–5、图生图降噪0.3–0.7、放大倍率1–2和方形/横向/纵向；程序对返回值做边界校验，异常使用30步、CFG4.3、降噪0.45、倍率2等默认值。种子每次随机。文生图画幅按参考工作流取约1MP：竖832×1216、横1216×832、方1024×1024；图生图使用预处理后的参考图尺寸。质量词与负面词采用参考工作流推荐模板（质量串含 score_9/score_8、absurdres、very aesthetic、extremely detailed；负面含 score_1~3、lowres、censor、sepia、shiny skin、artist name），不再使用旧的 masterpiece/score_7 三词组合。提示词遵守用户意图，仅添加有依据的细节。抽象请求使用质量词、原文与英文翻译兜底；不合规请求拦截。

已配置远端映射输出目录 H:\Anima\ComfyUI\output，通过ComfyUI本次prompt_id的history定位文件，不扫描最新文件；映射不可访问时使用服务器/view接口。系统只清理下载副本，不删除映射目录的原文件。

离线测试与节点预检不等于真实GPU出图验收。目标环境需重新加载机器人插件；插件与模型安装后需重启ComfyUI。源工作流及历史分析见 reference_manifest.json 和 workflows/，其中停用模式图仅作参考。

## 按题材的尺度引导

人物不再默认写入 fully clothed / modest clothing / neutral pose 等固定正面限制。普通、保守或不明确题材仅加少量负面引导（unrequested revealing clothing、sexualized pose、underwear、lingerie）；明确成人浪漫题材不加这些词，保留非露点服饰及正常亲密互动，尺度交给模型与随机种子。年龄明确未成年或不明时不得性化。露点、裸露生殖器、明确性行为始终禁止。

风格与文字稳定性：默认二次元插画（未指定画师或媒介）额外加 monochrome、greyscale、sketch、lineart 负面词，避免漂移成黑白线稿；用户明确要求黑白/线稿/素描时自动不加。未要求画面文字时默认加 text、watermark、signature，避免生成签名水印。这两组都只做负面约束，不改正面提示词，也不替代事实核验。

生成后复用bot已配置的VLM做独立视觉审核；未通过、无法判断或服务不可用均不返回图片给发送端。这是机器审核，不是绝对无误的保证。

## NewBie Formatter integration (2026-11-17)

Pinned MIT upstream: SuzumiyaAkizuki/ComfyUI-NewBie-LLM-Formatter,
revision `624ccf04cc05cf902b42525074aa9e08561ad8e8`. GitHub HEAD checked on
2026-11-17 and matches the pin. Original vendored sources and license are unchanged;
`vendor/newbie_formatter/provenance.json` records reuse and deferred mechanisms.

The runtime reuses Anima Hybrid format loading, the Low assembly template,
`output_instruction`, strict `parse_envelope`, `get_system_additions`, and the
upstream search/related/artist-profile tools through bounded transport.
The NSFW/jailbreaker agent role and bundled few-shot characters are NOT activated.

Stage boundaries:
1. Planning moderates the request, translates all explicit details and chooses bounded
   execution metadata. A rejection must quote the offending phrase from the request
   (`rejected_reason_quote`); a bare verdict is re-asked once, and ordinary nonsexual
   minors, clothing, actions and settings are explicitly non-offending. Source-linked
   visual facts must quote this request and fix the intended sense of ambiguous terms.
2. Scoped public retrieval distinguishes identity evidence from candidate related tags
   and artist style references. Optional native bot web search also accepts public concepts.
3. Assembly sees visual facts/evidence only, NOT generation settings. It returns only
   the upstream `xml_out`/`text_out` envelope. Unspecified staging is left unconstrained.
4. Semantic audit checks both tag and prose fidelity, including exact numeric ages.
   Every audit issue must carry a verbatim quote from the current draft; the program
   drops unquotable complaints, so a weak model cannot invent defects in text that is
   not there. Up to two bounded repairs are allowed, each independently rechecked, and
   a correction that drops a stated fact is rejected. If no usable correction survives,
   the run falls back to the original text plus faithful translation. No appearance-word
   clipping is used.
5. Recovery can only moderate/translate: extra formatter/tags/metadata fields cannot
   cross that boundary. The final bot-facing return format remains unchanged.

Output screening retains existing rules and configured VLM selection. A technical
review error is not a content violation. Ordinary nonsexual minors and non-explicit
adult romance are permitted; prohibited visible content remains blocked.

Deferred on purpose: global incremental baselines (cross-user contamination risk),
automatic artist selection (not user-requested), additional Medium/High tool loops
(latency), opt-in reference-image prompt reversal (needs separate attribute ownership),
and MOSS layout/editor/compiler (different model format).

Live test scripts under `D:\Anima\research` can use a test-only HTTP adapter to
current enabled utils/VLM endpoints without loading the broken native DB runtime.
This does not modify production model settings, bypass moderation/review, test native
MCP hosting, or send messages to groups. See per-run traces rather than treating unit
regressions or successful image generation as proof of visual accuracy.
