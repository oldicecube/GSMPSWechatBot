import os


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_EMOJI_INDEX = {}


def build_emoji_index(emoji_dir=None):
    global _EMOJI_INDEX

    candidates = []
    if emoji_dir:
        if os.path.isabs(emoji_dir):
            candidates.append(emoji_dir)
        else:
            candidates.append(os.path.join(BASE_DIR, emoji_dir))

    result = {}

    for folder in candidates:
        if not os.path.isdir(folder):
            continue

        try:
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue

                emoji_name, _ = os.path.splitext(name)
                emoji_name = emoji_name.strip()
                if not emoji_name or emoji_name in result:
                    continue

                result[emoji_name] = path
        except Exception:
            continue

    _EMOJI_INDEX = result
    return dict(_EMOJI_INDEX)


def get_emoji_list():
    return list(_EMOJI_INDEX.keys())


def get_emoji_path(name):
    if not name:
        return None
    return _EMOJI_INDEX.get(str(name).strip())
