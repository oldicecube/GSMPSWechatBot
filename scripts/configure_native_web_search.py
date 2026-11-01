"""Stage verified Parallel search on MaiBot's native MCP/web configuration."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tomlkit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    result=json.loads((ROOT/'docs/maibot-wechat-acceptance/native-search-20261031.json').read_text(encoding='utf8'))
    assert result['passed'], 'Actual native MCP + model roundtrip must pass'
    paths=[ROOT/'config/bot_config.toml',ROOT/'config/model_config.toml']
    snapshots=[p.read_bytes() for p in paths]
    bot,models=[tomlkit.parse(b.decode('utf8')) for b in snapshots]
    personality_before=bot['personality'].unwrap()
    name='Parallel-Free-Search'
    servers=bot['mcp'].get('servers')
    if servers is None or not isinstance(servers,tomlkit.items.AoT):
        entries=list(servers or [])
        servers=tomlkit.aot()
        for item in entries:
            table=tomlkit.table();table.update(dict(item));servers.append(table)
        bot['mcp']['servers']=servers
    existing=next((s for s in servers if s['name']==name),None)
    if existing is not None and existing.get('url')!='https://search.parallel.ai/mcp':
        raise RuntimeError('Existing MCP name points elsewhere; untouched')
    if existing is None:
        server=tomlkit.table();server.update(name=name,enabled=True,transport='streamable_http',
            url='https://search.parallel.ai/mcp',http_timeout_seconds=15.0,read_timeout_seconds=30.0)
        servers.append(server)
    bot['mcp']['enable']=True
    if 'web' not in models:models['web']=tomlkit.table()
    web=models['web'];web['preview_chars']=4000
    if 'services' not in web:web['services']=tomlkit.table()
    service=tomlkit.table();service.update(adapter='parallel_mcp',server_ref=name,timeout=25.0)
    web['services'][name]=service
    if 'web_search' not in models['model_task_config']:models['model_task_config']['web_search']=tomlkit.table()
    models['model_task_config']['web_search']['model_list']=[name]
    from src.config.config import Config, ModelConfig, _normalize_loaded_bot_config_dict
    for cls,doc in ((Config,bot),(ModelConfig,models)):
        data=tomlkit.parse(tomlkit.dumps(doc)).unwrap();data.pop('inner',None)
        if cls is Config:data=_normalize_loaded_bot_config_dict(data)
        cls.model_validate(data)
    assert bot['personality'].unwrap()==personality_before
    if any(p.read_bytes()!=old for p,old in zip(paths,snapshots)):
        raise RuntimeError('Operator changed configuration; left untouched')
    backup=ROOT/'data/backups'/('native-search-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    for path,doc in zip(paths,(bot,models)):
        shutil.copy2(path,backup/path.name)
        pending=path.with_suffix('.search.tmp');pending.write_text(tomlkit.dumps(doc),encoding='utf8',newline='');os.replace(pending,path)
    report=dict(at=datetime.now().isoformat(),server=name,selected_native_service=name,
        authentication='none',backup=str(backup),persona_preserved=True,
        requires_user_main_process_restart=True,main_process_restarted=False)
    (ROOT/'docs/maibot-wechat-acceptance/native-search-config-20261031.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report),flush=True)

if __name__=='__main__':main()
