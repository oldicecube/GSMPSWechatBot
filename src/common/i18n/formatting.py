from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from babel import Locale
from babel.dates import format_datetime as babel_format_datetime
from babel.numbers import format_decimal as babel_format_decimal

from .loaders import DEFAULT_LOCALE, to_babel_locale


def select_plural_category(locale: str, count: int | float | Decimal) -> str:
    babel_locale = Locale.parse(to_babel_locale(locale))
    return str(babel_locale.plural_form(count))


def format_datetime_localized(value: datetime | date, locale: str = DEFAULT_LOCALE, format: str = "medium") -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    return babel_format_datetime(value, format=format, locale=to_babel_locale(locale))


def format_number_localized(value: int | float | Decimal, locale: str = DEFAULT_LOCALE) -> str:
    return format_decimal_localized(value, locale=locale)


def format_decimal_localized(value: int | float | Decimal, locale: str = DEFAULT_LOCALE) -> str:
    return babel_format_decimal(value, locale=to_babel_locale(locale))
