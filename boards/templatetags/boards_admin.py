"""
The Admin board's presentation: what the factory's React page
(``AdminControlDashboardPage``, ``AdminDonut``, ``AdminActions`` and the
``utils/format.ts`` formatters) decides between the payload and the screen.

The page computes no business figure, and neither does this: every number
and every alert arrives decided from the service. What lives here is what
the page itself decides: a tile's tag and its tone, a bar's width, which
words sit under a figure, the donut's arcs. ``{% admin_view board as v %}``
hands the template one dict per tile, already formatted, so the template is
markup and nothing else.

THE RULE EVERY FORMATTER OBEYS
A figure with no source renders as a rule, never as a zero. ``None``, a
string, NaN: anything that is not a finite number becomes "—".
"""

from __future__ import annotations

import math
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from django import template

from .boards import MONTHS, indian, num

register = template.Library()

NO_VALUE = "—"

#: Alerts the action centre shows before it starts hiding them.
MAX_ALERTS = 7

#: The cost donut's four hues, in fixed order, keyed by the server's slice
#: key: the one licensed exception to "composition is tints of one hue",
#: because four unrelated cost lines are categorical identity. Validated on
#: the factory's side; every slice is also direct-labelled, so identity is
#: never carried by colour alone.
COST_COLOURS = {
    "labour": "#2563EB",
    "electricity": "#0D9488",
    "salary": "#7C3AED",
    "maintenance": "#DB2777",
}

#: Donut geometry, in the SVG's own 128-unit box.
RADIUS = 54
CIRCUMFERENCE = 2 * math.pi * RADIUS
#: The gap between two arcs, taken off the arc rather than added between, so
#: the ring still adds up to the whole.
GAP = 3

SEVERITY_CLASS = {"critical": "adm-a-crit", "warning": "adm-a-warn", "info": "adm-a-info"}
SEVERITY_MARK = {"critical": "!", "warning": "▲", "info": "i"}
SEVERITY_WORD = {"critical": "Critical", "warning": "Warning", "info": "Note"}

#: The sections as the action centre names them when they could not be read.
SECTION_LABELS = {
    "production": "Production",
    "dispatch": "Dispatch",
    "fg_storage": "FG storage",
    "pm_storage": "PM storage",
    "oil_storage": "Oil storage",
    "cost": "Cost",
    "alerts": "Alerts",
}

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------------------
# formatters (utils/format.ts)
# ---------------------------------------------------------------------------


def _round_half_up(value: float, digits: int = 0) -> Decimal:
    """JavaScript's rounding for display, not Python's banker's rounding."""
    quantum = Decimal(1).scaleb(-digits)
    return Decimal(repr(value)).quantize(quantum, rounding=ROUND_HALF_UP)


def _grouped(value: float, digits: int) -> str:
    """Indian grouping with exactly ``digits`` decimals, rounded half up."""
    rounded = _round_half_up(value, digits)
    negative = rounded < 0
    rounded = abs(rounded)
    whole_part = int(rounded)
    text = indian(whole_part)
    if digits:
        fraction = f"{rounded - whole_part:.{digits}f}"[1:]
        text += fraction
    return ("-" if negative and rounded else "") + text


@register.filter
def tons(value: Any) -> str:
    """A tonnage to one decimal; above ten thousand, whole."""
    n = num(value)
    if n is None:
        return NO_VALUE
    if abs(n) >= 10_000:
        return _grouped(n, 0)
    return _grouped(n, 1)


@register.filter
def whole(value: Any) -> str:
    """Whole number, Indian grouping."""
    n = num(value)
    return NO_VALUE if n is None else _grouped(n, 0)


@register.filter
def money(value: Any) -> str:
    """Rupees at the scale a factory reads them: Cr, then L, then rupees."""
    n = num(value)
    if n is None:
        return NO_VALUE
    if abs(n) >= 10_000_000:
        return f"₹{_round_half_up(n / 10_000_000, 2)} Cr"
    if abs(n) >= 100_000:
        return f"₹{_round_half_up(n / 100_000, 2)} L"
    return f"₹{_grouped(n, 0)}"


def money_parts(value: Any) -> Dict[str, str]:
    """Rupees split into the figure and its unit, for a tile's big number."""
    n = num(value)
    if n is None:
        return {"value": NO_VALUE, "unit": ""}
    if abs(n) >= 10_000_000:
        return {"value": f"₹{_round_half_up(n / 10_000_000, 2)}", "unit": "Cr"}
    if abs(n) >= 100_000:
        return {"value": f"₹{_round_half_up(n / 100_000, 2)}", "unit": "L"}
    return {"value": f"₹{_grouped(n, 0)}", "unit": ""}


@register.filter
def pct(value: Any) -> str:
    """A percentage, to one decimal."""
    n = num(value)
    return NO_VALUE if n is None else f"{_round_half_up(n, 1)}%"


@register.filter
def pct_rough(value: Any) -> str:
    """A percentage with no decimal, for prose."""
    n = num(value)
    return NO_VALUE if n is None else f"{_round_half_up(n, 0)}%"


def bar(value: Any) -> float:
    """A bar width, clamped to the track. Over capacity clamps to full: the
    tile's own percentage reports the overrun."""
    n = num(value)
    if n is None:
        return 0.0
    return max(0.0, min(100.0, n))


def _width(value: float) -> str:
    """A width for a ``style`` attribute: never localised, never NaN."""
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def fill_condition(used: Any) -> str:
    """How full is too full: 90 bad, 80 warn, the action centre's own
    thresholds, so a tile in the bad tint is one the alerts also shout about."""
    n = num(used)
    if n is None:
        return "ok"
    if n >= 90:
        return "bad"
    if n >= 80:
        return "warn"
    return "ok"


def short_oil(name: Optional[str]) -> str:
    """"SOYABEAN REFINED LOOSE OIL" -> "Soyabean": the variety is the word
    that tells one tank line from another, and the legend has room for one."""
    if not name:
        return NO_VALUE
    first = name.strip().split()[0] if name.strip() else name
    return first[:1] + first[1:].lower()


def _date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def short_window(start: Any, end: Any) -> str:
    """"1–15 Sep": the window, short enough for a donut's hole."""
    first, last = _date(start), _date(end)
    if not first or not last:
        return ""
    return f"{first.day}–{last.day} {MONTHS[last.month - 1]}"


def long_date(value: Any) -> str:
    """"Wednesday, 14 Oct 2026", under the clock."""
    day = _date(value)
    if not day:
        return ""
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]} {day.year}"


# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------


def _meter(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """An OpsMeter: segments laid in order on one scale, a legend for the
    labelled ones. A sliver keeps a round dot's width: almost none and none
    are different answers."""
    drawn = []
    for index, segment in enumerate(segments):
        share = bar(segment.get("pct"))
        drawn.append(
            {
                "fill": segment["fill"],
                "width": _width(share),
                "sliver": share > 0,
                "delay": f"{index * 0.1:.1f}s" if index > 0 else "",
            }
        )
    legend = [
        {"fill": segment["fill"], "label": segment["label"], "figure": segment.get("figure")}
        for segment in segments
        if segment.get("label")
    ]
    return {"segments": drawn, "legend": legend}


def _corner(label: str, value: str, unit: str = "", note: Optional[str] = None) -> Dict[str, Any]:
    return {"label": label, "value": value, "unit": unit, "note": note}


# ---------------------------------------------------------------------------
# the tiles
# ---------------------------------------------------------------------------


def _production(production: Optional[Dict[str, Any]], elapsed_pct: Any) -> Optional[Dict[str, Any]]:
    if not production:
        return None
    plan_pct = num(production.get("plan_pct"))
    elapsed = num(elapsed_pct)

    # The tag is the GAP against the month already spent, not the raw
    # percentage: 27% of plan is alarming on the 15th and fine on the 4th.
    if plan_pct is None:
        tag = {"label": "no plan filed", "tone": "nil"}
    elif elapsed is not None and elapsed - plan_pct >= 10:
        tag = {"label": f"{pct_rough(plan_pct)} of plan", "tone": "bad"}
    elif elapsed is not None and elapsed > plan_pct:
        tag = {"label": f"{pct_rough(plan_pct)} of plan", "tone": "warn"}
    else:
        tag = {"label": f"{pct_rough(plan_pct)} of plan", "tone": "ok"}

    avg = production.get("avg_tons_per_producing_day")
    plan_tons = production.get("plan_tons")
    segments = [
        {"fill": "main", "pct": plan_pct},
        {
            "fill": "mute",
            "pct": 0 if plan_pct is None else 100 - plan_pct,
            "label": "Plan",
            "figure": f"{tons(plan_tons)} T" if plan_tons is not None else NO_VALUE,
        },
        # A legend entry with no width: "how much of the plan is done" read
        # against "how much of the month is gone", without a second chart.
        {"fill": "mute", "pct": 0, "label": "Month gone", "figure": pct(elapsed)},
    ]
    if production.get("unplanned_tons"):
        segments.append(
            {"fill": "mute", "pct": 0, "label": "Unplanned", "figure": f"{tons(production['unplanned_tons'])} T"}
        )

    return {
        "tag": tag,
        "value": tons(production.get("mtd_tons")),
        "corner": _corner(
            "Today",
            tons(production.get("today_tons")),
            "T",
            f"against a {tons(avg)} T average" if avg is not None else None,
        ),
        "meter": _meter(segments),
    }


def _dispatch(dispatch: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not dispatch:
        return None
    total = num(dispatch.get("mtd_tons")) or 0.0
    companies = {row.get("company_code"): row for row in dispatch.get("companies") or []}
    oil = companies.get("JIVO_OIL") or {}
    mart = companies.get("JIVO_MART") or {}

    def share(value: Any) -> float:
        n = num(value)
        return n / total * 100 if total > 0 and n is not None else 0.0

    # The billed figure beside the shipped one: not a discrepancy to
    # reconcile away, but how much of this month's billing is still standing.
    invoiced = num(dispatch.get("invoiced_tons"))
    if invoiced is None:
        note = "Billed this month could not be read from SAP."
    elif invoiced > total:
        note = f"{tons(invoiced)} T billed this month · {tons(invoiced - total)} T of it still to ship"
    else:
        note = f"{tons(invoiced)} T billed this month · {tons(total - invoiced)} T of what shipped was billed earlier"

    avg = dispatch.get("avg_tons_per_dispatch_day")
    today = num(dispatch.get("today_tons")) or 0.0
    return {
        "tag": {"label": f"{tons(avg)} T a day", "tone": "neut"} if avg is not None else None,
        # "Left the gate" said out loud: the production tile beside it is
        # measured against a plan, and these are not on the same clock.
        "sub": (
            f"{whole(dispatch.get('trucks'))} trucks · {whole(dispatch.get('bills'))} bills "
            f"left the gate over {dispatch.get('dispatch_days')} days"
        ),
        "value": tons(dispatch.get("mtd_tons")),
        "corner": _corner(
            "Today", tons(dispatch.get("today_tons")), "T", None if today > 0 else "nothing out yet"
        ),
        "meter": _meter(
            [
                {"fill": "main", "pct": share(oil.get("tons")), "label": "Oil", "figure": f"{tons(oil.get('tons'))} T"},
                {"fill": "light", "pct": share(mart.get("tons")), "label": "Mart", "figure": f"{tons(mart.get('tons'))} T"},
            ]
        ),
        "note": note,
    }


def _fg(fg: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not fg:
        return None
    # The fullest rated store decides the condition: a combined 52% can hide
    # one store with half a day of headroom, and headroom is what is acted on.
    rated = [row for row in fg.get("rows") or [] if num(row.get("used_pct")) is not None]
    fullest = max(rated, key=lambda row: num(row.get("used_pct")), default=None)
    tag = None
    if fullest:
        condition = fill_condition(fullest.get("used_pct"))
        tag = {"label": f"{fullest.get('label')} {pct_rough(fullest.get('used_pct'))}", "tone": condition}

    capacity = fg.get("capacity_tons")
    sub = "Finished goods · " + (f"{tons(capacity)} T rated" if capacity is not None else "not every store is rated")
    if (fg.get("unweighed_items") or 0) > 0:
        sub += f" · {fg['unweighed_items']} SKUs have no case weight"

    rows = []
    for index, row in enumerate(fg.get("rows") or []):
        figure = (
            f"{tons(row.get('tons'))} / {tons(row.get('capacity_tons'))} T"
            if row.get("capacity_tons") is not None
            else f"{tons(row.get('tons'))} T"
        )
        rows.append(
            {
                "label": row.get("label"),
                "figure": figure,
                "width": _width(bar(row.get("used_pct"))),
                "fill": "main" if index == 0 else "light",
            }
        )

    unrated = fg.get("unrated") or []
    note = None
    if unrated:
        # Named on the tile because the total deliberately excludes it, and a
        # tonnage that appears in no total is a tonnage nobody manages.
        note = {
            "fill": "mute",
            "label": "Outside the rating: " + ", ".join(row.get("warehouse") for row in unrated),
            "figure": f"{tons(sum(num(row.get('tons')) or 0 for row in unrated))} T",
        }

    return {
        "tag": tag,
        "sub": sub,
        "value": tons(fg.get("total_tons")),
        "corner": _corner(
            "Space used",
            pct(fg.get("used_pct")) if fg.get("used_pct") is not None else NO_VALUE,
            "",
            f"{tons(fg.get('free_tons'))} T still free" if fg.get("free_tons") is not None else "a store here has no rating",
        ),
        "rows": rows,
        "note": note,
    }


def _pm(pm: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not pm:
        return None
    used = num(pm.get("used_pct"))
    headline = money_parts(pm.get("total_value"))
    return {
        "value": headline["value"],
        "unit": headline["unit"],
        "corner": _corner(
            "Space used",
            NO_VALUE if used is None else pct(used),
            "",
            f"{whole(pm.get('free_sqft'))} sq ft free" if pm.get("free_sqft") is not None else "pallet footprint not set",
        ),
        # No figure for the floor means a hatched track, never an empty one:
        # an empty track reads as "0% full".
        "unknown": used is None,
        "meter": None
        if used is None
        else _meter(
            [
                {"fill": "main", "pct": used, "label": "Filled", "figure": f"{whole(pm.get('occupied_sqft'))} sq ft"},
                {"fill": "mute", "pct": 100 - used, "label": "Free", "figure": f"{whole(pm.get('free_sqft'))} sq ft"},
            ]
        ),
    }


def _oil(oil: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not oil:
        return None
    used = num(oil.get("used_pct"))
    rated = used is not None
    # EMPTY, not used: the question an owner asks a tank farm is how much
    # more it will take.
    empty_pct = round((100 - used) * 10) / 10 if rated else None
    capacity = num(oil.get("capacity_tons"))
    total = num(oil.get("total_tons"))
    free = round((capacity - total) * 10) / 10 if capacity is not None and total is not None else None
    keys = ["main", "light", "mute"]
    return {
        "tag": {"label": f"loose oil, {oil.get('warehouse')}", "tone": "neut"},
        # With no rating the litres and the rule are the only basis a reader
        # has, and this tile's basis differs from the tiles beside it.
        "sub": "" if rated else f"{whole(oil.get('total_litres'))} litres in tank · 1,000 L = 1 T",
        "value": tons(oil.get("total_tons")),
        "corner": _corner("Empty", pct(empty_pct) if empty_pct is not None else NO_VALUE, "", None if rated else "no tank rating yet"),
        "rated": rated,
        "width": _width(bar(used)),
        "free": tons(free),
        "capacity": tons(capacity),
        "items": [
            {"fill": keys[index], "label": short_oil(row.get("label")), "figure": f"{tons(row.get('tons'))} T"}
            for index, row in enumerate((oil.get("rows") or [])[:3])
        ],
    }


def _today_label(line: Dict[str, Any]) -> Optional[str]:
    """What a cost line cost today, or why it is nil, or nothing.

    Nil today on a line with a month behind it prints the server's reason:
    electricity at ₹0 means today's reading is not in, not that the plant
    drew no power. Nil today on a line nil all month prints nothing.
    """
    today = num(line.get("today"))
    if today is None:
        return None
    if today > 0:
        count = num(line.get("today_detail_value"))
        unit = line.get("today_detail_unit")
        counted = f" · {whole(count)} {unit}" if count is not None and count > 0 and unit else ""
        return f"{money(today)}{counted} today"
    if (num(line.get("amount")) or 0) <= 0:
        return None
    return line.get("today_detail") or "nothing today"


def _cost(cost: Optional[Dict[str, Any]], period: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not cost:
        return None
    slices = cost.get("slices") or []
    total = num(cost.get("total")) or 0.0

    # NOT "reads zero": a line can be nil from a source that works.
    # Unsourced means nothing is configured behind it, the action centre's test.
    unsourced = sum(1 for line in slices if not line.get("has_source"))
    tag = (
        {"label": f"{unsourced} of {len(slices)} unsourced", "tone": "bad"}
        if unsourced
        else {"label": "all four sourced", "tone": "ok"}
    )

    # A nil line contributes no arc: a zero-width arc is invisible and would
    # reduce the donut to the configured lines. It keeps its legend row.
    arcs = []
    offset = 0.0
    for line in slices:
        amount = num(line.get("amount")) or 0.0
        if amount <= 0:
            continue
        length = amount / total * CIRCUMFERENCE if total > 0 else 0.0
        drawn = max(length - GAP, 0.0)
        arcs.append(
            {
                "key": line.get("key"),
                "label": line.get("label"),
                "colour": COST_COLOURS.get(line.get("key"), "var(--d2)"),
                "dash": f"{drawn:.3f} {CIRCUMFERENCE - drawn:.3f}",
                # Plus zero, so the first arc's offset is "0.000", not "-0.000".
                "offset": f"{-offset + 0.0:.3f}",
            }
        )
        offset += length

    legend = []
    for line in slices:
        amount = num(line.get("amount")) or 0.0
        nil = amount <= 0
        today = _today_label(line)
        spent_today = (num(line.get("today")) or 0) > 0
        title = " · ".join(
            part for part in (line.get("today_detail") if spent_today else None, line.get("warning"), line.get("basis")) if part
        )
        legend.append(
            {
                "key": line.get("key"),
                "label": line.get("label"),
                "colour": COST_COLOURS.get(line.get("key"), "var(--d2)"),
                "nil": nil,
                "money": money(amount),
                "share": ("no rate" if line.get("warning") else "nil") if nil else pct_rough(line.get("share_pct")),
                "detail": line.get("detail"),
                "today": today,
                "title": title,
            }
        )

    described = ", ".join(f"{line.get('label')} {money(line.get('amount'))}" for line in slices if (num(line.get("amount")) or 0) > 0)
    window = short_window(period.get("from"), period.get("to"))
    avg = cost.get("avg_per_day")
    return {
        "tag": tag,
        "total": money(cost.get("total")),
        "window": window,
        "aria": f"Factory cost {window}: {described or 'no cost line has a configured source'}.",
        "arcs": arcs,
        "legend": legend,
        "foot": f"{money(avg)} a day on average this month" if num(avg) is not None else None,
    }


def _actions(alerts: Optional[List[Dict[str, Any]]], degraded: List[str]) -> Optional[Dict[str, Any]]:
    """The action centre. No alerts because every rule passed, and no alerts
    because the tiles they read never loaded, must not look alike: a
    degraded board says so instead of showing an all-clear."""
    if alerts is None:
        return None
    counts = []
    for severity in ("critical", "warning", "info"):
        n = sum(1 for alert in alerts if alert.get("severity") == severity)
        if n:
            counts.append({"severity": severity, "text": f"{n} {SEVERITY_WORD[severity].lower()}"})

    shown = [
        {
            **alert,
            "css": SEVERITY_CLASS.get(alert.get("severity"), "adm-a-info"),
            "mark": SEVERITY_MARK.get(alert.get("severity"), "i"),
            "word": SEVERITY_WORD.get(alert.get("severity"), "Note"),
        }
        for alert in alerts[:MAX_ALERTS]
    ]
    hidden = len(alerts) - len(shown)
    return {
        "counts": counts,
        "alerts": shown,
        "degraded": (
            f"{', '.join(SECTION_LABELS.get(name, name) for name in degraded)} — the rules that watch "
            f"{'it' if len(degraded) == 1 else 'them'} "
            "have raised nothing because they saw nothing."
            if degraded
            else None
        ),
        "clear": not alerts and not degraded,
        "hidden": f"{hidden} more {'alert' if hidden == 1 else 'alerts'} below the fold." if hidden > 0 else None,
    }


def _topbar(board: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    output = board.get("output") or {}
    production = output.get("production")
    dispatch = output.get("dispatch")
    alerts = board.get("alerts")
    period = meta.get("period") or {}
    criticals = sum(1 for alert in alerts or [] if alert.get("severity") == "critical")
    return {
        "scope": meta.get("scope") or "",
        "chips": [
            {"label": "Window", "value": f"{period.get('from')} to {period.get('to')}" if period else NO_VALUE},
            {
                "label": "Month",
                "value": f"day {period.get('day_of_month')} of {period.get('days_in_month')}" if period else NO_VALUE,
            },
        ],
        "totals": [
            {
                "caption": "Produced",
                "value": f"{tons((production or {}).get('mtd_tons'))} T",
                # "No plan filed" under a figure SAP never answered for would be
                # a claim about the planner made on no evidence.
                "sub": (
                    "not read"
                    if not production
                    else f"{pct_rough(production.get('plan_pct'))} of plan"
                    if production.get("plan_pct") is not None
                    else "no plan filed"
                ),
                "missing": not production,
            },
            {
                "caption": "Dispatched",
                "value": f"{tons((dispatch or {}).get('mtd_tons'))} T",
                # Names the basis, not the companies: which EVENT this counts
                # is what a reader needs before comparing it with anything.
                "sub": "left the gate",
                "missing": not dispatch,
            },
            {
                "caption": "Open alerts",
                "value": str(len(alerts or [])),
                "sub": "not read" if alerts is None else f"{criticals} need a decision" if criticals else "none critical",
                "missing": alerts is None,
            },
        ],
        "date": long_date(meta.get("date")),
    }


def present(board: Dict[str, Any]) -> Dict[str, Any]:
    """The whole page's view-model, one dict per tile (None where the
    section could not be read)."""
    board = board or {}
    meta = board.get("meta") or {}
    output = board.get("output") or {}
    storage = board.get("storage") or {}
    period = meta.get("period") or {}
    return {
        "topbar": _topbar(board, meta),
        "production": _production(output.get("production"), period.get("elapsed_pct")),
        "dispatch": _dispatch(output.get("dispatch")),
        "fg": _fg(storage.get("fg")),
        "pm": _pm(storage.get("pm")),
        "oil": _oil(storage.get("oil")),
        "cost": _cost(board.get("cost"), period),
        "actions": _actions(board.get("alerts"), list(meta.get("degraded") or [])),
    }


@register.simple_tag
def admin_view(board: Dict[str, Any]) -> Dict[str, Any]:
    """``{% admin_view board as v %}``."""
    return present(board)
