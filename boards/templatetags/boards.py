"""
Formatting filters for the board templates: the factory boards' own
formatters (``whole``, ``qty``, ``money``, ``decimal``, ``dayLabel``,
``share``), so a figure reads the same on this wall as on the factory's.

Every one goes through ``num()``: a field the service did not send, or sent
as null, degrades to a rule and never to "None" or "nan" on a factory wall.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from django import template

register = template.Library()

RULE = "—"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def num(value: Any) -> Optional[float]:
    """A usable number, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def indian(number: float, digits: int = 0) -> str:
    """Indian grouping: 12,34,567 / 1,23,45,678.90.

    Rounded before the whole part and the fraction are split, so 612.97 to
    one place is 613.0 (not 612.0), and halves round up as the factory's
    browser does (32.5 → 33, where Python's round() would give 32).
    """
    rounded = Decimal(repr(abs(number))).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    negative = number < 0 and rounded != 0
    whole_part = int(rounded)
    fraction = "" if digits == 0 else f"{rounded - whole_part:.{digits}f}"[1:]

    text = str(whole_part)
    if len(text) > 3:
        head, tail = text[:-3], text[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        text = ",".join(groups) + "," + tail

    return ("-" if negative else "") + text + fraction


@register.filter
def whole(value: Any) -> str:
    """Whole number, Indian grouping."""
    number = num(value)
    return RULE if number is None else indian(round(number))


@register.filter
def qty(value: Any) -> str:
    """A quantity sized for a wall: lakhs and crores past a lakh."""
    number = num(value)
    if number is None:
        return RULE
    rounded = round(number)
    if abs(rounded) >= 1_00_00_000:
        return f"{rounded / 1_00_00_000:.2f} Cr"
    if abs(rounded) >= 1_00_000:
        return f"{rounded / 1_00_000:.2f} L"
    return indian(rounded)


@register.filter
def money(value: Any) -> str:
    """Indian-grouped rupees, compacted. A board has no room for paise."""
    number = num(value)
    if number is None:
        return RULE
    magnitude = abs(number)
    if magnitude >= 1_00_00_000:
        return f"₹{number / 1_00_00_000:.2f} Cr"
    if magnitude >= 1_00_000:
        return f"₹{number / 1_00_000:.1f} L"
    if magnitude >= 1_000:
        return f"₹{number / 1_000:.1f}k"
    return f"₹{round(number)}"


@register.filter
def money_full(value: Any) -> str:
    """Rupees in full, Indian grouping, no paise."""
    number = num(value)
    return RULE if number is None else f"₹{indian(round(number))}"


@register.filter
def dec(value: Any, digits: Any = 1) -> str:
    """One decimal (or ``digits``), where the fraction still carries meaning."""
    number = num(value)
    if number is None:
        return RULE
    try:
        places = int(digits)
    except (TypeError, ValueError):
        places = 1
    return indian(number, places)


@register.filter
def pct(value: Any, digits: Any = 0) -> str:
    """``12%`` / ``12.5%``."""
    number = num(value)
    if number is None:
        return RULE
    try:
        places = int(digits)
    except (TypeError, ValueError):
        places = 0
    return f"{number:.{places}f}%"


@register.filter
def signed(value: Any) -> str:
    """``+12`` / ``-3`` / ``0``, for a day-on-day change."""
    number = num(value)
    if number is None:
        return RULE
    rounded = round(number)
    return f"+{indian(rounded)}" if rounded > 0 else indian(rounded)


@register.filter
def day_label(value: Any) -> str:
    """An ISO date as ``10 Sep``. Parsed by hand: never shifted by a zone."""
    if isinstance(value, (date, datetime)):
        return f"{value.day} {MONTHS[value.month - 1]}"
    text = str(value or "")[:10]
    parts = text.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return RULE
    month = int(parts[1])
    if not 1 <= month <= 12:
        return RULE
    return f"{int(parts[2])} {MONTHS[month - 1]}"


@register.filter
def weekday(value: Any) -> str:
    """``Mon``, from an ISO date."""
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%a")
    except ValueError:
        return RULE


@register.filter
def share(part: Any, total: Any) -> float:
    """Share of a total as a bar width (0-100). Empty, never NaN."""
    numerator = num(part)
    denominator = num(total)
    if numerator is None or not denominator or denominator <= 0:
        return 0.0
    return max(0.0, min(100.0, numerator / denominator * 100))


@register.filter
def get(mapping: Any, key: Any) -> Any:
    """``dict|get:key``, for reasons keyed by section name."""
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def clock(value: Any) -> str:
    """``14:05`` from an ISO datetime, for a stamped time."""
    try:
        return datetime.fromisoformat(str(value)).strftime("%H:%M")
    except ValueError:
        return RULE


#: Words written as people write them, not capitalized: "pos" is "POs".
ACRONYMS = {
    word: word.upper()
    for word in ("ho", "sap", "pf", "bst", "po", "grpo", "hr", "etp", "fg", "rm", "pm", "kl", "atm", "id")
}
ACRONYMS.update({"pos": "POs", "grpos": "GRPOs"})


@register.filter
def section_label(value: Any) -> str:
    """A payload key as people read it: "cash_issued" → "Cash issued",
    "pending_ho" → "Pending HO". For the not-read chips under a board."""
    words = [word for word in str(value or "").replace("-", "_").split("_") if word]
    if not words:
        return ""
    first_is_acronym = words[0].lower() in ACRONYMS
    words = [ACRONYMS.get(word.lower(), word.lower()) for word in words]
    if not first_is_acronym:
        words[0] = words[0].capitalize()
    return " ".join(words)
