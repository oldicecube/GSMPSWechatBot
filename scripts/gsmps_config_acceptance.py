"""Read-only GSMPS shell configuration declaration acceptance."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.maibot_shell_registry import catalog
from core.shell_plugin_config import settings


def main():
    rows = []
    failures = []
    for plugin in catalog():
        plugin_id = plugin['id']
        fields = plugin.get('config_fields') or {}
        try:
            values = settings(plugin_id)
            for name, field in fields.items():
                if name not in values:
                    failures.append(f'{plugin_id}.{name}: missing runtime value')
                    continue
            rows.append({'id': plugin_id, 'fields': len(fields), 'settings_ok': True})
        except Exception as exc:
            failures.append(f'{plugin_id}: {type(exc).__name__}: {exc}')
            rows.append({'id': plugin_id, 'fields': len(fields), 'settings_ok': False})
    report = {'passed': not failures, 'plugin_count': len(rows), 'rows': rows, 'failures': failures,
              'production_mutated': False, 'restarted': False}
    out = ROOT / 'docs/maibot-wechat-acceptance/gsmps-config-20261031.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'passed': report['passed'], 'plugin_count': len(rows), 'failures': failures}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
