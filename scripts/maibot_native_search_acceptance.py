"""Native MCP + unified search provider and optional real model/tool roundtrip."""
import asyncio
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
INSTANCE=Path(tempfile.mkdtemp(prefix='native-search-',dir=ROOT/'temp'))
os.environ['MAIBOT_INSTANCE_DIR']=str(INSTANCE)
(INSTANCE/'config').mkdir()
for name in ('bot_config.toml','model_config.toml'):
    shutil.copy2(ROOT/'config'/name, INSTANCE/'config'/name)


async def main():
    from src.config.official_configs import MCPConfig, MCPServerItemConfig
    from src.web_search.config import WebConfig, WebServiceConfig, WebSearchTaskConfig, resolve_web_search_config
    from src.web_search.service import WebSearchService
    from src.web_search.provider import WebSearchToolProvider
    from src.mcp_module.service import MCPService
    from src.core.tooling import ToolInvocation, ToolExecutionContext
    from src.llm_models.model_client.openai_client import OpenaiClient
    from src.llm_models.model_client.base_client import ResponseRequest
    from src.config.model_configs import APIProvider, ModelInfo
    from src.llm_models.payload_content.context_item import ContextItemBuilder, ContextItemMeta, RoleType
    from src.llm_models.payload_content.tool_option import ToolOption
    checks=[]
    def check(name,passed,**detail):
        checks.append(dict(name=name,passed=bool(passed),**detail));print(name,bool(passed),flush=True)
    mcp=MCPService()
    model_client=None
    report=dict(synthetic_only=True,physical_messages=False,at=datetime.now().isoformat(),checks=checks)
    try:
        await mcp.ensure_initialized(MCPConfig(enable=True,servers=[MCPServerItemConfig(name='Parallel-Free-Search',
            transport='streamable_http',url='https://search.parallel.ai/mcp',http_timeout_seconds=15,read_timeout_seconds=30)]))
        cfg=resolve_web_search_config(WebConfig(services={'Parallel-Free-Search':WebServiceConfig(
            adapter='parallel_mcp',server_ref='Parallel-Free-Search',timeout=25)}),WebSearchTaskConfig(model_list=['Parallel-Free-Search']))
        provider=WebSearchToolProvider(WebSearchService(mcp),lambda:cfg)
        specs=await provider.list_tools()
        check('native_tools_registered',{s.name for s in specs}=={'web_search','web_fetch'})
        context=ToolExecutionContext(session_id='synthetic-search-20261031',stream_id='synthetic-stream')
        results=[]
        for name,args in [('web_search',dict(query='site:docs.python.org asyncio TaskGroup documentation',max_results=3)),
                          ('web_fetch',dict(url='https://docs.python.org/3/library/asyncio-task.html'))]:
            result=await provider.invoke(ToolInvocation(tool_name=name,arguments=args,session_id=context.session_id),context)
            results.append(result)
            check(name+'.real_native_mcp',result.success and 'TaskGroup' in result.content,
                error=result.error_message,metadata=result.metadata)
        from src.maisaka.tool_result_store import tool_result_store
        check('source_and_excerpt_labels',all('[外部网页资料]' in r.content and r.metadata.get('result_id') for r in results))
        # Real MaiBot OpenAI adapter selects the registered native search tool.
        model_client=OpenaiClient(APIProvider(name='search-acceptance',base_url='https://oai.endpoints.kepler.ai.cloud.ovh.net/v1',
            api_key='',auth_type='none',timeout=30,max_retry=1,system_messages_at_start=True))
        model=ModelInfo(name='search-acceptance',model_type='llm',model_identifier='Mistral-Small-3.2-24B-Instruct-2506',
            api_provider='search-acceptance',force_stream_mode=False,visual=True)
        definitions=[ToolOption.from_definition(s.to_llm_definition()) for s in specs]
        items=[ContextItemBuilder().set_role(RoleType.System).add_text_content('Synthetic capability test. Search before answering; do not invent sources.').build(),
            ContextItemBuilder().add_text_content('Find official Python TaskGroup documentation using web_search now.').build(),
            ContextItemBuilder().set_role(RoleType.System).add_text_content('Use search query site:docs.python.org asyncio TaskGroup documentation.').build()]
        t=time.monotonic()
        request=ResponseRequest(model_info=model,context_items=items,tool_options=definitions,max_tokens=1024)
        response=await model_client.get_response(request)
        calls=response.tool_calls or []
        check('llm_calls_native_search',bool(calls) and calls[0].func_name=='web_search',seconds=round(time.monotonic()-t,2))
        if calls and calls[0].func_name=='web_search':
            call=calls[0]
            result=await provider.invoke(ToolInvocation(tool_name=call.func_name,arguments=call.args,session_id=context.session_id),context)
            check('model_requested_search_returns_sources',result.success and 'docs.python.org' in result.content)
            items.extend(response.output_items)
            items.append(ContextItemBuilder().set_role(RoleType.Tool).set_meta(ContextItemMeta.create(logical_turn_id=request.logical_turn_id))
                .set_tool_call_id(call.call_id).set_tool_name(call.func_name).add_text_content(result.content).build())
            items.append(ContextItemBuilder().add_text_content('Answer briefly from these retrieved results and include the official source URL. Do not use tools again.').build())
            # The anonymous OVH endpoint needs pacing across actual requests.
            await asyncio.sleep(max(0,32-(time.monotonic()-t)))
            response=await model_client.get_response(ResponseRequest(model_info=model,context_items=items,tool_options=definitions,max_tokens=1024))
            check('llm_answers_from_real_tool_result','docs.python.org' in (response.content or '') and 'TaskGroup' in (response.content or ''))
            report['answer_excerpt']=(response.content or '')[:1200]
    except Exception as exc:
        import traceback
        check('unhandled',False,error=type(exc).__name__)
        print(traceback.format_exc(),flush=True)
    finally:
        if model_client:await model_client.client.close()
        await mcp.close()
        report['passed']=bool(checks) and all(c['passed'] for c in checks)
        (ROOT/'docs/maibot-wechat-acceptance/native-search-20261031.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return report['passed']

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
