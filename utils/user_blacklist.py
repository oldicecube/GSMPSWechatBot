"""One persistent bot-access blacklist shared by shell and native inputs."""
import re
import threading
from functools import lru_cache
from pathlib import Path
from utils.sqlite_store import load_document, save_document

PATH = 'data/blacklist.json'
LOCK = threading.RLock()


def user_key(platform, user_id):
    user_id = str(user_id or '').strip()
    if platform == 'qq':
        raw = user_id.removeprefix('qq:')
        return 'qq:' + str(int(raw)) if re.fullmatch(r'[1-9][0-9]{4,19}', raw) else ''
    if platform == 'wechat':
        from utils.wechat_identity import canonical_wechat_user_id
        return canonical_wechat_user_id(user_id) if re.fullmatch(r'wxid_[A-Za-z0-9_-]+', user_id) else ''
    return ''


@lru_cache(maxsize=32)
def _load_cached(path, fingerprint):
    value = load_document(path, default={})
    return value if isinstance(value, dict) else {}


def load(path=PATH):
    from utils.sqlite_store import DB_PATH, BASE_DIR
    source = Path(path) if Path(path).is_absolute() else Path(BASE_DIR)/path
    def stamp(path):
        try:
            stat = Path(path).stat()
            return stat.st_mtime_ns,stat.st_size,stat.st_ino
        except FileNotFoundError:
            return None
    # Invalidate on SQLite WAL writes/checkpoints and legacy-file replacement.
    fingerprint = tuple(stamp(p) for p in (DB_PATH,DB_PATH+'-wal',source))
    return dict(_load_cached(str(path),fingerprint))


def is_banned(platform, user_id, *, path=PATH):
    key = user_key(platform, user_id)
    if not key:
        return False
    return any(bool(value) and user_key('qq' if str(stored).startswith('qq:') else 'wechat', stored) == key
               for stored, value in load(path).items())


def add(key, *, path=PATH):
    platform = 'qq' if str(key).startswith('qq:') else 'wechat'
    normalized = user_key(platform, key)
    if not normalized:
        raise ValueError('无效用户 ID')
    with LOCK:
        existed = is_banned(platform, key, path=path)
        data = load(path)
        data[normalized] = True
        save_document(path, data)
        _load_cached.cache_clear()
        return not existed
