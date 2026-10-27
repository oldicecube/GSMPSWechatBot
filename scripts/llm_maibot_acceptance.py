"""End-to-end acceptance checks for the MaiBot-backed legacy LLM facade."""
from __future__ import annotations

import asyncio, json, os, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIBOT = Path(os.environ.get("MAIBOT_ROOT", r"D:\maibot"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(MAIBOT))

from llm.core import LLMService  # noqa: E402


def main() -> None:
    service = LLMService()
    prompt = service._build_prompt("测试用户", "请只回复 OK", "test-group", {"quoted_content": "上一条引用"})
    assert "测试用户" in prompt and "请只回复 OK" in prompt
    result = service.handle_message("test-group", "测试用户", "请只回复 OK", session_id="acceptance")
    assert result.get("should_reply") is True, result
    assert result.get("messages") and result["messages"][0]["content"].strip(), result
    aux = service._provider_call([{"role": "user", "content": "Reply with exactly OK."}], task_name="utils")
    assert str(aux.content).strip(), aux

    emoji_db = MAIBOT / "data" / "MaiBot.db"
    conn = sqlite3.connect(emoji_db)
    emoji_count = conn.execute("select count(*) from images where image_type='emoji' and is_registered=1").fetchone()[0]
    conn.close()
    emoji_files = len(list((MAIBOT / "data" / "emoji").glob("*")))
    assert emoji_count == emoji_files == 16, (emoji_count, emoji_files)
    changed = os.popen(f'git -C "{ROOT}" diff 5258467 --name-only').read().splitlines()
    allowed = {"TODO_LLM_MIGRATION.md", "llm/core/__init__.py", "llm/core/maibot_service.py", "plugins/emoji.py", "plugins/jane.py"}
    assert set(changed) <= allowed, changed
    report = {"status": "passed", "model": result.get("maibot_model"), "reply": result["messages"][0]["content"], "emoji_count": emoji_count, "external_files_changed": changed}
    out = ROOT / "docs" / "llm-migration-acceptance.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
