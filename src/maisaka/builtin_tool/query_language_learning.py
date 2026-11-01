"""查询 MaiBot 已学习的表达方式和黑话，供模型回答能力自检问题。"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlmodel import select

from src.common.database.database import get_db_session
from src.common.database.database_model import Expression, Jargon
from src.core.tooling import ToolExecutionContext, ToolExecutionResult, ToolInvocation, ToolSpec
from .context import BuiltinToolRuntimeContext


def get_tool_spec(*, enabled: bool = True) -> ToolSpec:
    return ToolSpec(
        name="query_language_learning",
        description=(
            "查询麦麦当前已学习的群聊表达方式和黑话。返回实际数据库中的词条、含义、"
            "使用次数和表达示例；没有结果时说明当前筛选范围为空，不要据此声称从未学习。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "可选，按词条、情景或含义过滤。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 12},
            },
            "additionalProperties": False,
        },
        provider_name="maisaka_builtin",
        provider_type="builtin",
        enabled=enabled,
    )


def _session_match(session_id: str, raw: str, *, is_global: bool = False) -> bool:
    if is_global:
        return True
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return session_id in str(raw)
    return isinstance(data, dict) and session_id in data


async def handle_tool(
    tool_ctx: BuiltinToolRuntimeContext,
    invocation: ToolInvocation,
    context: Optional[ToolExecutionContext] = None,
) -> ToolExecutionResult:
    del context
    runtime = tool_ctx.runtime
    session_id = str(runtime.session_id or "")
    query = str(invocation.arguments.get("query") or "").strip().casefold()
    try:
        limit = max(1, min(30, int(invocation.arguments.get("limit", 12) or 12)))
    except (TypeError, ValueError):
        limit = 12

    def read() -> dict[str, Any]:
        with get_db_session() as db:
            expressions = list(db.exec(select(Expression).order_by(Expression.count.desc()).limit(200)).all())
            jargons = list(db.exec(select(Jargon).order_by(Jargon.count.desc()).limit(200)).all())
        expression_rows = []
        for item in expressions:
            situation, style = str(item.situation or "").strip(), str(item.style or "").strip()
            if not situation or not style or style.casefold() in {"legacy", "unknown"}:
                continue
            if item.session_id not in (None, "", session_id) and not query:
                continue
            text = f"{situation} {style}".casefold()
            if query and query not in text:
                continue
            expression_rows.append({"situation": situation, "style": style, "count": int(item.count or 0)})
        jargon_rows = []
        for item in jargons:
            content, meaning = str(item.content or "").strip(), str(item.meaning or "").strip()
            if not content:
                continue
            if not _session_match(session_id, str(item.session_id_dict or ""), is_global=bool(item.is_global)) and not query:
                continue
            if query and query not in f"{content} {meaning}".casefold():
                continue
            jargon_rows.append({"word": content, "meaning": meaning or "含义仍在学习中", "count": int(item.count or 0)})
        return {
            "session_id": session_id,
            "expression_count": len(expression_rows),
            "jargon_count": len(jargon_rows),
            "expressions": expression_rows[:limit],
            "jargons": jargon_rows[:limit],
        }

    try:
        payload = await __import__("asyncio").to_thread(read)
    except Exception as exc:
        return tool_ctx.build_failure_result(invocation.tool_name, f"查询学习记录失败：{type(exc).__name__}")
    return tool_ctx.build_success_result(
        invocation.tool_name,
        content=json.dumps(payload, ensure_ascii=False),
        structured_content=payload,
    )
