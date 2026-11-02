"""Host bridge to MaiBot's actual subagent runtime, with a durable leisure journal."""
import asyncio
import json

from utils.fun_journal import FunJournal

OWNER = 'wechatbot.autonomous-life'
ALLOWED_TOOLS = {'web_search', 'web_fetch', 'browse_story', 'sandbox_program', 'restricted_python', 'get_hot_topics'}


async def control(plugin_id, args):
    if plugin_id != OWNER:
        return {'success': False, 'error': '仅自主生活插件可以操作乐子任务'}
    from src.chat.heart_flow.heartflow_manager import heartflow_manager
    from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
    from src.maisaka.builtin_tool.subagent import _run_sub_agent_rounds, _is_grantable
    from src.maisaka.subagent_tasks import SUBAGENT_MAX_RUNNING_TASKS
    from src.platform_io import get_platform_io_manager, RouteKey
    from src.platform_io.chat_features import route_override
    stream_id = str(args.get('stream_id') or '')
    runtime = await heartflow_manager.get_or_create_heartflow_chat(stream_id)
    stream = runtime.chat_stream
    route = RouteKey(platform=stream.platform, account_id=stream.account_id, scope=stream.scope)
    if not stream.is_group_session or not await get_platform_io_manager().is_target_allowed(route, chat_type='group', target_id=stream.group_id):
        return {'success': False, 'error': '群聊来源未获准'}
    if any(route_override(route, stream.group_id, feature) is False for feature in ('plugins_enabled', 'llm_enabled')):
        return {'success': False, 'error': '本群LLM或插件已关闭'}
    journal = FunJournal()
    # Recover a process restart explicitly; never silently replay side effects.
    for running in journal.snapshot(stream_id)['running']:
        task = runtime._subagent_tasks.get(running['id'])
        if task is None or task.status != 'running':
            journal.finish(running['id'], 'interrupted', '上次子代理已中断；尚未确认的工作不算完成，Planner可决定是否继续。')
    action = str(args.get('action') or 'status')
    if action == 'status':
        return {'success': True, **journal.snapshot(stream_id)}
    if action == 'consume':
        return {'success': journal.consume(stream_id, str(args.get('report_id') or ''))}
    if action in {'pause', 'abandon'}:
        for run in journal.transition(stream_id, str(args.get('direction_id') or ''), action):
            runtime._subagent_tasks.stop(run)
        return {'success': True, **journal.snapshot(stream_id)}
    if action not in {'start', 'continue'}:
        return {'success': False, 'error': '未知乐子操作'}
    if runtime._subagent_tasks.running_count >= SUBAGENT_MAX_RUNNING_TASKS:
        return {'success': False, 'error': '原生子代理并发已满'}
    registry = runtime._tool_registry
    if registry is None:
        return {'success': False, 'error': '原生工具注册表尚未就绪'}
    mode = str(args.get('mode') or 'hot')
    if action == 'continue':
        direction = next((row for row in journal.snapshot(stream_id)['directions']
                          if row['id'] == str(args.get('direction_id') or '')), None)
        if direction:
            mode = direction['mode']
    specs = [spec for spec in await registry.list_tools(runtime._reasoning_engine._build_tool_availability_context())
             if spec.name in ALLOWED_TOOLS and _is_grantable(spec)
             and (mode == 'code' or spec.name not in {'sandbox_program', 'restricted_python'})]
    if not specs:
        return {'success': False, 'error': '没有可用的联网或隔离沙箱工具'}
    run = journal.reserve(stream_id, str(args.get('title') or '继续探索'), mode,
        str(args.get('task') or ''), str(args.get('direction_id') or '') if action == 'continue' else '',
        limit=min(12, max(1, int(args.get('daily_limit') or 12))))
    tool_ctx = BuiltinToolRuntimeContext(runtime._reasoning_engine, runtime)
    async def runner(background_task):
        async with asyncio.timeout(600):
            result = await _run_sub_agent_rounds(tool_ctx, task_id=run['id'], task=run['task']+
                journal.continuation_material(run['direction'])+
                '\n只做授权范围内的探索。代码必须调用隔离沙箱。不得自行发群消息。最后简报实际做了什么、结果、失败和下一步，不声称未执行过的操作。',
                granted_specs=specs, tool_usage=background_task.tool_usage, max_rounds=5,
                result_recorder=lambda name, arguments, result: journal.record_step(run['id'], name, arguments, result))
            result.conclusion = result.conclusion[:1800]
            return result
    def finished(task):
        report = ('工具实际调用记录：'+json.dumps(task.tool_usage, ensure_ascii=False)+'\n' +
                  (task.conclusion or task.error_message or '本轮没有得到结果'))[:2200]
        if journal.finish(task.task_id, task.status, report):
            # Durable before notification. On the next turn the plugin also
            # exposes this report until Planner explicitly consumes it.
            runtime.deliver_subagent_result(task)
    try:
        runtime._subagent_tasks.start(task_id=run['id'], description=run['task'],
            granted_tools=[spec.name for spec in specs], runner=runner, on_finished=finished)
    except BaseException:
        journal.finish(run['id'], 'failed', '原生子代理启动失败，未确认完成任何工作')
        raise
    return {'success': True, 'task_id': run['id'], 'direction_id': run['direction'], 'status': 'running',
            'granted_tools': [spec.name for spec in specs]}
