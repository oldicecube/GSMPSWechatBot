"""Read-only audit of active learning/memory paths and backlogs."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def count(db, table, where=''):
    try:
        return int(db.execute(f'SELECT count(*) FROM {table} {where}').fetchone()[0])
    except Exception:
        return None


def main():
    bot = tomllib.loads((ROOT/'config/bot_config.toml').read_text(encoding='utf8'))
    model = tomllib.loads((ROOT/'config/model_config.toml').read_text(encoding='utf8'))
    report = {
        'at': datetime.now().isoformat(),
        'active_native_learning': {
            'expression_learning_rules': bot.get('expression', {}).get('learning_list', []),
            'jargon_learning_rules': bot.get('jargon', {}).get('learning_list', []),
            'memory_enabled': bool(bot.get('a_memorix', {}).get('plugin', {}).get('enabled', True)),
            'profile_injection': bool(bot.get('a_memorix', {}).get('integration', {}).get('enable_person_profile_injection', True)),
            'heuristic_memory': bool(bot.get('a_memorix', {}).get('integration', {}).get('heuristic_memory_recall_enabled', True)),
            'learner_models': model.get('model_task_config', {}).get('learner', {}).get('model_list', []),
        },
        'legacy_sqlmodel_learning': {},
        'native_style_learning_store': {},
        'blocking_risks': [],
        'production_mutated': False,
        'restarted': False,
    }
    with sqlite3.connect(ROOT/'data/MaiBot.db') as db:
        report['legacy_sqlmodel_learning'] = {
            'expressions': count(db, 'expressions'),
            'jargons': count(db, 'jargons'),
            'high_frequency_terms': count(db, 'high_frequency_terms'),
            'expression_legacy_style': count(db, 'expressions', "WHERE lower(style)='legacy'"),
            'jargon_empty_meaning': count(db, 'jargons', "WHERE meaning='' OR meaning IS NULL"),
        }
    learning_db = ROOT/'data/llm_learning.sqlite3'
    with sqlite3.connect(learning_db) as db:
        report['native_style_learning_store'] = {
            table: count(db, table) for table in ('learning_samples','slang_terms','pending_slang_terms','style_expressions','learning_outbox','cycle_curation_runs')
        }
        report['native_style_learning_store']['safe_slang'] = count(db, 'slang_terms', 'WHERE safe_to_use=1')
        report['native_style_learning_store']['pending_repeated_slang'] = count(db, 'pending_slang_terms', 'WHERE occurrence_count>=2')
        report['native_style_learning_store']['curation_errors'] = count(db, 'cycle_curation_runs', "WHERE outcome='error'")
    if report['native_style_learning_store']['pending_repeated_slang']:
        report['blocking_risks'].append('Native style learner has repeated pending slang; provider failures can delay curation, but evidence is persisted.')
    if report['native_style_learning_store']['curation_errors']:
        report['blocking_risks'].append('Historical cycle curation errors exist; facade now acknowledges native trim-learning instead of retrying a no-op cycle forever.')
    if report['legacy_sqlmodel_learning']['expression_legacy_style']:
        report['blocking_risks'].append('Legacy expression rows use style=legacy; selector recovery filters old @/poke templates and recovers compact phrases.')
    path = ROOT/'docs/maibot-wechat-acceptance/learning-memory-audit-20261031.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
