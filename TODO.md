# Wechatbot 开发任务 TODO

## 2026-10-23 Business acceptance (in progress)

- Encoding checkpoint: inspected prompt/provider/learner/memory Python and TODO files are strict UTF-8 without BOM. Use apply_patch for Unicode source; never pipe CJK source through PowerShell's default input encoding. Validate UTF-8/no U+FFFD and py_compile after changes. Recheck this rule at every acceptance checkpoint.
- Preserve the user's uncommitted tests/ deletions. New opt-in synthetic acceptance scripts live under scripts/ and persist complete real API inputs, outputs, HTTP statuses, returned model IDs and production parser results under ignored output/.
- [~] Re-run learning, persisted resource recall, person-memory native tool round-trip and speaking/silence decisions with valid Chinese input. Prior question-mark-corrupted inputs and string reply_to responses do not count as successful behavioral acceptance.
- [x] Specify the integer-array reply_to contract in the batch prompt; keep strict parsing rather than guessing message ID mappings.
- [x] Filter prompt_visible=false records in final structured history rendering, not just its intermediate placeholder.
- [x] Align expression-learning prompt contract with storage validation: require current human source_ids and conservatively derive them only from literal visible evidence when the model omits the field.
- [x] Re-run isolated live acceptance through Kilo: HTTP 200 transport, real style-card output, slang persistence/reuse, short-memory recall, person-profile tool lookup, reply/silence decisions, and integer-array reply_to parsing verified. Expression extraction was observed in a live round; the post-fix repeated rounds did not always choose a new expression, so no new expression persistence is claimed from those rounds.
- [~] Keep observing provider cache telemetry across production traffic; isolated Kilo acceptance reached high reuse on repeated stable sections but mixed first-use/changed-tail requests remain provider-dependent.
- [x] Make proactive low-information participation more positive: release single short social turns earlier, allow one optional ultra-short teasing reply from a matching sentence pattern or slang, and block the same reusable wording from the previous two Bot replies.
- [x] Live acceptance after this change: 11 Kilo requests returned HTTP 200; a non-direct low-information round returned a short social reply with a recalled slang candidate, while direct factual, addressed-to-other, closed-topic, technical, memory, and person-profile rounds retained expected behavior.

- [x] 1. MaiBot 风格的适时 @ 与群成员身份验证
- [x] 2. 自我认知、系统消息、prefix 与 Bot 回显来源隔离
- [x] 3. 引用来源人与引用正文的独立上下文
- [x] 4. 按群聊密度积累和批量请求 LLM
- [x] 5. 首选 API 在线验收：连通性及实际业务提示词请求已通过
- [x] 6. LLM 模块调用路径检查
- [x] 7. 情感化和无厘头回复规则
- [x] 8. 黑话/句式轮换和近期重复反馈
- [x] 9. 本次修复结束后停止旧版 Bot 进程并核验（main.py 与自动重启脚本均已停止）
- [x] 10. 学习与长期记忆的消息来源边界
- [x] 11. AI 插件在线验收：真实模型完成增删查改，13 次工具调用成功；中文/Emoji/别名路由与删除均通过
- [x] 12. 按群聊氛围允许适度粗口和互相调侃
- [x] 13. 业务缓存在线验收：首选 Responses 显式边界；追加 5/20 条分别 93.58%/89.57%，相同请求 99.996%；长期运行继续观察
- [x] 14. 修复 AI 插件编码、开发指南、校验、修复预算、命令路由及负面测试

## 2026-10-20 复核结果

- 不再把短 Prompt 连续重发的高命中率当成长期业务验收。
- 前序新增代码中的中文问号已修复；新增开发规范和错误码采用 ASCII，中文源码/消息通过 UTF-8 字节测试。
- 插件使用受限 AST 解释器，不再 exec 模型源码；循环/集合/递归/文本有资源上限。
- 共 120 项回归测试通过，包括跨调度周期前缀、共享嵌套资源限制、Responses 推理参数映射和配置缓存 TTL 读取。
- 完整 4079 条业务历史回放，请求字符量降低约 57.1%；追加 20 条后相同字符前缀约 88.6%，该数值不是在线缓存命中率。
- 早期首选 API 返回 API_KEY_DISABLED，用户恢复后已完成在线验收，并将首选协议改为经验证的 Responses；密钥与其他端点保持原样。
- 旧 Bot 及自动重启脚本已停止，WeFlow 保持运行；修复需要下次正常启动时加载。

## 2026-10-20 在线验收进度

- 首选 API 已恢复；真实模型插件 CRUD 验收通过，验收文件位于临时目录且已清理，无微信消息发送。
- Chat 对照请求的缓存比例约 36% 至 39%；Responses 显式边界测试后，追加 5 条为 93.58%、累计追加 20 条为 89.57%、相同请求重发为 99.996%（均按网关 usage 计算）。
- Responses 和 Chat 均完成真实模型插件 CRUD；Responses 13 次开发工具调用成功，17 次模型请求，中文/Emoji/UTF-8/别名路由/删除清理全部通过。
- 修复 Responses 错传 reasoning_effort 导致 SDK TypeError；新增 Responses 请求/用量诊断和配置 TTL 保留。
- 本次仅完成有限序列在线验收；窗口换档、上下文压缩、工具定义变化及长期空闲后的冷缓存仍应持续观察。
- Bot 保持停止，未向微信发送测试消息，未提交或推送。

## 首选 uuapi 图像与最低推理配置

- [x] 第一个 uuapi 开启 supports_images，端点独立设置 reasoning_effort=low，全局 high 与其他 API 保持原样。
- [x] 真实图像在线识别通过；新增端点推理覆盖、继承与配置读取测试通过。

## 2026-10-21 生产缓存与学习系统审计
- Operating rule: review and update this TODO at each progress checkpoint. Hand-edited text files use UTF-8 (no BOM unless the existing file has a documented different encoding); validate strict UTF-8 decoding and absence of replacement characters after writes. For live API acceptance, prefer the uuapi Grok 0.08 / Grok-4.7 endpoint and record the endpoint/model that actually answered. Never label local construction tests as online acceptance.

- Status key: [~] investigation verified, code fix or production acceptance still pending.
- [~] 15. Cache audit: stable per-record history, volatile current-turn separation, learning-unit body deduplication and record-local compression weights implemented. Grok-4.7 Chat repeat reached 99.87%; append reached 53.41%, so Chat append reuse remains below target and primary Responses production observation is still pending.
- [x] 16. Slang/expressions/person-profile responsibility audit: person profiles are on-demand via lookup_group_memory; expression writes require current human source_ids; slang feedback exposes use timing/frequency guidance; expression injection/use counters are separated.
- [x] 17. Prompt injection verified locally and with Grok-4.7 using anonymized current-group context; combined reply returned valid JSON. Production raw transcript was not printed or stored in reports.
- [x] 18. LLM-owned human-reply style/timing card implemented separately from Persona: LLM writes abstract natural-language guidance only, concrete expressions/slang stay in their own stores, and the card is filtered to 900 characters.

### 2026-10-21 Audit Findings

- Runtime trace has 3,607 cache-request events but 96 correlated usage events; request records include per-endpoint fallback attempts, not 3,607 distinct replies. Of 96 usage rows, 50 report cache telemetry unavailable. Missing cache tokens are unknown, not zero.
- Matched normal-reply usage is only 5 rows (10.83% weighted cache reads on that small sample). Pre-compression curation has 13 matched usage rows, only 6 with cache telemetry (2.66% weighted on telemetry-known input). Context compression has one matched usage row at zero cached tokens. These are successful-request samples, not an overall lifetime rate.
- The attached 2026-10-21 billing rows at 19:48:48 and 19:49:02 map exactly to production telemetry: pre-compression curation used 602,986 input tokens, then context compression used 241,238 input tokens. Unlabeled dashboard token subcolumns do not support an aggregate cache-rate calculation.
- Prompt defect: curation/compression render history as an aggregate message. Pre-compression curation also serializes learning units built from the same records, duplicating message text. A synthetic 100-record production-builder test measured 2x duplicated source text and 2.833x total curation wire characters vs raw text. Appending a record rewrites the aggregate message, unlike normal replies' per-record history.
- The density gate is already burst-limited, so scheduling is not the sole cause. Per-model request events show endpoint-pool attempts; without correlated provider errors, the trace cannot explain every failed attempt.
- Learning paths are connected: safe contextual slang/scene candidates, bounded expression recall, person-scoped facts, and a persisted style card reach reply prompt sections. Static review found a duplicated ASCII question-mark check that misses Chinese `？` in slang scene intent, and expression repetition suppression compares concatenated situation/pattern/examples to recent replies, making matches unlikely. Person-profile retrieval has no output character cap.
- Isolated live style check used `WechatLLM-Grok0.08-3 / grok-4.7`, synthetic records and a temporary SQLite store. The initial request timed out; the 1,000-character-budget retry returned `那就去摸鱼吧` but omitted the card, so it is not a card test. With the production 1,800-character budget, the card was injected and the model returned `这波稳了！今晚可以好好摸鱼放松了。` (3,179 input tokens; 1,408 cached), without a canned-laughter prefix. No production transcript, learning database, or WeChat delivery was used.
- User clarified: external Persona is a separate plugin and out of scope. The LLM-owned card must learn style and reply timing from human messages only, never persist concrete vocabulary/phrases, and remain a bounded abstract reference.
- Implementation is now authorized: preserve transcript storage, task triggers and queue order; change only prompt construction/learning representation and bounded retrieval. No production history rewrite or background-job reordering.

### 2026-10-22 LLM reply-system acceptance in progress
### 2026-10-22 LLM reply-system acceptance in progress

- Operating checkpoint: source/config/Markdown files inspected for this change are UTF-8 without BOM. Writes use explicit UTF-8/no BOM and LF; after each write, strict UTF-8 decoding, no U+FFFD, py_compile, and targeted tests are required. Do not use PowerShell default text encoding for source edits.
- [~] 19. Current audit found real prompt corruption in `llm/prompt/prompt_builder.py` (literal question-mark replacements), a direct-use conflict for attention-period slang, and a deterministic numeric style summary being rendered into the live style reference. Prompt/style fixes are in progress; online UUAPI Grok-4.7 acceptance is still pending.
- [x] 20. Added regression coverage for abstract style-card filtering, sentence-priority/slang-permission rules, sparse emoji/repetition guidance, and style prompt separation from numeric/persona data.

### 2026-10-22 Final verification checkpoint
### 2026-10-22 Final verification checkpoint

- [x] 19. Fixed literal `????` prompt corruption, made attention-period slang permission-only, separated the LLM-owned abstract style card from deterministic numeric group statistics and Persona, and added sentence-priority/emoji/redundant-function guidance.
- [x] 20. Added focused style-learning maintenance prompt, wired it into cycle curation, added abstract-card filtering and same-line redundant-question normalization. Regression suite reached 133 passing tests.
- [x] 21. Live UUAPI acceptance used `WechatLLM-Grok0.08-3 / grok-4.7`: focused style learning returned and applied an abstract card in a temporary SQLite store; production-shaped reply returned a real JSON reply. No Bot start, WeChat send, or production DB write.
- [~] 22. Live Chat/Grok cache boundary improved after preserving a fixed prefix, shrinking the volatile tail, removing duplicated batch bodies, and suppressing repeated target bodies already present in history. The latest real run reported cold/first-append cache reads of 192 tokens, a later repeat of 53,632/53,665 (99.94%), and one subsequent timeout; provider route/cache warm-up is still unstable, so the 85% production target is not marked complete.

### 2026-10-21 Post-fix Verification

- 126 regression tests passed. Tests are tracked source now; `__pycache__` remains ignored.
- Anonymized current-group style learning through `WechatLLM-Grok0.08-3 / grok-4.7`: 100 human records, 2,827 input tokens, 192 cached tokens; the style-only request reported 2,827 prompt tokens in the saved report. The model returned an abstract card with tone, attitude, speaking method, reply timing and silence timing; concrete legacy fields were removed.
- Combined production-shaped prompt through the same endpoint used a 225-character normalized card, 12 anonymized history records, 5,525 prompt tokens and 5,504 cached tokens; output was valid JSON with one context-appropriate reply.
- Fresh Chat cache probe through the same endpoint: cold request timed out, repeat request cached 15,808/15,828 tokens (99.87%), append request cached 8,384/15,697 (53.41%). The cache structure is improved, but Chat append behavior remains endpoint-limited/insufficient.
- No Bot process was started, no WeChat message was sent, and no production learning database was written by these acceptance probes.
- Prompt-builder recovery used an unreachable Git blob after an accidental empty-file write; UTF-8/no BOM/AST validation passed before continuing.
