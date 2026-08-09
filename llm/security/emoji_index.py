import os
import re


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
            for name in sorted(os.listdir(folder)):
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue

                emoji_name, _ = os.path.splitext(name)
                emoji_name = emoji_name.strip()
                if not emoji_name:
                    continue

                # Keep the full stem for compatibility and expose synonyms
                # from names such as "抽象 或 整活 或 我在.png".
                names = [emoji_name]
                names.extend(
                    part.strip()
                    for part in re.split(r"\s+或\s+", emoji_name)
                    if part.strip()
                )
                for selectable_name in names:
                    if selectable_name and selectable_name not in result:
                        result[selectable_name] = path
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
