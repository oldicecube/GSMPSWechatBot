class I18nError(Exception):
    """国际化基础异常。"""


class InvalidLocaleError(I18nError):
    """Locale 格式非法。"""


class LocaleNotFoundError(I18nError):
    """未找到指定 locale 的翻译目录。"""


class InvalidTranslationFileError(I18nError):
    """翻译文件结构非法。"""


class DuplicateTranslationKeyError(I18nError):
    """同一 locale 下存在重复的翻译 key。"""
