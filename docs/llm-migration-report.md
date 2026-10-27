# WeChat Bot → MaiBot LLM Migration Report

The local `dev` branch at commit `5258467` remains the source baseline. The remote `origin/dev` SHA could not be verified because GitHub SSH and HTTPS access were unavailable.

The old `LLMService` import now resolves to `MaibotLLMService`, which starts an embedded `maibot-native-core` thread from the vendored repository-local `src/` package. The runtime owns MaiSaka sessions, planner/replyer, native model routing, memory automation, expression/jargon learning, context compaction and built-in Agent tools. The existing WeChat sender and HTTP payload path remains the only ingress/egress translation boundary.

At startup the repository's legacy `config.json` LLM section is converted to native `config/bot_config.toml` and `config/model_config.toml`; the embedded `src.config.config` then reads those local files. Persona/style, model providers, task routing, visual flags, memory/learning settings and native paths are therefore local MaiBot configuration. Auxiliary plugin calls from `jane`, `emoji`, `lottery`, `fx`, and `persona` use the same local native runtime.

The acceptance script sent a real text request through MaiBot's Kilo model and received `OK`; all outbound message delivery remained intercepted. The report is stored in `docs/llm-migration-acceptance.json`. The same external endpoint can intermittently return HTTP 503 or network errors, so retry logic is part of the facade and external availability remains a deployment concern.

Emoji migration is verified separately: 16 files exist in MaiBot `data/emoji` and 16 registered records exist in `MaiBot.db`.

The only compatibility boundary is the WeChat message ingress/egress translation. LLM reasoning, model selection, prompt assembly, output parsing, memory, learning, context policy, reply timing and native Agent tools run through the repository-local MaiBot core.

Runtime verification now prints `[LLM INIT] ... maibot=True` during Dispatcher startup, `[LLM ROUTE]` at the direct or batch dispatch boundary, and `[MAIBOT CORE]` request/response markers from the facade. The acceptance suite covers direct, auxiliary and batch calls and passed with `MaibotLLMService`; `MAIBOT_INTERCEPT_SEND=1` prevents outbound WeChat sends during tests.

The previous lack of obvious MaiBot startup text was caused by the old facade importing the external `D:\maibot` tree. The corrected startup now prints `[MAIBOT CORE] native_runtime_started thread=maibot-native-core` and MaiSaka logs such as `Maisaka 运行时已启动`, `进入Planner` and `开始思考` directly under the Wechat Bot workspace.
