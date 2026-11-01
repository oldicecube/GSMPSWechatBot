"""Deprecated import compatibility; MaiBot owns the only emoji catalog."""
from llm.core.maibot_emoji import get_native_emoji_path, native_emoji_catalog


def build_emoji_index(emoji_dir=None):
    # Old directories are ignored; no independent filename index is built.
    return native_emoji_catalog()


def get_emoji_list():
    return list(native_emoji_catalog())


def get_emoji_path(name):
    return get_native_emoji_path(name)
