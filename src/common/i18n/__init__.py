from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache

from .loaders import DEFAULT_LOCALE


@lru_cache(maxsize=1)
def _get_manager():
    from .manager import I18nManager

    return I18nManager()


def set_locale(locale: str) -> str:
    return _get_manager().set_locale(locale)


def get_locale() -> str:
    return _get_manager().get_locale()


def reload_translations(locale: str | None = None) -> None:
    _get_manager().reload(locale)


def t(key: str, locale: str | None = None, **kwargs: object) -> str:
    return _get_manager().t(key, locale=locale, **kwargs)


def tn(key: str, count: int | float, locale: str | None = None, **kwargs: object) -> str:
    return _get_manager().tn(key, count=count, locale=locale, **kwargs)


def use_locale(locale: str):
    return _get_manager().use_locale(locale)


def format_datetime_localized(value: datetime | date, locale: str | None = None, format: str = "medium") -> str:
    from .formatting import format_datetime_localized as _format_datetime_localized

    return _format_datetime_localized(value, locale=locale or get_locale(), format=format)


def format_number_localized(value: int | float | Decimal, locale: str | None = None) -> str:
    from .formatting import format_number_localized as _format_number_localized

    return _format_number_localized(value, locale=locale or get_locale())


def format_decimal_localized(value: int | float | Decimal, locale: str | None = None) -> str:
    from .formatting import format_decimal_localized as _format_decimal_localized

    return _format_decimal_localized(value, locale=locale or get_locale())


__all__ = [
    "DEFAULT_LOCALE",
    "format_datetime_localized",
    "format_decimal_localized",
    "format_number_localized",
    "get_locale",
    "reload_translations",
    "set_locale",
    "t",
    "tn",
    "use_locale",
]
