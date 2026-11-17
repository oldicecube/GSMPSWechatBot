# Pinned upstream formatter subset

Project: SuzumiyaAkizuki/ComfyUI-NewBie-LLM-Formatter
Revision: 624ccf04cc05cf902b42525074aa9e08561ad8e8
License: MIT; original copyright and LICENSE preserved.

Unmodified files: prompt_formats.py, prompt_formats.json, output_contract.py, agent_prompts.py.
Runtime reuse: get_format_spec("Anima"), the literal LOW_ASSEMBLY_PROMPT (read with ast.literal_eval without importing agent roles), and parse_envelope for the nested formatter result.

The bot's wrapper retains its existing moderation, translation, identity verification, bounded generation parameters, search adapter and VLM output screening. The upstream two-field JSON envelope is nested inside the root bot JSON as formatter. Model input remains one tag line, a blank line and an English paragraph. No original NewBie XML/MOSS mode, ComfyUI node UI, permissive NSFW agent role, jailbreak option or unrestricted external tool loop is enabled.

The actual upstream tools.py functions are now used by a bounded Low-style planner/retriever/assembler with a semantic fact-audit step. Transport retries are bounded in anima/tag_tools.py; files remain unmodified on disk. Raw search candidates and co-occurrence results are not treated as proven character attributes. The earlier guard_identity_paragraph word filter has been removed.
