# WeChat Bot → MaiBot LLM Migration Report

The local `dev` branch at commit `5258467` remains the source baseline. The remote `origin/dev` SHA could not be verified because GitHub SSH and HTTPS access were unavailable.

The old `LLMService` import now resolves to `MaibotLLMService`. It creates MaiBot's `src.services.llm_service.LLMServiceClient`, uses MaiBot model task resolution/provider selection, and extracts MaiBot output items into the old dispatcher's existing `messages` result. The existing WeChat sender and HTTP payload path remains outside this change.

Persona and style are read from MaiBot's native `bot_config.toml` when available, with old WeChat identity/style values retained as fallback. Group and private prompt text are passed into the MaiBot prompt context. Auxiliary plugin calls from `jane`, `emoji`, `lottery`, `fx`, and `persona` are routed through the MaiBot facade.

The acceptance script sent a real text request through MaiBot's Kilo model and received `OK`; all outbound message delivery remained intercepted. The report is stored in `docs/llm-migration-acceptance.json`. The same external endpoint can intermittently return HTTP 503 or network errors, so retry logic is part of the facade and external availability remains a deployment concern.

Emoji migration is verified separately: 16 files exist in MaiBot `data/emoji` and 16 registered records exist in `MaiBot.db`.

The only compatibility boundary is the WeChat message ingress/egress translation. LLM reasoning, model selection, prompt assembly, output parsing, and auxiliary model calls run through MaiBot.
