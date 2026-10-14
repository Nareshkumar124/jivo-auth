"""
The plant board's page, as data: a port of the factory's
``PlantBoardDashboardPage.tsx`` (and the shared ``OpsTopbar``, ``OpsBand``,
``OpsGroup``, ``OpsMeter``, ``OpsBars`` and ``OpsPeople`` it draws with).

The service returns the factory's payload, figures only. Everything the
React page computed on top of it (the still-to-buy share, which pill a tile
wears and in which tone, the meter's segments and their legend, the seven
bars, the people strips) is computed here, once, so the templates only lay
out what this hands them. ``boards_plant.plant_view`` calls :func:`present`.

The page's rules, kept:

* **A figure with no source does not look like a figure.** Every formatter
  degrades to a rule, never to "None" or "nan".
* **A section that could not be read keeps its place and says so.** Here a
  tile rather than a band (the service degrades tile by tile), drawn as an
  ``.ops-unread`` tile with the reason.
* **Colour carries meaning and nothing else.** Segments name a weight of the
  band's own hue (``main``/``light``/``mute``), never a colour.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from ..templatetags.boards import MONTHS, day_label, dec, money, num, qty, share, whole

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

#: The band columns the factory page passes to each ``OpsBand``. Every track
#: is ``minmax(0, …)``: a bare ``fr`` floors at its content and pushes the
#: people strip off the band's right-hand edge.
PURCHASE_COLUMNS = "minmax(0, 1.05fr) minmax(0, 1.15fr) minmax(0, 1fr) minmax(0, 1fr)"
STORE_COLUMNS = PURCHASE_COLUMNS
PRODUCTION_COLUMNS = "minmax(0, 1.1fr) minmax(0, 1.1fr) minmax(0, 1.05fr) minmax(0, 1.05fr)"
SHIFTING_COLUMNS = "minmax(0, 1fr) minmax(0, 1fr)"

#: The three routes off the floor, always the same tint: colour follows the
#: destination, never its size.
ROUTE_FILL = {"BH-BT": "main", "DISPATCH": "light", "ELSEWHERE": "mute"}
ROUTE_LABEL = {"BH-BT": "BH-BT basement", "DISPATCH": "Dispatch to Mart", "ELSEWHERE": "Elsewhere"}

#: The plain-English name of each tile, for the unread state.
TILE_NAMES = {
    "purchase_plan": "Plan this month",
    "purchased": "Purchased",
    "benchmark": "Against benchmark",
    "open_pos": "Open POs",
    "stock_space": "Stock space",
    "non_moving": "Non-moving stock",
    "pm_in": "Packing material in",
    "blowing": "Blowing this month",
    "today_lines": "Today on the lines",
    "monthly_planning": "Monthly planning",
    "floor_stock": "Total stock",
    "wastage": "Wastage",
    "declared": "Declared today",
    "shipped": "Shipped today",
}


# ---------------------------------------------------------------------------
# the page's own formatters (the shared ones come from templatetags.boards)
# ---------------------------------------------------------------------------


def pct_label(part: Any, total: Any) -> str:
    """A share for a tag, which says nothing rather than 0% when it cannot be
    computed (the page's ``pct``)."""
    p, t = num(part), num(total)
    if p is None or t is None or t <= 0:
        return "—"
    return f"{round(p / t * 100)}%"


def js_number(value: Any) -> str:
    """A number as JavaScript prints it in a template string: ``81.3``,
    ``90``. The page puts the attainment and the plan's day count in text
    this way."""
    n = num(value)
    if n is None:
        return "—"
    if n == int(n):
        return str(int(n))
    return repr(round(n, 6))


def plural(count: Any, word: str, suffix: str = "s") -> str:
    return word if num(count) == 1 else word + suffix


def width(value: float) -> str:
    """A percentage for an inline style: clamped, and with a dot whatever the
    locale (``50.00``)."""
    return f"{max(0.0, min(100.0, float(value or 0))):.2f}"


def segment(fill: str, pct: float, label: Optional[str] = None, figure: Optional[str] = None) -> Dict[str, Any]:
    """One part of an ``OpsMeter``. A segment worth a sliver still shows: the
    template gives any positive share the bar's own height as a minimum."""
    return {"fill": fill, "pct": width(pct), "visible": (pct or 0) > 0, "label": label, "figure": figure}


def meter(*segments: Dict[str, Any]) -> Dict[str, Any]:
    parts = [part for part in segments if part]
    for index, part in enumerate(parts):
        # The page staggers the grow animation one tenth of a second a part.
        part["delay"] = f"{index * 0.1:.1f}" if index else ""
    return {"kind": "meter", "segments": parts, "keys": [part for part in parts if part["label"]]}


def bars(days: List[Dict[str, Any]], key: str, fmt) -> Dict[str, Any]:
    """An ``OpsBars``: the trailing days, each a share of the best day in the
    window (not of a target), the last drawn as today. A day of nothing has
    no figure over it and no stub: an empty column already says it."""
    best = max([1.0] + [num(day.get(key)) or 0.0 for day in days])
    out = []
    for index, day in enumerate(days):
        value = num(day.get(key)) or 0.0
        pct = share(value, best)
        last = index == len(days) - 1
        out.append(
            {
                "label": "Today" if last else str(day.get("date") or "")[8:10],
                "height": width(max(2.0, min(100.0, pct))) if pct > 0 else "0.00",
                "value": fmt(value) if value > 0 else "",
                "now": last,
                "delay": f"{0.02 + index * 0.06:.2f}",
            }
        )
    return {"kind": "bars", "days": out}


def tag(label: str, tone: str = "neut") -> Dict[str, str]:
    return {"label": label, "tone": tone}


def tile(key: str, name: str, *, tag=None, sub: str = "", value: str = "", unit: str = "", viz=None) -> Dict[str, Any]:
    return {"key": key, "name": name, "tag": tag, "sub": sub, "value": value, "unit": unit, "viz": viz, "unread": None}


def unread(key: str, reason: Optional[str]) -> Dict[str, Any]:
    """A tile whose section is None: its name, a "not read" pill and the
    reason, never a zero."""
    return {
        "key": key,
        "name": TILE_NAMES.get(key, key),
        "tag": tag("not read", "nil"),
        "sub": "",
        "value": "",
        "unit": "",
        "viz": None,
        "unread": reason or "This tile could not be read on the last refresh.",
    }


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


def present(board: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The whole page for one payload: the header and the four bands."""
    board = board or {}
    meta = meta if meta is not None else (board.get("meta") or {})
    reasons = meta.get("reasons") or {}
    workforce = board.get("workforce")

    def build(key: str, draw) -> Dict[str, Any]:
        data = board.get(key)
        if data is None:
            return unread(key, reasons.get(key))
        return draw(key, data)

    return {
        "topbar": _topbar(board, meta),
        "bands": [
            _band(
                "purchase",
                "Purchase",
                "BH-PM · BH-BS · BH-NM · BH-PC",
                PURCHASE_COLUMNS,
                [
                    build("purchase_plan", _purchase_plan),
                    build("purchased", _purchased),
                    build("benchmark", _benchmark),
                    build("open_pos", _open_pos),
                ],
                people_for(workforce, "purchase"),
            ),
            _band(
                "store",
                "Store",
                "Packaging",
                STORE_COLUMNS,
                [
                    build("stock_space", _stock_space),
                    build("non_moving", _non_moving),
                    build("pm_in", _pm_in),
                    build("blowing", _blowing),
                ],
                people_for(workforce, "store"),
            ),
            _band(
                "production",
                "Production",
                "BH-PF",
                PRODUCTION_COLUMNS,
                [
                    build("today_lines", _today_lines),
                    build("monthly_planning", _monthly_planning),
                    build("floor_stock", _floor_stock),
                    build("wastage", _wastage),
                ],
                people_for(workforce, "production"),
            ),
            _band(
                "shifting",
                "Shifting",
                "Off BH-PF",
                SHIFTING_COLUMNS,
                [
                    build("declared", _declared),
                    build("shipped", _shipped),
                ],
                people_for(workforce, "shifting"),
            ),
        ],
    }


def _band(domain: str, title: str, scope: str, columns: str, tiles, people) -> Dict[str, Any]:
    return {"domain": domain, "title": title, "scope": scope, "columns": columns, "tiles": tiles, "people": people}


# ------------------------------------------------------------------ header


def _topbar(board: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    """Who is in the plant, and which plan the bands are measured against.

    The totals carry the WORKFORCE, not a summary of the bands: a figure
    appears in exactly one place, and the place is its own band. The plan is
    named rather than the calendar month, because it is the window two of the
    four bands are measured over.
    """
    plan = board.get("plan")
    degraded = meta.get("degraded") or []
    warnings = meta.get("warnings") or []
    workforce = board.get("workforce")
    company = meta.get("company_code") or meta.get("company") or "Jivo Oil"

    if plan:
        plan_chip = (
            f"day {js_number(plan.get('days_elapsed'))} of {js_number(plan.get('days_total'))}"
            f"{'' if plan.get('is_current') else ' · not this month'}"
        )
    elif "plan" in degraded:
        # The factory says "none in SAP" whenever the plan is null; an unread
        # plan is not an absent one, so it says which here.
        plan_chip = "not read"
    else:
        plan_chip = "none in SAP"

    count = len(degraded)
    chips = [
        {"label": "Plan", "value": plan_chip},
        # Stated once, here, rather than on each tile that reads in tonnes.
        {"label": "Tonnage", "value": "1000 L = 1 t · oil and FG only"},
        {"label": "Feed", "value": f"{count} {plural(count, 'tile')} unread" if count else "all tiles current"},
    ]
    if warnings:
        chips.append({"label": "Check", "value": warnings[0]})

    total_salary = (workforce or {}).get("total_salary_monthly")
    per_day = (workforce or {}).get("total_cost_per_day")
    people = (workforce or {}).get("total_people")
    totals = [
        {
            "caption": "Total wage bill",
            "value": money(total_salary),
            "sub": "not configured" if per_day is None else f"{money(per_day)} a day",
            "missing": total_salary is None,
        },
        {"caption": "People on roll", "value": whole(people), "sub": _roll_sub(workforce), "missing": people is None},
        {
            "caption": "Employees against labour",
            "value": _roll_split(workforce),
            "sub": "on the payroll against hired in",
            "missing": people is None,
        },
    ]

    generated = str(meta.get("generated_at") or "")
    stamp = _stamp(meta)
    return {
        "title": "Plant Control",
        "scope": f"{company} · {(plan or {}).get('name') or 'no production plan'}",
        "chips": chips,
        "totals": totals,
        "time": generated[11:19] if len(generated) >= 19 else "—",
        "date": stamp,
    }


def _stamp(meta: Dict[str, Any]) -> str:
    """``Wednesday, 16 Sep 2026``, the clock's date line."""
    text = str(meta.get("generated_at") or meta.get("as_of") or meta.get("date") or "")[:10]
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return ""
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]} {day.year}"


def _roll_sub(workforce: Optional[Dict[str, Any]]) -> str:
    """How many departments the head count actually speaks for."""
    if not workforce or workforce.get("total_people") is None:
        return "nobody configured"
    missing = len(workforce.get("unconfigured") or [])
    if missing == 0:
        return "every department counted"
    return f"{whole(missing)} {plural(missing, 'department')} not counted"


def _roll_split(workforce: Optional[Dict[str, Any]]) -> str:
    """Staff against hired-in labour, or a rule when both halves are unknown."""
    bands = (workforce or {}).get("bands") or {}
    staff = hired = None
    for band in bands.values():
        if band.get("employees") is not None:
            staff = (staff or 0) + band["employees"]
        if band.get("labour") is not None:
            hired = (hired or 0) + band["labour"]
    if staff is None and hired is None:
        return "—"
    return f"{whole(staff)} · {whole(hired)}"


def people_for(workforce: Optional[Dict[str, Any]], band: str) -> Dict[str, Any]:
    """One band's people strip (``peopleFor`` + ``OpsPeople``).

    A band with nothing configured is four rules, never zeros. Purchase is
    permanently in that state: no department maps to it. The split bar is
    drawn only when both headcounts are known.
    """
    row = ((workforce or {}).get("bands") or {}).get(band)
    if not row or (row.get("employees") is None and row.get("labour") is None):
        employees = labour = employee_cost = labour_cost = None
    else:
        employees = row.get("employees")
        labour = row.get("labour")
        employee_cost = row.get("employee_cost_per_day")
        labour_cost = row.get("labour_cost_per_day")

    both = employees is not None and labour is not None
    on_shift = employees + labour if both else (labour if labour is not None else employees)
    total = employees + labour if both else 0

    def role(label: str, count, cost, dot: str) -> Dict[str, Any]:
        return {
            "label": label,
            "count": count,
            "cost": f"{money(cost)} a day" if cost is not None else "Cost unavailable",
            "dot": dot,
        }

    return {
        "on_shift": on_shift,
        "roles": [
            role("Employees", employees, employee_cost, "var(--d1)"),
            role("Labour", labour, labour_cost, "var(--d3)"),
        ],
        "split": (
            {"employees": width(employees / total * 100), "labour": width(labour / total * 100)}
            if both and total > 0
            else None
        ),
    }


# ------------------------------------------------------------ band 1: purchase


def _purchase_plan(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """The requirement in three states that add up to the headline: already
    drawn, held in the stores, still to buy. Scaled against whichever is
    larger, the requirement or its parts, so an unusual month stays honest."""
    planning = num(p.get("planning_value")) or 0.0
    issued = num(p.get("issued_value")) or 0.0
    on_hand = num(p.get("on_hand_value")) or 0.0
    to_purchase = max(0.0, planning - issued - on_hand)
    scale = max(planning, issued + on_hand + to_purchase)
    unpriced = num(p.get("unpriced_count")) or 0
    return tile(
        key,
        "Plan this month",
        tag=tag(f"{pct_label(to_purchase, planning)} still to buy"),
        # No subtitle, except the one that is not decoration: a component
        # with no price makes every rupee figure here read low.
        sub=f"{whole(unpriced)} components carry no price" if unpriced > 0 else "",
        value=money(planning),
        unit="total PM required",
        viz=meter(
            segment("mute", share(issued, scale), "Already drawn", money(issued)),
            segment("light", share(on_hand, scale), "PM on hand", money(on_hand)),
            segment("main", share(to_purchase, scale), "PM to purchase", money(to_purchase)),
        ),
    )


def _purchased(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    ordered = p.get("ordered_value")
    return tile(
        key,
        "Purchased",
        tag=tag(f"{whole(p.get('po_count'))} POs raised"),
        sub=f"{whole(p.get('po_lines'))} lines",
        value=money(ordered),
        unit="ordered",
        viz=meter(
            segment("main", share(p.get("po_received_value"), ordered), "Received", money(p.get("po_received_value"))),
            segment("mute", share(p.get("po_open_value"), ordered), "Still to come", money(p.get("po_open_value"))),
        ),
    )


def _benchmark(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """Only the shortfall, split into its two halves: a tile about what is
    missing should not spend its bar on what is fine."""
    below = num(p.get("below_benchmark_count")) or 0
    return tile(
        key,
        "Against benchmark",
        tag=(
            tag(f"{pct_label(below, p.get('sku_count'))} of the range", "bad")
            if below > 0
            else tag("all covered", "ok")
        ),
        value=whole(below),
        unit="SKUs below",
        viz=meter(
            segment("light", share(p.get("low_count"), below), "Low", whole(p.get("low_count"))),
            segment("main", share(p.get("critical_count"), below), "Critical", whole(p.get("critical_count"))),
        ),
    )


def _open_pos(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """Overdue first: on Oil every open packing line was already past due on
    9 September, so "how much is on order" alone is half an answer."""
    overdue = num(p.get("open_po_overdue_count")) or 0
    count = p.get("open_po_count")
    value = p.get("open_po_value")
    return tile(
        key,
        "Open POs",
        tag=tag(f"{whole(overdue)} overdue", "bad") if overdue > 0 else tag(f"{whole(count)} SKUs on order"),
        sub=f"{whole(count)} of the plan's items have something on order",
        value=money(value),
        viz=meter(
            segment("main", share(p.get("open_po_recent_value"), value), "Raised this month", money(p.get("open_po_recent_value"))),
            segment("mute", share(p.get("open_po_older_value"), value), "Older POs", money(p.get("open_po_older_value"))),
        ),
    )


# --------------------------------------------------------------- band 2: store


def _stock_space(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """The floor leads; how long ago somebody counted it is the pill. Until
    the pallet footprint is known both halves stay in their own real units
    and nothing is guessed."""
    area = p.get("area") or {}
    days = p.get("audit_days_ago")
    if days is None:
        pill = tag("never counted", "nil")
    else:
        pill = tag(f"counted {whole(days)}d ago", "bad" if days > 90 else ("warn" if days > 45 else "ok"))

    sqft = area.get("sqft")
    occupied = area.get("occupied_sqft")
    if occupied is None:
        sub = f"{whole(sqft)} sq ft · {qty(area.get('held_pieces'))} pcs · {money(area.get('held_value'))} held"
        fills = ["main", "light", "mute"]
        viz = meter(
            *[
                segment(fills[index] if index < len(fills) else "mute", share(block.get("sqft"), sqft), block.get("label"), f"{whole(block.get('sqft'))} sq ft")
                for index, block in enumerate(area.get("blocks") or [])
            ],
            segment("mute", 0, "In use", "set the floor a pallet takes"),
        )
    else:
        sub = f"{whole(sqft)} sq ft across {whole(area.get('store_count'))} stores · {dec(area.get('pallets'))} pallets standing"
        viz = meter(
            segment("main", share(occupied, sqft), "In use", f"{whole(occupied)} sq ft"),
            segment("mute", share(area.get("free_sqft"), sqft), "Free", f"{whole(area.get('free_sqft'))} sq ft"),
        )

    pct = area.get("occupied_pct")
    return tile(
        key,
        "Stock space",
        tag=pill,
        sub=sub,
        value=whole(sqft) if pct is None else dec(pct),
        unit="sq ft of floor" if pct is None else "% of the floor in use",
        viz=viz,
    )


def _non_moving(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    slow = num(p.get("slow_moving_count")) or 0
    dead = num(p.get("non_moving_count")) or 0
    split = slow + dead
    return tile(
        key,
        "Non-moving stock",
        tag=tag(f"{whole(p.get('item_count'))} SKUs idle"),
        sub=f"Oldest {whole(p.get('oldest_days'))} days · {whole(p.get('recent_count'))} SKUs still moving",
        value=money(p.get("total_value")),
        unit="idle in the stores",
        viz=meter(
            segment("light", share(slow, split), "Slow 30-45d", whole(slow)),
            segment("main", share(dead, split), "Non-moving 45d +", whole(dead)),
        ),
    )


def _pm_in(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """Received at the gate with no GRPO yet is not a quality question, and
    is drawn only when it is not zero."""
    received = p.get("received_qty")
    awaiting = num(p.get("awaiting_grpo_qty")) or 0
    return tile(
        key,
        "Packing material in",
        tag=tag(f"{whole(p.get('po_count'))} POs · {whole(p.get('line_count'))} lines"),
        sub=f"Total {qty(received)} pcs",
        value=whole(p.get("count")),
        unit="vehicles",
        viz=meter(
            segment("main", share(p.get("accepted_qty"), received), "Booked to SAP", qty(p.get("accepted_qty"))),
            segment("light", share(p.get("rejected_qty"), received), "Rejected", qty(p.get("rejected_qty"))),
            segment("mute", share(awaiting, received), "Awaiting GRPO", qty(awaiting)) if awaiting > 0 else None,
        ),
    )


def _blowing(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """Bottles lead, money follows; the pill is what is turning right now."""
    running = num(p.get("running_runs")) or 0
    return tile(
        key,
        "Blowing this month",
        tag=(
            tag("no line running", "nil")
            if running == 0
            else tag(f"{whole(running)} running · {money(p.get('running_cost'))} so far", "ok")
        ),
        sub=f"{money(p.get('cost'))} this month",
        value=qty(p.get("bottles_made")),
        unit="bottles",
        viz=bars(p.get("daily") or [], "bottles", qty),
    )


# ---------------------------------------------------------- band 3: production


def _today_lines(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """In CASES, alone on this band: the run register is typed in cases, and
    a wall figure the floor cannot find on its own screen is one it stops
    trusting."""
    planned = num(p.get("planned_cases"))
    produced = num(p.get("produced_cases"))
    runs = num(p.get("runs"))
    left = None if planned is None or produced is None else max(0.0, planned - produced)
    if runs is None:
        label = "—"
    elif runs == 0:
        label = "nothing planned"
    else:
        label = f"{whole(runs)} runs · {whole(p.get('lines'))} lines"
    return tile(
        key,
        "Today on the lines",
        tag=tag(label, "neut" if runs else "nil"),
        sub=f"Planned {qty(planned)} cases · drafts counted as planned",
        value=qty(produced),
        unit="cases",
        viz=meter(
            segment("main", share(produced, planned)),
            segment("mute", share(left, planned), "Left to make", qty(left)),
        ),
    )


def _monthly_planning(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """On the TONNAGE, because that is what the tile reads in. What the plan
    cannot speak for is disclosed as zero-width legend entries, not slices."""
    attainment = num(p.get("attainment_tons_pct"))
    if attainment is None:
        tone = "nil"
    elif attainment >= 90:
        tone = "ok"
    elif attainment >= 70:
        tone = "warn"
    else:
        tone = "bad"
    planned = num(p.get("planned_tons")) or 0.0
    produced = num(p.get("produced_tons")) or 0.0
    left = max(0.0, planned - produced)
    unplanned = num(p.get("produced_unplanned_tons")) or 0
    unweighed = num(p.get("unweighed_lines")) or 0
    return tile(
        key,
        "Monthly planning",
        tag=tag("no plan" if attainment is None else f"{js_number(attainment)}% produced", tone),
        sub=f"{whole(p.get('produced_tons'))} t produced · {qty(p.get('produced_qty'))} pcs",
        value=whole(p.get("planned_tons")),
        unit="t planned",
        viz=meter(
            segment("main", share(produced, planned)),
            segment("mute", share(left, planned), "Left to make", f"{whole(left)} t"),
            segment("mute", 0, "Unplanned", f"{whole(unplanned)} t") if unplanned > 0 else None,
            segment("mute", 0, "No litre volume", f"{whole(unweighed)} SKUs") if unweighed > 0 else None,
        ),
    )


def _floor_stock(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """Stock that has never left folds into the oldest bucket: it is the
    worse case, and a fourth segment has no room to be read."""
    age = p.get("age") or {}

    def tons(bucket: str) -> float:
        return num((age.get(bucket) or {}).get("tons")) or 0.0

    fresh = tons("fresh")
    mid = tons("d4_7")
    oldest = tons("d7_plus") + tons("never_shipped")
    aged = fresh + mid + oldest
    never_items = (age.get("never_shipped") or {}).get("items")
    unweighed = num(p.get("unweighed_items")) or 0
    if unweighed > 0:
        sub = f"{whole(p.get('item_count'))} SKUs · {whole(unweighed)} carry no litre volume, so are absent from the tonnage"
    else:
        sub = f"{whole(p.get('item_count'))} SKUs · {whole(never_items)} have never shipped from here"
    return tile(
        key,
        "Total stock",
        tag=tag(money(p.get("stock_value"))),
        sub=sub,
        value=dec(p.get("tons")),
        unit="tonnes at BH-PF",
        viz=meter(
            segment("mute", share(fresh, aged), "Up to 3 days", f"{dec(fresh)} t"),
            segment("light", share(mid, aged), "4–7 days", f"{dec(mid)} t"),
            segment("main", share(oldest, aged), "Over 7 days", f"{dec(oldest)} t"),
        ),
    )


def _wastage(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """The newest day anything was logged against, named and aged; the bars
    are the week in money, because pieces, kilos and metres do not add."""
    day = p.get("logged_latest") or {}
    behind = num(p.get("logged_days_behind"))
    if behind is None:
        label, tone = "nothing logged", "nil"
    elif behind == 0:
        label, tone = f"logged today · {whole(day.get('logs'))} rows", "neut"
    else:
        label = f"{day_label(p.get('logged_latest_date'))} · {whole(behind)}d behind"
        tone = "warn" if behind > 2 else "neut"
    others = day.get("pm_other") or []
    sub = f"Raw material {money(day.get('rm_value') or 0)} · packing {qty(day.get('pm_pieces'))} pcs"
    if others:
        sub += ", " + ", ".join(f"{whole(row.get('qty'))} {row.get('uom')}" for row in others)
    return tile(
        key,
        "Wastage",
        tag=tag(label, tone),
        sub=sub,
        value=money(day.get("pm_value")),
        unit="packing material",
        viz=bars(p.get("logged_daily") or [], "total_value", money),
    )


# ------------------------------------------------------------ band 4: shifting


def _stage_value(stage: Dict[str, Any]) -> str:
    """Tonnes when SAP answered, pieces when it did not: an outage costs this
    band its unit, never its figures."""
    return dec(stage.get("total_tons")) if stage.get("tonnage_available") else qty(stage.get("total_pieces"))


def _stage_unit(stage: Dict[str, Any]) -> str:
    return "tonnes" if stage.get("tonnage_available") else "pcs, SAP not answering"


def _route_label(row: Dict[str, Any]) -> str:
    base = ROUTE_LABEL.get(row.get("route")) or row.get("name") or row.get("route") or ""
    codes = row.get("codes") or []
    return f"{base} · {', '.join(codes)}" if codes else base


def _route_meter(stage: Dict[str, Any]) -> Dict[str, Any]:
    """The routes off the floor, always in the same order and tint."""
    weighed = bool(stage.get("tonnage_available"))
    parts = []
    for row in stage.get("routes") or []:
        parts.append(
            segment(
                ROUTE_FILL.get(row.get("route"), "mute"),
                share(row.get("tons"), stage.get("total_tons")) if weighed else share(row.get("pieces"), stage.get("total_pieces")),
                _route_label(row),
                f"{dec(row.get('tons'))} t" if weighed else f"{qty(row.get('pieces'))} pcs",
            )
        )
    unweighed = num(stage.get("unweighed_items")) or 0
    if unweighed > 0:
        parts.append(segment("mute", 0, "No litre volume", f"{whole(unweighed)} SKUs"))
    return meter(*parts)


def _declared(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """A withdrawn declaration leads the pill: only this register keeps it."""
    retracted = num(p.get("retracted_movements")) or 0
    transfers = num(p.get("transfers")) or 0
    if retracted > 0:
        pill = tag(f"{whole(retracted)} retracted", "warn")
    elif transfers == 0:
        pill = tag("nothing declared", "nil")
    else:
        pill = tag(f"{whole(transfers)} {plural(transfers, 'declaration')}")
    return tile(
        key,
        "Declared today",
        tag=pill,
        sub=f"{qty(p.get('total_pieces'))} pcs in {whole(p.get('boxes'))} boxes · the keeper's own note",
        value=_stage_value(p),
        unit=_stage_unit(p),
        viz=_route_meter(p),
    )


def _shipped(key: str, p: Dict[str, Any]) -> Dict[str, Any]:
    rejected = num(p.get("rejected_pieces")) or 0
    if rejected > 0:
        label = (
            f"{dec(p.get('rejected_tons'))} t rejected"
            if p.get("tonnage_available")
            else f"{qty(rejected)} pcs rejected"
        )
        pill = tag(label, "bad")
    else:
        pill = tag(f"{whole(p.get('transfers'))} transfers out")
    return tile(
        key,
        "Shipped today",
        tag=pill,
        sub=f"{qty(p.get('total_pieces'))} pcs in {whole(p.get('boxes'))} boxes · scanned onto a BST",
        value=_stage_value(p),
        unit=_stage_unit(p),
        viz=_route_meter(p),
    )
