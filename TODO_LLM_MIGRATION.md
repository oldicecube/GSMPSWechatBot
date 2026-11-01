# WeChat Bot `dev` → MaiBot LLM Core Migration

## Boundary

- Source baseline: local `dev` at `5258467`; remote `origin/dev` cannot currently be verified because GitHub SSH/HTTPS access is unavailable.
- Preserve every non-LLM feature in `D:/Wechat Bot`.
- Replace only the old `llm/` execution path with MaiBot reasoning/model logic.
- Keep exactly one compatibility boundary: WeChat message ingress/egress translation at the adapter boundary.
- Persona, group/private prompts, memory, learning, model selection and Agent Tools must be supplied to MaiBot through its native context/config/tool interfaces.
- Do not import the old `llm/autonomy` loop into MaiBot; it would create a competing planner and change MaiBot behavior.

## Plan

- [ ] Verify remote `dev` SHA when credentials/network are available; record it without modifying source history until verified.
- [x] Vendor MaiBot's `src/` core, native prompts/assets/locales and native config schemas into this repository; runtime no longer imports `D:\\maibot`.
- [x] Add an embedded native MaiBot/MaiSaka runtime and keep the WeChat ingress/egress conversion at the adapter boundary.
- [x] Define an old-dispatcher → MaiBot prompt/session mapping.
- [x] Define MaiBot response text → old sender result mapping, preserving the existing sender/HTTP payload path.
- [x] Replace the dispatcher-visible `LLMService` import with the MaiBot adapter.
- [x] Generate `config/bot_config.toml` and `config/model_config.toml` from the legacy `config.json` LLM settings before importing native MaiBot config.
- [x] Route native MaiSaka chat history, memory automation, expression/jargon learning, context compaction, planner/replyer policy and built-in tools through the embedded runtime.
- [x] Route auxiliary plugin LLM calls (`jane`, `emoji`, `lottery`, `fx`, `persona`) through the MaiBot-backed service facade; no plugin imports `DeepSeekProvider` directly.
- [x] Preserve all non-LLM plugins, `auto/`, services, data stores, RCON and media paths; source diff is limited to the LLM facade, dependent auxiliary plugins, TODO and acceptance report.
- [x] Add a send-independent MaiBot API contract test: prompt `请只回复OK` returned `OK` through `MaibotLLMService` using the configured Kilo model.
- [x] Run startup, MaiBot model request, message facade, persona/config, emoji registry and graceful shutdown tests.
- [x] Add visible `[LLM INIT]`, `[LLM ROUTE]` and `[MAIBOT CORE]` runtime markers so the active core is diagnosable from console and app logs.
- [x] Fix batch-message translation to accept the Dispatcher batch contract and preserve batch context in the MaiBot prompt.
- [x] Allow auxiliary MaiBot calls to accept legacy provider options such as `timeout` without bypassing MaiBot.
- [x] Add `MAIBOT_INTERCEPT_SEND=1` send interception for end-to-end tests without sending to WeChat.
- [x] Verify direct, auxiliary and batch MaiBot calls return through the facade; acceptance result is recorded in `docs/llm-migration-acceptance.json`.
- [x] Update this TODO and write `docs/llm-migration-acceptance.json`.

## External blockers

- [x] Audit the 16 legacy Agent Tools against actual native providers and plugin registrations; see `docs/maibot-agent-tools-migration-20261029.md` for missing, partial and configuration-dependent capabilities.
- [x] Port `manage_scheduled_reminder` into the independent `wechatbot.scheduled-reminder` SDK plugin with trusted requester scope, durable storage and native proactive dispatch. Isolated acceptance passed; production activation awaits the user's manual restart.
- [x] Expose `minecraft_rcon_action` as the independent `wechatbot.restricted-rcon` SDK Tool, retaining existing whitelist/configuration and Host-only credentials with single-use request grants.
- [x] Add `wechatbot.restricted-python` with an allowlisted AST interpreter and Windows child-process resource limits; install and WeChat-adapt upstream `github.erhan111119.mai-repost-shit` without enabling its configuration. See `docs/maibot-agent-tools-install-20261029.md`.
- [ ] Migrate the remaining legacy tool gaps explicitly identified in that audit; preserving legacy source and shell commands alone does not expose them to the native Planner.

- Remote `origin/dev` SHA cannot be confirmed until GitHub authentication/network access works.
- Real WeChat receive/send verification requires a running WeFlow account; outbound tests remain intercepted until explicitly changed.
- External model availability remains a deployment concern; the local native runtime was observed reaching MaiSaka planner and model selection, but configured free endpoints returned HTTP 429/503 during the test window.
