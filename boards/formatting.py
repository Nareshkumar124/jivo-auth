"""
Number, date and JSON helpers shared by every board service.

The factory's boards each carry a copy of ``_f``, ``_tons``, ``_lakhs`` and
``_as_date``; this is the one copy here. Payloads are plain JSON (dates as
ISO strings, decimals as floats) so they can sit in the cache and be served
as-is from the data endpoint.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

#: The business's fixed rule: tons = litres / 1000 (density 1.0 where edible
#: oil is about 0.91, so tonnage reads ~10% above a weighbridge). Shared by
#: every board so two screens can't disagree about the same warehouse.
LITRES_PER_TON = 1000


def f(value: Any) -> float:
    """A float, with None and bad input as 0.0."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def f_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def tons(litres: Any) -> float:
    return round(f(litres) / LITRES_PER_TON, 2)


def lakhs(amount: Any) -> str:
    """``12.3 L`` / ``1.20 Cr``, for a subtitle."""
    value = f(amount)
    if abs(value) >= 1_00_00_000:
        return f"{value / 1_00_00_000:.2f} Cr"
    return f"{value / 1_00_000:.1f} L"


def pct(part: Any, whole: Any) -> Optional[float]:
    """``part`` as a percentage of ``whole``, or None when there is no whole."""
    denominator = f_or_none(whole)
    numerator = f_or_none(part)
    if not denominator or numerator is None:
        return None
    return round(numerator / denominator * 100, 1)


def as_date(value: Any) -> Optional[date]:
    """A date from whatever a driver hands back (date, datetime, ISO string)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def iso(value: Any) -> Optional[str]:
    """A date or datetime as an ISO string, or None."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def tz() -> ZoneInfo:
    return ZoneInfo(settings.BOARDS_TIME_ZONE)


def now() -> datetime:
    """The factory's local time now."""
    return timezone.now().astimezone(tz())


def today() -> date:
    """The factory's local date. Never UTC: a wall board's "today" must not
    roll over at half past five in the evening."""
    return now().date()


def month_start(day: date) -> date:
    return day.replace(day=1)


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def days_ago(day: date, days: int) -> date:
    return day - timedelta(days=days)


def jsonable(value: Any) -> Any:
    """A payload as plain JSON types, recursively."""
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value
