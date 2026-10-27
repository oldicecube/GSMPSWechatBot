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
- [x] Add a local MaiBot core runtime import boundary and an explicit WeChat message adapter: `llm/core/maibot_service.py`.
- [x] Define an old-dispatcher → MaiBot prompt/session mapping.
- [x] Define MaiBot response text → old sender result mapping, preserving the existing sender/HTTP payload path.
- [x] Replace the dispatcher-visible `LLMService` import with the MaiBot adapter.
- [ ] Map old persona, group/private prompts, reply style, memory and learning configuration into MaiBot config/context.
- [ ] Map old tools into MaiBot native Tool Providers; keep only message transport compatibility in the adapter.
- [ ] Preserve all non-LLM plugins, `auto/`, services, data stores, RCON and media paths.
- [x] Add a send-independent MaiBot API contract test: prompt `请只回复OK` returned `OK` through `MaibotLLMService` using the configured Kilo model.
- [ ] Run startup, message ingress, planner/reply, tool call, persistence and shutdown tests.
- [ ] Update this TODO after each milestone and write `docs/llm-migration-report.md`.

## External blockers

- Remote `origin/dev` SHA cannot be confirmed until GitHub authentication/network access works.
- Real WeChat receive/send verification requires a running WeFlow account; outbound tests remain intercepted until explicitly changed.
