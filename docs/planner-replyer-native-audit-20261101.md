# Planner / Replyer native contract audit

Audit date: 2026-11-01 (Asia/Shanghai). Production processes were not restarted.

## Verified upstream

- Official repository: `Mai-with-u/MaiBot` (the `MaiM-with-u` address redirects to it).
- Vendored version: **1.3.15**. Local import commit: `0541dbc8090b49a3924d80e816e19a4be9a9b52d`.
- Verified native reference: `baf2bc02e723078d108fafbb3353f69c8929f2bc`.
- At import, `reply.py`, `reasoning_engine.py`, `chat_loop_service.py` and
  `maisaka_generator_base.py` match that official reference byte for byte.
- Official main observed at audit: `c6429547af711555e3d4d1b8d04b413572cb616c`
  (1.3.19). It adds passive reply supplementation and other features.
  This branch does **not** claim to be a full 1.3.19 upgrade or an identical fork.

## Native execution contract

1. Planner selects actions/tools and provides `reply_reference` with factual context.
2. `execute_reply` always calls the native replyer manager and
   `generate_reply_with_context`, including expression selection and reply hooks.
3. Only the Replyer's generated final text enters native post-processing.
4. Native post-processing creates independent message sequences, with attachments.
5. The send loop delivers them in order to the current platform stream and records
   each external message receipt. Partial delivery retains the successful receipts.
6. Replyer failure returns failure. Planner draft text is never a fallback body.
7. Normal, passive and Focus entry points continue to use their native protocols.

The local `reply.messages` direct-send extension violated steps 2–3. It has been
removed from both `reply` and `set_reply_options` schemas and from the execution
path. Legacy `messages` arguments in model history are ignored. This is a native
contract restoration, not a second draft-rewrite mechanism.

Multi-message delivery remains available through native response segmentation.
The ordinary/passive/Focus Planner templates and protocol reminders have been
restored to the verified vendored official version.

## Retained adapter extensions

- Stable QQ/WeChat identities, source labels, platform-specific routing and
  unsupported-field handling.
- Tool-result provenance, model quota recovery, bounded duplicate suppression,
  read-only peer-group and language-learning tools.
- A host-generated tool availability snapshot is passed to Replyer as reference
  data. It does not make Replyer a tool executor or substitute for tool results.
- Custom persona, behavior and emoji preferences are deployment configuration,
  not evidence of identical upstream defaults. Production persona was preserved.

## Verification

`scripts/planner_replyer_contract_acceptance.py` checks actual execution with
intercepted sends: native schemas; always invoking Replyer even with a legacy
Planner body argument; handoff facts; independent fragment receipts; generation
failure; and partial delivery failure. It does not contact real groups.

Additional acceptance: request-format and transport bridge suites. All tests use
isolated instances. Subjective live model style is not certified by these tests.

Recorded results:

- Planner/Replyer contract: 5 tests passed.
- Request format: 9 tests passed.
- WeChat translation/outbound unit suite: 65 tests passed.
- Full isolated native bridge: 81 acceptance checks passed, exit code 0.
- Indexed source validation: 371 Python files, 9 TOML files and 24 JSON files
  parsed successfully. TypeScript configurations use JSONC and are checked by
  the TypeScript build instead of a strict JSON parser.
- WebUI `npm run build`: passed (TypeScript and Vite, exit code 0). Vite reports
  large output chunks as a bundle-size warning, not a compilation failure.
- All three Planner protocol templates and both protocol-reminder constants
  match the pinned official reference.

The older `maibot_style_acceptance.py` report compares deployment preferences and
an old persona checksum, and fails those configuration-equality assertions for
the user's current customized persona/style/emoji preferences. It is not reported
as passing and does not indicate a remaining Planner direct-send path. Those
deployment preferences were not overwritten during this mechanism audit.

## Publication boundary

Source code, plugin adaptation, WebUI source and development documentation are
published. Local model credentials, live bot/adapter configuration, private chat
logs, runtime state, downloaded dependencies and generated build output are not
part of this change. The tracked deployment configs retain their prior public
defaults; the local production edits remain local.
