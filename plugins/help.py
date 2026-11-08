"""/help: render the live help.txt using a cropped ACG wallpaper and readable cards."""
WEBUI_CONFIG = {'version': 1, 'fields': {}}
COMMAND = '/help'

from pathlib import Path
import threading
import uuid
from core.sender import send

BASE_DIR = Path(__file__).resolve().parent
HELP_FILE = BASE_DIR / 'help.txt'
TEMP_DIR = BASE_DIR.parent / 'temp' / 'help'
_slots = threading.BoundedSemaphore(2)


def visible_commands(context):
    from core import maibot_shell_registry as registry
    platform = str(context.get('platform') or 'wechat')
    rows = [row for row in registry.catalog() if row.get('enabled')
            and platform in row.get('platforms', ['wechat','qq'])]
    by_command = {command:row for row in rows for command in row.get('commands',[])+row.get('aliases',[])}
    def visible(label):
        command = label.split()[0]
        row = by_command.get(command)
        if not row or not registry.allowed(row['id'],context):
            return False
        dispatcher = registry.get_dispatcher()
        if dispatcher and not dispatcher._module_enabled_for_group(row['module_key'],context.get('group'),context):
            return False
        if command == '/ban':
            from plugins.ban import is_admin
            return is_admin(context)
        if command == '/rcon':
            from plugins.rcon import _is_admin
            return _is_admin(context)
        if command == '/shit':
            import tomllib
            # Source availability is distinct from the shell command switch.
            folder = 'tieba_share' if label.split()[1:2] == ['tb'] else 'mai-repost-shit'
            try:
                cfg = tomllib.loads((registry.root()/'plugins'/folder/'config.toml').read_text(encoding='utf8'))
                return cfg.get('plugin',{}).get('enabled') is True
            except (OSError,ValueError):
                return False
        return True
    return visible


def handle(content, context):
    if str(content or '').strip():
        return '用法：/help，发送当前可用功能的帮助图片。'
    if not _slots.acquire(blocking=False):
        return '帮助图片正在生成，请稍后再试。'
    background = output = None
    try:
        from plugins import acgpic
        from utils.help_poster import parse_help,render_help
        # Re-read on every request: edits never require a module reload.
        text = Path(HELP_FILE).read_text(encoding='utf-8-sig')
        parsed = parse_help(text,visible_commands(context))
        try:
            background = acgpic.fetch_random_image(portrait=True)
        except Exception as exc:
            print(f'[HELP] background unavailable: {type(exc).__name__}',flush=True)
        TEMP_DIR.mkdir(parents=True,exist_ok=True)
        output=TEMP_DIR/('help_'+uuid.uuid4().hex+'.jpg')
        try:
            render_help(parsed,background,output)
        except (OSError,ValueError) as exc:
            if not background:
                raise
            print(f'[HELP] wallpaper decode failed: {type(exc).__name__}',flush=True)
            render_help(parsed,None,output)
        target=context.get('group') or context.get('sessionId') or context.get('user')
        ok,error=send(target=target,file_path=str(output),mode='wechat_file')
        return None if ok else '帮助图片发送失败，请稍后重试。'
    except FileNotFoundError:
        return '帮助文件不存在，请联系管理员。'
    except Exception as exc:
        print(f'[HELP] failed: {type(exc).__name__}: {exc}',flush=True)
        return '帮助图片生成失败，请联系管理员检查帮助文件。'
    finally:
        for path in (background,output):
            if path:
                try:Path(path).unlink(missing_ok=True)
                except OSError:pass
        _slots.release()
