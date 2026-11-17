"""Optional bounded lookup through the bot's existing web-search/MCP service."""
import asyncio
import logging
import uuid

LOG = logging.getLogger(__name__)


def lookup_entity(term, kind):
    try:
        from llm.core.maibot_runtime import native_runtime
        from src.config.config import model_config
        from src.core.tooling import ToolInvocation, ToolExecutionContext
        from src.mcp_module.service import get_mcp_service
        from src.web_search.config import resolve_web_search_config
        from src.web_search.service import WebSearchService
        config = resolve_web_search_config(model_config.web, model_config.model_task_config.web_search)
        if not config.enabled or not native_runtime._loop or not native_runtime._loop.is_running():
            return []
        session = "anima-search-" + uuid.uuid4().hex
        suffix={"character":" anime character official name series","artist":" artist illustration danbooru tag","concept":" meaning visual appearance terminology"}
        if kind not in suffix:return []
        query = term + suffix[kind]
        invocation = ToolInvocation("web_search", {"query":query,"max_results":3}, session_id=session)
        context = ToolExecutionContext(session_id=session)
        async def run():
            return await asyncio.wait_for(WebSearchService(get_mcp_service()).execute(config, invocation, context), timeout=8)
        future = asyncio.run_coroutine_threadsafe(run(), native_runtime._loop)
        try:
            result = future.result(timeout=9)
        except Exception:
            future.cancel()
            raise
        return [{"title":x.title[:200],"url":x.url[:300],"snippet":x.snippet[:500]} for x in result.results[:3]]
    except Exception as exc:
        LOG.warning("Anima optional search unavailable: %s", type(exc).__name__)
        return []
