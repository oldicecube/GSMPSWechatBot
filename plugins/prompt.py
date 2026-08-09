"""/prompt: send the last complete LLM prompt to an administrator."""

from __future__ import annotations

import os

from core.sender import send
from llm.config import get_llm_config
from llm.prompt_capture import get_last_prompt


COMMAND = "/prompt"
PROMPT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prompt.txt",
)


def handle(content: str, context: dict):
    config = get_llm_config()
    admin_wxids = {
        str(item).strip()
        for item in (config.get("admin_wxids") or [])
        if str(item).strip()
    }
    wxid = str(
        (context or {}).get("wxid")
        or ((context or {}).get("raw") or {}).get("wxid")
        or ""
    ).strip()
    if wxid not in admin_wxids:
        return None

    prompt = get_last_prompt()
    if not prompt:
        return "当前还没有转发给 LLM 的提示词"

    try:
        with open(PROMPT_FILE, "w", encoding="utf-8", newline="\n") as file:
            file.write(prompt)
    except OSError as exc:
        return f"提示词文件写入失败：{exc}"

    target = (context or {}).get("group") or (context or {}).get("user")
    ok, error = send(target=target, file_path=PROMPT_FILE, mode="wechat_file")
    if not ok:
        return f"提示词文件发送失败：{error}"
    return None
