"""Live native embedding acceptance with synthetic inputs and isolated storage.

Does not start/restart production, read production chat data, or send WeChat messages.
"""
from __future__ import annotations
import asyncio
from dataclasses import asdict
from datetime import datetime
import io
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = {'checks': [], 'mode': 'live-embedding/synthetic-only/isolated-storage'}


def check(name, passed, detail=None):
    REPORT['checks'].append(dict(name=name, passed=bool(passed), detail=detail))
    print('EMBEDDING', name, bool(passed), detail, flush=True)


async def run(instance):
    import numpy as np
    from src.common.database.database import initialize_database, get_db_session
    from src.config.config import global_config, model_config
    from src.services.embedding_service import EmbeddingServiceClient
    from src.A_memorix.core.image.embedder import HostImageEmbedder, _probe_png

    initialize_database()
    check('config.task_model_types', all(
        next(m for m in model_config.models if m.name == name).model_type == task
        for task in ['embedding', 'image_embedding']
        for name in getattr(model_config.model_task_config, task).model_list))
    check('config.single_space_no_cross_model_fallback', all(
        len(getattr(model_config.model_task_config, t).model_list) == 1
        for t in ['embedding', 'image_embedding']))
    check('config.embedding_features', global_config.expression.use_vector_expression and
          global_config.a_memorix.image_memory.enabled and
          global_config.a_memorix.embedding.paragraph_vector_backfill.enabled and
          global_config.a_memorix.retrieval.relation_vectorization.enabled and
          global_config.a_memorix.retrieval.relation_vectorization.backfill_enabled)
    texts = ['Minecraft服务器正常运行，大家可以进服游玩。',
             '游戏服务器运行正常，没有故障。', '牛肉面加一个鸡蛋。']
    client = EmbeddingServiceClient(request_type='embedding.acceptance.synthetic')
    results = await client.embed_texts(texts, max_concurrent=2)
    vectors = np.asarray([r.embedding for r in results])
    normalized = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    check('native.text_dimension_finite', vectors.shape == (3, 1024) and np.isfinite(vectors).all())
    check('native.chinese_semantic_order', float(normalized[0] @ normalized[1]) >
          float(normalized[0] @ normalized[2]) + .1,
          {'related': float(normalized[0] @ normalized[1]), 'unrelated': float(normalized[0] @ normalized[2])})

    # Preserve rejection of fake, constant, and unstable image backends after improving probe images.
    fingerprint = {'model': 'fixture', 'dimension': 2}
    for name, values in [('constant', [[1., 0.]] * 3), ('zero', [[0., 0.]] * 3),
                         ('unstable', [[1., 0.], [0., 1.], [-1., 0.]])]:
        probe = HostImageEmbedder()
        probe._request = AsyncMock(side_effect=[{'embedding': np.asarray(v), 'fingerprint': fingerprint} for v in values])
        result = await probe.probe()
        check('image_probe.reject_' + name, not result['available'], result.get('message'))
    embedder = HostImageEmbedder(preprocess_version=global_config.a_memorix.image_memory.preprocess_version)
    probe = await embedder.probe()
    REPORT['image_probe'] = probe
    check('native.image_probe', probe.get('available') and probe.get('dimension') == 2048, probe)
    if not probe.get('available'):
        return

    from src.A_memorix.core.runtime.sdk_memory_kernel import SDKMemoryKernel
    from src.A_memorix.core.runtime.models import KernelSearchRequest
    cfg = global_config.a_memorix.model_dump()
    cfg['storage']['data_dir'] = str(instance / 'data' / 'a-memorix')
    kernel = SDKMemoryKernel(plugin_root=ROOT / 'src/A_memorix', config=cfg)
    # Run each production operation explicitly so no periodic task races the test.
    with patch.object(kernel, '_start_background_tasks', new=AsyncMock()):
        try:
            await kernel.initialize()
            recovered = await kernel._recover_embedding_once(sample_text=texts[0])
            check('memory.embedding_recovered', recovered.get('success'), recovered)
            check('memory.dimension_matches_native_task', kernel.embedding_dimension == 1024,
                  kernel.embedding_dimension)
            rows = []
            for i, text in enumerate([texts[0], texts[2]]):
                row = await kernel.ingest_text(external_id=f'embedding-test-{i}', source_type='chat_summary',
                    text=text, chat_id='embedding-test-chat', respect_filter=False,
                    relations=[{'subject': 'Minecraft服务器', 'predicate': '状态', 'object': '正常运行'}] if i == 0 else [])
                rows.append(row)
                check(f'memory.ingest_{i}_not_metadata_only', bool(row.get('stored_ids')) and not row.get('warnings'), row)
            check('memory.paragraph_vectors_written', all(
                row['stored_ids'][0] in kernel._paragraph_store() for row in rows))
            check('memory.relation_vector_written', len(rows[0]['stored_ids']) > 1 and
                  kernel._graph_vector_id('relation', rows[0]['stored_ids'][1]) in kernel._graph_vector_store())
            search = await kernel.search_memory(KernelSearchRequest(query=texts[1], limit=3,
                chat_id='embedding-test-chat', respect_filter=False))
            REPORT['memory_search'] = search
            check('memory.semantic_recall', bool(search.get('hits')) and
                  any('服务器' in str(h.get('content', '')) for h in search.get('hits', [])) and
                  'vector' in str(search.get('available_channels')), search)
            spaces = await kernel._vector_space_service.list_spaces()
            check('memory.space_fingerprint_saved', bool(spaces.get('items')) and
                  any('nemotron' in str(s.get('embedding_fingerprint')) for s in spaces['items']), spaces)

            # Simulate an embedding outage without contacting a deliberately broken endpoint.
            kernel._set_embedding_degraded(active=True, reason='synthetic acceptance outage')
            kernel.relation_vectors_enabled = False
            pending = await kernel.ingest_text(external_id='embedding-queued', source_type='chat_summary',
                text='测试服务器明天上午进行维护。', chat_id='embedding-test-chat', respect_filter=False,
                relations=[{'subject': '测试服务器', 'predicate': '维护时间', 'object': '明天上午'}])
            check('memory.outage_metadata_and_backfill_queue', bool(pending.get('warnings')) and
                  pending['stored_ids'][0] not in kernel._paragraph_store(), pending)
            kernel.relation_vectors_enabled = True
            recovered = await kernel._recover_embedding_once(sample_text=texts[0])
            check('memory.paragraph_backfill_after_recovery', recovered.get('success') and
                  pending['stored_ids'][0] in kernel._paragraph_store(), recovered.get('backfill'))
            relation_backfill = await kernel._vector_runtime_service._backfill_missing_dual_vector_pool_entries(batch_size=8)
            check('memory.relation_backfill', kernel._graph_vector_id('relation', pending['stored_ids'][1])
                  in kernel._graph_vector_store(), relation_backfill)

            images = kernel.image_memory_runtime
            space = await images.ensure_embedding_space()
            check('image_memory.space_ready', space.get('status') == 'ready', space)
            red = _probe_png(square=True)
            ingested = await images.ingest(image_bytes=red, source_kind='message', external_ref='embedding-image-1',
                scope_type='chat', chat_id='embedding-test-chat', message_id='synthetic-image-1')
            jobs = await images.process_jobs_once()
            check('image_memory.jobs_indexed', images.metadata_store.get_image_embedding(
                ingested['asset_id'], __import__('src.A_memorix.core.image.fingerprints', fromlist=['fingerprint_hash']).fingerprint_hash(images.fingerprint)
                ).get('status') == 'ready', jobs)
            # Different bytes, same visual content: require vector recall, not exact-hash lookup.
            from PIL import Image
            buf = io.BytesIO()
            Image.open(io.BytesIO(red)).save(buf, format='BMP')
            search = await images.search(image_bytes=buf.getvalue(), chat_ids=['embedding-test-chat'],
                candidate_limit=8, similarity_threshold=.72)
            check('image_memory.visual_recall', search.get('success') and any(
                h['asset_id'] == ingested['asset_id'] and h['match_kind'] == 'visual_similar'
                for h in search.get('hits', [])), search)
            hidden = await images.search(image_bytes=red, chat_ids=['other-chat'], candidate_limit=8,
                similarity_threshold=.72)
            check('image_memory.chat_scope_isolated', not hidden.get('hits'))
        finally:
            await kernel.shutdown()
            check('memory.writer_lock_released', not kernel._runtime_writer_lock.held)

    from src.common.database.database_model import Expression
    from src.chat.replyer.expression_vector_index import ExpressionVectorIndex, ExpressionVectorIndexUpsertItem
    situations = ['服务器运行正常', '服务器发生故障', '问候群友', '今天午饭', '明天下雨', '游戏更新',
                  '大家晚安', '早上起床', '朋友开玩笑', '周末出门']
    items = []
    with get_db_session() as session:
        for i, situation in enumerate(situations, 1):
            session.add(Expression(id=i, situation=situation, style='回应' + situation,
                content_list='[]', count=1, session_id='embedding-test-chat', checked=True))
            items.append(ExpressionVectorIndexUpsertItem(id=i, situation=situation, style='回应' + situation,
                count=1, session_id='embedding-test-chat', checked=True, modified_by=''))
    index = ExpressionVectorIndex()
    path = str(instance / 'data/expression-test/index.json')
    result = await index.upsert_expressions(index_path=path, expressions=items)
    check('expression.vector_index_built', result is not None and result.total_count == 10 and result.failed_count == 0,
          asdict(result) if result else None)
    recalled = await index.select_candidates(index_path=path, session_id='embedding-test-chat',
        query_text='游戏服务器现在没有故障，一切正常', scoped_candidates=[asdict(i) for i in items],
        candidate_pool_size=3, cluster_pool_size=10)
    check('expression.semantic_recall', bool(recalled) and any(i['id'] == 1 for i in recalled), recalled)
    await index.stop_history_backfill()


def main():
    instance = ROOT / 'temp/maibot-embedding-acceptance' / datetime.now().strftime('%Y%m%d-%H%M%S')
    (instance / 'config').mkdir(parents=True)
    for name in ['bot_config.toml', 'model_config.toml']:
        shutil.copy2(ROOT / 'config' / name, instance / 'config' / name)
    shutil.copy2(ROOT / 'config.json', instance / 'config.json')
    os.environ['MAIBOT_INSTANCE_DIR'] = str(instance)
    os.environ['MAIBOT_INTERCEPT_SEND'] = '1'
    os.chdir(instance)
    from llm.core.maibot_native_config import sync_native_config
    sync_native_config()
    REPORT['instance'] = str(instance)
    try:
        asyncio.run(run(instance))
    except Exception:
        import traceback
        check('acceptance.exception', False, traceback.format_exc())
    REPORT['passed'] = bool(REPORT['checks']) and all(c['passed'] for c in REPORT['checks'])
    output = ROOT / 'docs/maibot-wechat-acceptance/embedding-native-20261029.json'
    output.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding='utf8')
    print('REPORT', output, flush=True)
    return 0 if REPORT['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
