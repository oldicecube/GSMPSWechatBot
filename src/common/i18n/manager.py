from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

import logging
import os
import threading

from .exceptions import I18nError, InvalidLocaleError
from .formatting import select_plural_category
from .loaders import format_template
from .loaders import DEFAULT_LOCALE, TranslationValue, get_locales_root, load_locale_catalog, normalize_locale

logger = logging.getLogger("maibot.i18n")


class I18nManager:
    """基于 JSON 的轻量级国际化管理器。"""

    def __init__(self, default_locale: str = DEFAULT_LOCALE, locales_root: Path | None = None):
        self._default_locale = normalize_locale(default_locale)
        self._locales_root = get_locales_root(locales_root)
        self._catalog_cache: dict[str, dict[str, TranslationValue]] = {}
        self._locale_override: ContextVar[str | None] = ContextVar("maibot_locale", default=None)
        self._warning_cache: set[tuple[str, str, str]] = set()
        self._cache_lock = threading.RLock()
        self._warning_lock = threading.Lock()

    def set_locale(self, locale: str) -> str:
        self._default_locale = normalize_locale(locale)
        return self._default_locale

    def get_locale(self) -> str:
        override_locale = self._locale_override.get()
        if override_locale:
            return override_locale

        env_locale = os.getenv("MAIBOT_LOCALE")
        if env_locale:
            try:
                return normalize_locale(env_locale)
            except InvalidLocaleError:
                self._log_once(
                    ("invalid_env_locale", "env", env_locale),
                    logging.WARNING,
                    f"检测到非法 MAIBOT_LOCALE={env_locale}，已回退到默认 locale {self._default_locale}",
                )
        return self._default_locale

    @contextmanager
    def use_locale(self, locale: str) -> Iterator[None]:
        token = self._locale_override.set(normalize_locale(locale))
        try:
            yield
        finally:
            self._locale_override.reset(token)

    def reload(self, locale: str | None = None) -> None:
        with self._cache_lock:
            if locale is None:
                self._catalog_cache.clear()
                return
            self._catalog_cache.pop(normalize_locale(locale), None)

    def t(self, key: str, locale: str | None = None, **kwargs: object) -> str:
        translation_value, translation_locale = self._get_translation_value(key, locale)
        template = self._get_standard_template(key, translation_value, translation_locale)
        if template is None:
            return key

        return self._format_translation(key, template, kwargs)

    def tn(self, key: str, count: int | float, locale: str | None = None, **kwargs: object) -> str:
        translation_value, translation_locale = self._get_translation_value(key, locale)
        if translation_value is None:
            return key

        if not isinstance(translation_value, dict):
            self._log_once(
                ("non_plural_key", translation_locale, key),
                logging.WARNING,
                f"翻译 key '{key}' 不是 plural 节点，已回退到普通 t()",
            )
            return self.t(key, locale=translation_locale, count=count, **kwargs)

        try:
            plural_category = select_plural_category(translation_locale, count)
        except Exception as exc:
            logger.warning(f"为 key '{key}' 选择 plural category 失败: {exc}，已回退到 other")
            plural_category = "other"

        template = translation_value.get(plural_category) or translation_value.get("other")
        if template is None:
            self._log_once(
                ("plural_missing_template", translation_locale, key),
                logging.WARNING,
                f"翻译 key '{key}' 缺少 plural 模板，已回退到 key 本身",
            )
            return key

        formatting_kwargs = dict(kwargs)
        formatting_kwargs["count"] = count
        return self._format_translation(key, template, formatting_kwargs)

    def _get_standard_template(
        self,
        key: str,
        translation_value: TranslationValue | None,
        translation_locale: str,
    ) -> str | None:
        if translation_value is None:
            return None
        if not isinstance(translation_value, dict):
            return translation_value

        template = translation_value.get("other")
        if template is None:
            self._log_once(
                ("plural_missing_other", translation_locale, key),
                logging.WARNING,
                f"翻译 key '{key}' 缺少 other plural category，已回退到 key 本身",
            )
        return template

    def _format_translation(self, key: str, template: str, kwargs: dict[str, object]) -> str:
        try:
            return format_template(template, **kwargs)
        except Exception as exc:
            logger.error(f"翻译 key '{key}' 格式化失败: {exc}")
            return template

    def _get_translation_value(self, key: str, locale: str | None) -> tuple[TranslationValue | None, str]:
        target_locale = self._resolve_locale(locale)
        target_catalog = self._get_catalog(target_locale)
        if key in target_catalog:
            return target_catalog[key], target_locale

        if target_locale != self._default_locale:
            default_catalog = self._get_catalog(self._default_locale)
            if key in default_catalog:
                self._log_once(
                    ("missing_key_fallback", target_locale, key),
                    logging.WARNING,
                    f"翻译 key '{key}' 在 locale '{target_locale}' 中缺失，"
                    f"已回退到默认 locale '{self._default_locale}'",
                )
                return default_catalog[key], self._default_locale

        self._log_once(
            ("missing_key", target_locale, key),
            logging.WARNING,
            f"翻译 key '{key}' 缺失，locale='{target_locale}'，默认 locale='{self._default_locale}'",
        )
        return None, target_locale

    def _resolve_locale(self, locale: str | None) -> str:
        if locale is None:
            return self.get_locale()

        try:
            return normalize_locale(locale)
        except InvalidLocaleError:
            current_locale = self.get_locale()
            self._log_once(
                ("invalid_locale", "explicit", locale),
                logging.WARNING,
                f"检测到非法 locale='{locale}'，已回退到当前默认 locale {current_locale}",
            )
            return current_locale

    def _get_catalog(self, locale: str) -> dict[str, TranslationValue]:
        normalized_locale = normalize_locale(locale)
        with self._cache_lock:
            if normalized_locale in self._catalog_cache:
                return self._catalog_cache[normalized_locale]

        try:
            catalog = load_locale_catalog(normalized_locale, self._locales_root)
        except I18nError as exc:
            self._log_once(
                ("load_failed", normalized_locale, exc.__class__.__name__),
                logging.WARNING,
                f"加载 locale '{normalized_locale}' 失败: {exc}",
            )
            return {}

        with self._cache_lock:
            if normalized_locale in self._catalog_cache:
                return self._catalog_cache[normalized_locale]
            self._catalog_cache[normalized_locale] = catalog
            return catalog

    def _log_once(self, cache_key: tuple[str, str, str], level: int, message: str, *args: object) -> None:
        with self._warning_lock:
            if cache_key in self._warning_cache:
                return
            self._warning_cache.add(cache_key)
        logger.log(level, message, *args)
