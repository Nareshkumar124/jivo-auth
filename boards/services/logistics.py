"""
The Logistics Control board: the factory's "Operations board", composed here.

The factory has no composed endpoint for this board. Its React page fans out
to a dozen feeds and composes the three bands in the browser
(``useLogisticsControlBoard``); this service ports BOTH halves -- each feed's
backend read, straight off the factory's PostgreSQL and SAP HANA, and the
hook's composition -- into one server-side build.

Three bands in the order the plant works: what is standing in BH-BT, what
left it today and this month, and the fleet and paperwork trailing behind.
Each band closes with who is on shift, because "612 tonnes on 34 vehicles"
and "118 labour on the floor" are read together.

The board's one hard rule, kept from the factory: a figure with no source
behind it does not look like a figure. A feed that cannot be read is ``None``
in the payload (``meta.degraded`` names it, ``meta.reasons`` says why) and
its tiles render as "not read", never as zero.

Scope. Everything that makes one plant's board different from another's is
in ``LogisticsScope``: the warehouse, the companies ADDED together, the
labour departments, and the tiles with no source on that side of the site.
``JIVO_OIL`` (and Mart, whose dispatch is one half of the Oil board) gets
the BH-BT board; ``JIVO_BEVERAGES`` gets its own.

Not ported in this pass (the tile says so where it matters):

* The WMS pallet-slot fallback behind "Stock on hand" (four JSON-document
  collections summarised client-side). The tile's first choice, tonnage
  against the rated capacity, is ported; without a capacity the meter is
  left empty and a note says why.
* The drill-down panels and the settings page.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import formatting
from ..sections import SectionBuilder
from ..sources import SAPConnectionError, SAPDataError
from ..sources.hana import LITRES_PER_UNIT
from ..templatetags.boards import RULE, day_label, indian, money, num

# ---------------------------------------------------------------------------
# Constants (logistics-control.constants.ts, with the factory's reasoning)
# ---------------------------------------------------------------------------

#: How often the wall re-reads, from LOGISTICS_CONTROL_REFRESH_MS. The
#: freight feeds polled five-minutely in the browser are read on this
#: cadence too: the composed board is cached by the shell, and one HANA
#: aggregate a minute is cheaper than the browser's per-row fan-out was.
REFRESH_SECONDS = 60

#: Where non-moving starts, in days since the item last moved. 40 matches
#: nothing else in the factory (the Warehouse Control board opens at 45);
#: this board owns its own bands rather than moving three shared constants.
NON_MOVING_FROM_DAYS = 40
#: Where the second, open-ended band starts.
NON_MOVING_AGEING_DAYS = 90
#: SAP item group the stock and non-moving reads are scoped to: 102,
#: finished goods. Never 105 (packaging): BH-BT holds packaging lines worth
#: an apparent 229 T on one shrink-film SKU's bad case weight.
FINISHED_GOODS_ITEM_GROUP = 102
STOCK_ITEM_GROUPS: Tuple[int, ...] = (FINISHED_GOODS_ITEM_GROUP,)

#: The month's dispatch target, in tonnes. 0 hides the target entirely; the
#: tile says the target is unset rather than reporting a -100% miss.
MONTHLY_TARGET_TONNES = 0

#: The freight funnel's age bands: floors of EXCLUSIVE windows ("0-14",
#: "15-29", "30-44", "45+"). All three feeds bucket on these same edges.
FUNNEL_AGE_BANDS: Tuple[int, ...] = (0, 15, 30, 45)
#: The edges the SAP readers and the GRPO summary bucket on.
AGE_BANDS: Tuple[int, ...] = (15, 30, 45)
FUNNEL_STAGES: Tuple[str, ...] = ("GRPO", "AP", "PAYMENT")
STAGE_LABELS = {"GRPO": "GRPO", "AP": "AP", "PAYMENT": "Payment"}

#: Transit age band edges: "up to 3 days", "4 - 7 days", "over 7 days".
TRANSIT_FRESH_DAYS = 3
TRANSIT_AGEING_DAYS = 7
#: How far back the transit read looks for an unmatched invoice. A receipt
#: nobody keyed leaves its invoice unmatched forever.
TRANSIT_LOOKBACK_DAYS = 60
#: Floors that actually ship stock. Without it the read picks up
#: rate-difference debit notes against other plants' warehouses.
TRANSIT_SHIPPING_WAREHOUSES: Tuple[str, ...] = ("BH-PF", "BH-BT")
#: The intercompany legs: (sender, customer codes, receiver).
TRANSIT_ROUTES: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("JIVO_OIL", ("CUSTA000606",), "JIVO_MART"),
)

#: Loading cost per litre, in rupees: the figure written on the board's
#: design. Nothing in either repo holds a loading rate.
LOADING_COST_PER_LITRE = 0.8

#: How far back the freight-payments figure looks, in days.
FREIGHT_PAYMENT_DAYS = 30

#: Ceiling on rows pulled into a roll-up. The pending-bill feed slices
#: AFTER ordering by dispatch date descending, so overflow drops the oldest
#: and most overdue bills first; the tile says so rather than totalling a
#: truncated set.
MAX_ROLLUP_ROWS = 2000
#: The pending-dispatch window: all of it. A bill stuck since April is the
#: single most important row that question has.
PENDING_WINDOW = (date(2000, 1, 1), date(2099, 12, 31))
#: The booking states the pending tile asks for, one read each. The slices
#: are what keep an all-time window under the row cap.
PENDING_STATUSES: Tuple[str, ...] = ("PENDING", "BOOKED")

#: The vendor group a haulier belongs to, matched on the group's NAME.
TRANSPORTER_GROUP_NAME = "TRANSPORTER"
#: The expense account outbound freight is booked to. Every service GRPO
#: for a transporter in both companies posts here.
OUTWARD_FREIGHT_ACCOUNT = "5670001"
MAX_VENDOR_ROWS = 25

#: How many SAP doc entries one bills lookup binds into a single IN (...).
BILL_LOOKUP_CHUNK = 500

#: Salary is entered monthly and served per day; the factory's serializer
#: divides by a flat 30.
SALARY_DAYS_IN_MONTH = 30

#: The Cost Master type the labour tile prices head count with.
LABOUR_COST_TYPE_CODE = "factory-labour"
SCOPE_SPECIFICITY = {"FACTORY": 0, "COMPANY": 1, "DEPARTMENT": 2, "VALUE": 3}

#: Statuses that count as "still employed" for a head count.
IN_SERVICE_STATUSES: Tuple[str, ...] = ("ACTIVE", "PROBATION", "ON_LEAVE", "SUSPENDED")

#: BST statuses where the truck itself is still committed to the load.
ENGAGED_BST_STATUSES: Tuple[str, ...] = ("IN_TRANSIT", "SCANNING")

#: Packing-material rules the non-moving read carries (packing_material.constants).
PM_ITEM_GROUP = 105
PM_ITEM_GROUP_NAME = "PACKAGING MATERIAL"
TRANS_TYPE_GOODS_ISSUE = 60
TRANS_TYPE_PRODUCTION_RECEIPT = 59
TRANS_TYPE_TRANSFER_IN = 67
TRANS_TYPE_GRPO = 20
CONSUMPTION_WINDOW_DAYS = 365

#: Inventory units that are a mass or a volume rather than a count of
#: things. Mirrors ``_MASS_OR_VOLUME_UOMS`` in the factory's stock service:
#: a case weight only converts an on-hand figure that is a piece count.
MASS_OR_VOLUME_UOMS = frozenset(
    {"KG", "KGS", "KGM", "GM", "GMS", "GRM", "MT", "TON", "TONNE",
     "L", "LT", "LTR", "LTRS", "LITRE", "LITRES", "ML", "CC", "M3"}
)

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

#: Row tint rises with the age band, fresh at the top.
AGE_CLASS = ("ops-a-new", "ops-a-ok", "ops-a-warn", "ops-a-bad")


# ---------------------------------------------------------------------------
# Scope (logistics-control.scopes.ts)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LogisticsScope:
    """Everything that makes one operations board a different board from another.

    ``warehouse_company`` names whose SAP chart of warehouses holds the
    floor; ``settings_company`` whose typed-in rows the board reads;
    ``dispatch_companies`` is what is ADDED. Mixing them up is how a board
    reports another company's numbers under this company's heading.
    """

    key: str
    title: str
    warehouse: str
    warehouse_company: str
    settings_company: str
    dispatch_companies: Tuple[str, ...]
    #: Gate labour department names counting against each card.
    section_departments: Dict[str, Tuple[str, ...]]
    #: Employee-directory department CODES (and names) counting against
    #: each card. The fallback, not the master: the typed figure wins.
    section_employee_departments: Dict[str, Tuple[str, ...]]
    #: Tiles with no source under this scope, each with the sentence it prints.
    absent: Dict[str, str]
    #: Tiles this scope does not show at all. Their feeds are not read.
    hidden: Tuple[str, ...]


SECTION_EMPLOYEE_DEPARTMENTS = {
    # The FG basement, BH-BT. The generated code was truncated at 30 chars.
    "warehouse": ("WAREHOUSE_FG_WAREHOUSE_BASEMEN", "FG_WAREHOUSE_BASEMENT"),
    # The directory calls dispatch *Dock*; its two parents were never staffed.
    "dispatch": ("DOCK", "DISPATCHED_FRONTEND", "DISPATCHED_BACKEND"),
    "transport": ("TRANSPORT_TRANSPORT", "LOGISTICS", "TRANSPORT"),
}

OIL_SCOPE = LogisticsScope(
    key="oil",
    title="Operations board",
    warehouse="BH-BT",
    warehouse_company="JIVO_OIL",
    settings_company="JIVO_OIL",
    dispatch_companies=("JIVO_OIL", "JIVO_MART"),
    # BH-BT is the basement floor, so its labour is the gate's "Warehouse
    # Basement" department. "Warehouse Beverage" and "Warehouse Gupta" are
    # different floors and must not be folded in. Dispatch and transport
    # have no gate department, so their strips say they are unassigned.
    section_departments={"warehouse": ("Warehouse Basement",), "dispatch": (), "transport": ()},
    section_employee_departments=SECTION_EMPLOYEE_DEPARTMENTS,
    absent={},
    hidden=(),
)

BEVERAGES_SCOPE = LogisticsScope(
    key="beverages",
    title="Beverages operations",
    warehouse="BH-FG",
    warehouse_company="JIVO_BEVERAGES",
    settings_company="JIVO_BEVERAGES",
    dispatch_companies=("JIVO_BEVERAGES",),
    section_departments={"warehouse": ("Warehouse Beverage",), "dispatch": (), "transport": ()},
    section_employee_departments=SECTION_EMPLOYEE_DEPARTMENTS,
    absent={
        "pallet_space": (
            "No WMS floor is mapped for Beverages, so pallet slots cannot "
            "stand in for a rated capacity."
        ),
        "stock_in_transit": (
            "No intercompany route is configured for Beverages — this read "
            "covers Oil to Mart only."
        ),
    },
    # Two tiles off this wall because the question behind them is an Oil
    # question: the godown movement register is the oil floor's, and
    # Beverages scans no boxes.
    hidden=("allocated", "unscanned"),
)


def scope_for_company(company_code: str) -> LogisticsScope:
    """Beverages gets its own plant; everyone else (Oil, Mart, unknown) gets
    the Oil board, which Mart's own shipments are inside."""
    if (company_code or "").upper() == "JIVO_BEVERAGES":
        return BEVERAGES_SCOPE
    return OIL_SCOPE


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _f(value: Any) -> float:
    return formatting.f(value)


def _f_or_none(value: Any) -> Optional[float]:
    return formatting.f_or_none(value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _col(row: Dict[str, Any], name: str) -> Any:
    """A SAP column by the alias the SQL wrote. HANA folds an unquoted alias
    to upper case (``AS docs`` comes back as ``DOCS``); a quoted one keeps
    its spelling."""
    return row[name] if name in row else row.get(name.upper())


def _half_up(number: float, places: int) -> float:
    """``number`` to ``places``, a tie rounded away from zero, as the page
    rounds: ``toLocaleString`` and ``Math.round`` both send 32.5 to 33,
    where Python's ``round`` sends it to the even 32. Done on the float's
    exact value, as the browser does, so 2.675 (really 2.67499...) stays 2.67."""
    step = Decimal(1).scaleb(-places)
    return float(Decimal(number).quantize(step, rounding=ROUND_HALF_UP))


def dec(value: Any, digits: Any = 1) -> str:
    """The page's ``decimal()``: Indian grouping, a fixed number of places.

    Rounded BEFORE it is split into whole and fraction. The shared ``dec``
    filter truncates the whole part and rounds the fraction on its own, so a
    carry is lost: 612.97 tonnes printed as "612.0" and 2.96 as "2.0". On
    this board nearly every tonnage goes through here, so it rounds first.
    """
    number = num(value)
    if number is None:
        return RULE
    try:
        places = max(int(digits), 0)
    except (TypeError, ValueError):
        places = 1
    return indian(_half_up(number, places), places)


def whole(value: Any) -> str:
    """The page's ``whole()``: a whole number, Indian grouping, a tie up."""
    number = num(value)
    return RULE if number is None else indian(_half_up(number, 0))


def page_money(value: Any) -> str:
    """The page's own ``money()``: lakhs and crores to two places.

    Not the shared ``money`` filter, which gives lakhs one place (that is
    the people strip's formatter, and it keeps it). The factory page passes
    this one to the freight matrix and the workforce total, so "₹2.84 L"
    there must not read "₹2.8 L" here.
    """
    number = num(value)
    if number is None:
        return RULE
    magnitude = abs(number)
    if magnitude >= 1_00_00_000:
        return f"₹{number / 1_00_00_000:.2f} Cr"
    if magnitude >= 1_00_000:
        return f"₹{number / 1_00_000:.2f} L"
    if magnitude >= 1_000:
        return f"₹{number / 1_000:.1f}k"
    return f"₹{round(number)}"


def company_label(code: str) -> str:
    """``JIVO_OIL`` reads as "OIL" on a board where every company shares the prefix."""
    text = code or ""
    upper = text.upper()
    if upper.startswith("JIVO"):
        text = text[4:].lstrip("_ -")
    return text.replace("_", " ") or code


def is_piece_uom(uom: Optional[str]) -> bool:
    """Whether an on-hand quantity in ``uom`` is a count of pieces. Unknown
    and blank answer False, so unweighable rows land in the disclosure."""
    trimmed = (uom or "").strip()
    return bool(trimmed) and trimmed.upper() not in MASS_OR_VOLUME_UOMS


def kilograms_per_piece(row: Dict[str, Any]) -> Optional[float]:
    """Kilograms in one piece, or None where the row cannot be weighed.

    ``gross_weight_per_case`` is the GROSS weight of one sales case, while
    on-hand is a piece count, so weight per piece is the case weight over
    pieces per box. A factor of exactly 1 means the SKU is sold by the piece.
    """
    if not is_piece_uom(row.get("uom")):
        return None
    per_case = _f_or_none(row.get("gross_weight_per_case"))
    if per_case is None or per_case <= 0:
        return None
    pieces = _f_or_none(row.get("pieces_per_box"))
    if pieces is None or pieces <= 0:
        return None
    return per_case / pieces


def roll_up_tonnage(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Total a warehouse in tonnes, carrying its own incompleteness with it.

    Negative on-hand rows are included as-is: hiding them would make the
    board disagree with the stock screen for a reason nobody could see.
    """
    kg = 0.0
    weighed = unweighed = non_piece = 0
    for row in rows:
        if not is_piece_uom(row.get("uom")):
            non_piece += 1
            continue
        per_piece = kilograms_per_piece(row)
        if per_piece is None:
            unweighed += 1
            continue
        kg += _f(row.get("on_hand")) * per_piece
        weighed += 1
    return {
        "tonnes": kg / 1000,
        "weighed_items": weighed,
        "unweighed_items": unweighed,
        "non_piece_items": non_piece,
        "coverage": (weighed / len(rows)) if rows else None,
    }


def weigh_items(items: Sequence[Dict[str, Any]], stock_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Weigh a set of (item_code, quantity) against the warehouse's own
    weight table. Items the occupancy feed cannot weigh are counted, not
    dropped, so the caller can say how much of the answer is missing."""
    per_piece: Dict[str, float] = {}
    for row in stock_rows:
        kg = kilograms_per_piece(row)
        if kg is not None:
            per_piece[(row.get("item_code") or "").strip()] = kg
    kg_total = 0.0
    weighed = unweighed = 0
    for item in items:
        rate = per_piece.get((item.get("item_code") or "").strip())
        if rate is None:
            unweighed += 1
            continue
        kg_total += _f(item.get("quantity")) * rate
        weighed += 1
    return {"tonnes": kg_total / 1000, "weighed": weighed, "unweighed": unweighed}


def day_on_day(today_tonnes: float, yesterday_tonnes: float) -> Optional[Dict[str, Any]]:
    """Today against the same point yesterday. None when yesterday moved
    nothing (a percentage against zero is infinite); flat below half a
    percent, so the arrow does not redraw over rounding noise."""
    if not yesterday_tonnes > 0:
        return None
    change = (today_tonnes - yesterday_tonnes) / yesterday_tonnes * 100
    if abs(change) < 0.5:
        return {"pct": 0.0, "direction": "flat"}
    return {"pct": abs(change), "direction": "up" if change > 0 else "down"}


def day_plan(bills: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """What the day is committed to move: booked bills only, read off
    ``vehicle_id`` rather than the client-writable status. Bills already
    dispatched stay in, or the bar would fill while the target fell."""
    booked = [bill for bill in bills if bill.get("vehicle_id") is not None]
    return {
        "tonnes": sum(_f(bill.get("weight_kg")) for bill in booked) / 1000,
        "bills": len(booked),
        "unbooked_bills": len(bills) - len(booked),
    }


def cost_per_litre(freight_amount: float, covered_litres: float, window_litres: float, loading: float):
    """The bilty half measured, the loading half configured. None when no
    litres are covered: "₹0.80 per litre" would read as freight being free."""
    if not covered_litres > 0:
        return None
    freight = freight_amount / covered_litres
    return {
        "freight": freight,
        "loading": loading,
        "total": freight + loading,
        "covered_litres": covered_litres,
        "window_litres": window_litres,
        "coverage_pct": min(100.0, covered_litres / window_litres * 100) if window_litres > 0 else 0.0,
    }


def funnel_column(stage: str, buckets, bands: Sequence[int], unavailable: Optional[str] = None) -> Dict[str, Any]:
    """A funnel column from buckets somebody else already counted. The bands
    are EXCLUSIVE windows, so a cell is simply its own bucket; a bucket
    outside every band is still in the column total."""
    if unavailable or buckets is None:
        return {
            "stage": stage, "total": 0, "amount": 0.0, "unpriced": 0, "undated": 0,
            "cells": [{"band": band, "count": 0, "amount": 0.0, "unpriced": 0} for band in bands],
            "unavailable": unavailable or "No feed",
        }
    cells = []
    for index, band in enumerate(bands):
        ceiling = bands[index + 1] if index + 1 < len(bands) else None
        in_band = [b for b in buckets if b["band"] >= band and (ceiling is None or b["band"] < ceiling)]
        cells.append({
            "band": band,
            "count": sum(_int(b.get("documents")) for b in in_band),
            "amount": sum(_f(b.get("amount")) for b in in_band),
            "unpriced": sum(_int(b.get("unpriced")) for b in in_band),
        })
    return {
        "stage": stage,
        "total": sum(_int(b.get("documents")) for b in buckets),
        "amount": sum(_f(b.get("amount")) for b in buckets),
        "unpriced": sum(_int(b.get("unpriced")) for b in buckets),
        "undated": 0,
        "cells": cells,
        "unavailable": None,
    }


def band_label(bands: Sequence[int], index: int) -> str:
    band = bands[index]
    if index + 1 >= len(bands):
        return f"{band}+"
    return f"{band}–{bands[index + 1] - 1}"


def _max_or_none(left: Optional[int], right: Optional[int]) -> Optional[int]:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _clamp(value: Any, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, _f(value)))


# ---------------------------------------------------------------------------
# Visualisations: plain data with the geometry precomputed
# ---------------------------------------------------------------------------

def meter(segments: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """A composition bar. A sliver still has to be seen: a segment worth 0.9%
    gets the bar's own height as a floor, so it reads as a round dot."""
    drawn = []
    for index, segment in enumerate(segments):
        pct = _f(segment.get("pct"))
        drawn.append({
            "fill": segment.get("fill", "main"),
            "width": round(_clamp(pct), 2),
            "sliver": pct > 0,
            "delay": f"{index * 0.1:.1f}s" if index > 0 else None,
            "label": segment.get("label"),
            "figure": segment.get("figure"),
        })
    return {
        "kind": "meter",
        "segments": drawn,
        "legend": [s for s in drawn if s["label"]],
    }


def bars(days: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """A short column chart, heights a share of the tallest day. The 2% floor
    keeps a small-but-real day visible and must NOT apply to a day of nothing."""
    drawn = []
    for index, day in enumerate(days):
        pct = _f(day.get("pct"))
        drawn.append({
            "label": "Today" if index == len(days) - 1 else day.get("label"),
            "height": round(max(2.0, min(100.0, pct)), 2) if pct > 0 else 0.0,
            "value": day.get("value"),
            "now": index == len(days) - 1,
            "delay": f"{0.02 + index * 0.06:.2f}s",
        })
    return {"kind": "bars", "days": drawn}


def pair(rows: Sequence[Dict[str, Any]], note: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Two figures as stacked bars on one scale, so the visible gap is the
    difference between them and nothing else."""
    return {
        "kind": "pair",
        "rows": [
            {"label": r["label"], "figure": r["figure"], "width": round(_clamp(r.get("pct")), 2), "fill": r.get("fill", "main")}
            for r in rows
        ],
        "note": note,
    }


def duo(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Two figures side by side, divided: for a tile whose answer is two
    numbers rather than one split into parts."""
    return {"kind": "duo", "items": [{"k": i["k"], "v": i["v"]} for i in items]}


def matrix(columns: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Open freight documents by age: bands down, stages across. Each cell
    carries its money under its count, except for a counts-only stage whose
    documents are not money yet."""
    bands = [cell["band"] for cell in columns[0]["cells"]] if columns else []
    rows = []
    for index, band in enumerate(bands):
        cells = []
        for column in columns:
            cell = next((c for c in column["cells"] if c["band"] == band), None)
            count = _int(cell.get("count")) if cell else 0
            amount = _f(cell.get("amount")) if cell else 0.0
            unpriced = _int(cell.get("unpriced")) if cell else 0
            show_cash = (
                not column.get("unavailable")
                and not column.get("counts_only")
                and count > 0
                and (amount > 0 or unpriced > 0)
            )
            cells.append({
                "unavailable": bool(column.get("unavailable")),
                "count": count,
                "cash": page_money(amount) if show_cash and amount > 0 else None,
                "unpriced": unpriced if show_cash and unpriced > 0 else 0,
                "plus": show_cash and amount > 0 and unpriced > 0,
            })
        rows.append({
            "label": band_label(bands, index),
            "age_class": AGE_CLASS[min(index, len(AGE_CLASS) - 1)],
            "cells": cells,
        })
    return {
        "kind": "matrix",
        "stages": [{"stage": c["stage"], "label": STAGE_LABELS.get(c["stage"], c["stage"])} for c in columns],
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

class LogisticsBoardService(SectionBuilder):
    """The Logistics Control board, one feed per section.

    ``company_code`` picks the scope (the plant); ``self.hana`` is the
    warehouse company's SAP reader and ``hana_for`` hands out the others.
    """

    REFRESH_SECONDS = REFRESH_SECONDS

    def __init__(self, company_code: str, *, postgres, hana_for, today: Optional[date] = None, **extra):
        self.company_code = company_code
        self.scope = scope_for_company(company_code)
        self.postgres = postgres
        self.hana_for = hana_for
        self.hana = hana_for(self.scope.warehouse_company)
        self.today = today or formatting.today()
        self.month_start = formatting.month_start(self.today)
        self.yesterday = self.today - timedelta(days=1)
        self._company_ids: Optional[Dict[str, int]] = None
        self._init_sections()

    # ------------------------------------------------------------------ build
    def build(self) -> Dict[str, Any]:
        scope = self.scope
        payload: Dict[str, Any] = {
            "scope": {
                "key": scope.key,
                "title": scope.title,
                "warehouse": scope.warehouse,
                "warehouse_company": scope.warehouse_company,
                "settings_company": scope.settings_company,
                "dispatch_companies": list(scope.dispatch_companies),
                "hidden": list(scope.hidden),
            },
            "today": self.today.isoformat(),
            "month_start": self.month_start.isoformat(),
            "yesterday": self.yesterday.isoformat(),
        }

        # SAP feeds first: one outage costs one timeout, and the warehouse
        # company's own read is the one that says whether SAP is up.
        payload["stock"] = self.section("stock", self._read_stock, needs_factory=False)
        payload["non_moving"] = self.section(
            "non_moving", lambda: self._read_non_moving(payload["stock"]), needs_factory=False
        )
        payload["warehouse_settings"] = self.section(
            "warehouse_settings", self._read_warehouse_settings, needs_sap=False
        )
        payload["board_settings"] = self.section("board_settings", self._read_board_settings, needs_sap=False)
        payload["pending_dispatch"] = self.section("pending_dispatch", self._read_pending_dispatch)
        payload["day_plan"] = self.section("day_plan", self._read_day_plan)
        if "allocated" not in scope.hidden:
            payload["allocated"] = self.section("allocated", self._read_allocated, needs_sap=False)
        payload["dispatch"] = self.section("dispatch", self._read_dispatch_summary, needs_sap=False)
        if "unscanned" not in scope.hidden:
            payload["unscanned"] = self.section("unscanned", self._read_partial_scans, needs_sap=False)
        payload["fleet"] = self.section(
            "fleet", lambda: self._read_fleet(payload["board_settings"]), needs_sap=False
        )
        payload["transit"] = self.section("transit", self._read_transit, needs_factory=False)
        payload["transporter_account"] = self.section(
            "transporter_account", self._read_transporter_account, needs_factory=False
        )
        payload["grpo_queue"] = self.section("grpo_queue", self._read_grpo_queue, needs_sap=False)
        payload["freight_rate"] = self.section("freight_rate", self._read_freight_rate, needs_factory=False)
        payload["expense"] = self.section("expense", self._read_expense, needs_sap=False)
        payload["employee_roll"] = self.section("employee_roll", self._read_employee_roll, needs_sap=False)

        # The hook's derivations, over whatever was read.
        payload["workforce"] = self._compose_workforce(payload)
        payload["topbar"] = self._compose_topbar(payload)
        payload["bands"] = [
            self._warehouse_band(payload),
            self._dispatch_band(payload),
            self._transport_band(payload),
        ]
        payload["meta"] = {
            **self.section_meta(),
            "refresh_seconds": self.REFRESH_SECONDS,
            "clock_date": self._clock_date(),
        }
        return payload

    # ------------------------------------------------------------ plumbing
    def _company_id_map(self) -> Dict[str, int]:
        """``code -> id`` for every company this board names, from the
        factory's own company table. A code with no row is warned once."""
        if self._company_ids is None:
            wanted = sorted({*self.scope.dispatch_companies, self.scope.settings_company, self.scope.warehouse_company})
            rows = self.postgres.rows(
                "SELECT id, code FROM company_company WHERE code = ANY(%s) AND is_active",
                [wanted],
            )
            found = {row["code"]: int(row["id"]) for row in rows}
            for code in wanted:
                if code not in found:
                    self.warn(f"{code} has no company row in the factory database.")
            self._company_ids = found
        return self._company_ids

    def _dispatch_company_ids(self) -> List[int]:
        ids = self._company_id_map()
        return [ids[code] for code in self.scope.dispatch_companies if code in ids]

    def _each_dispatch_company(self, read: Callable[[str, Any], Any]) -> Tuple[Dict[str, Any], List[str]]:
        """Run one SAP read per dispatch company, the way the browser fanned out.

        The warehouse company's own failure is THE outage and propagates (the
        section degrades and SAP latches off); a secondary company that
        cannot be read is named in ``unread`` and warned, because its half
        missing from an "Oil | Mart" figure must be said rather than summed
        away. A partial sum reads as a small day.
        """
        results: Dict[str, Any] = {}
        unread: List[str] = []
        for code in self.scope.dispatch_companies:
            try:
                results[code] = read(code, self.hana_for(code))
            except (SAPConnectionError, SAPDataError) as exc:
                if code == self.scope.warehouse_company:
                    raise
                unread.append(code)
                self.warn(f"{company_label(code)}'s SAP could not be read: {exc}")
        return results, unread

    def _clock_date(self) -> str:
        d = self.today
        return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]} {d.year}"

    def _gross_weight_expr(self, alias: str = "i") -> str:
        """Gross weight of one sales case, or NULL where SAP has none. NULL
        rather than 0 so "no weight recorded" is told apart from "weighs
        nothing" and disclosed. ``U_Gross_Weight`` is a user field: a schema
        without it fails this read, which degrades the tile with the reason."""
        return f'NULLIF(COALESCE({alias}."U_Gross_Weight", 0), 0)'

    # ---------------------------------------------------------- warehouse
    def _read_stock(self) -> Dict[str, Any]:
        """One warehouse's finished-goods stock with the SAP pack fields that
        turn pieces into tonnes (stock_dashboard.hana_reader.get_warehouse_occupancy)."""
        hana = self.hana
        codes = ", ".join(str(int(code)) for code in STOCK_ITEM_GROUPS)
        sql = f"""
            SELECT
                w."ItemCode",
                i."ItemName",
                COALESCE(w."OnHand", 0)       AS "OnHand",
                COALESCE(i."SalFactor2", 0)   AS "PiecesPerBox",
                COALESCE(i."SalPackUn", 0)    AS "LitresPerPiece",
                COALESCE(w."StockValue", 0)   AS "StockValue",
                COALESCE(i."InvntryUom", '')  AS "Uom",
                {self._gross_weight_expr("i")}           AS "GrossWeightPerCase"
            FROM {hana.table("OITW")} w
            JOIN {hana.table("OITM")} i ON i."ItemCode" = w."ItemCode"
            WHERE w."WhsCode" = ?
              AND COALESCE(w."OnHand", 0) <> 0
              AND i."ItmsGrpCod" IN ({codes})
            ORDER BY COALESCE(w."OnHand", 0) DESC
        """
        rows = []
        for raw in hana.rows(sql, [self.scope.warehouse]):
            gross = raw.get("GrossWeightPerCase")
            rows.append({
                "item_code": raw.get("ItemCode") or "",
                "item_name": raw.get("ItemName") or "",
                "on_hand": _f(raw.get("OnHand")),
                # None rather than 0 where SAP holds nothing, so a caller decides
                # what an unconfigured item means instead of dividing by zero.
                "pieces_per_box": _f(raw.get("PiecesPerBox")) or None,
                "litres_per_piece": _f(raw.get("LitresPerPiece")) or None,
                "stock_value": _f(raw.get("StockValue")),
                "uom": raw.get("Uom") or "",
                "gross_weight_per_case": float(gross) if gross is not None else None,
            })
        rolled = roll_up_tonnage(rows)
        return {
            **rolled,
            "items": len(rows),
            "on_hand_total": sum(r["on_hand"] for r in rows),
            "value": sum(r["stock_value"] for r in rows),
            "unconfigured_items": sum(1 for r in rows if r["pieces_per_box"] is None),
            # The weight table, kept so the non-moving feed (which carries no
            # unit of measure) can be weighed against the same item master.
            "weights": {
                r["item_code"].strip(): kilograms_per_piece(r)
                for r in rows
                if kilograms_per_piece(r) is not None
            },
        }

    def _read_warehouse_settings(self) -> Dict[str, Any]:
        """The two facts about a warehouse SAP does not hold, typed in by an
        operator. The factory creates the row on first read; here an absent
        row answers nulls, which is the same "not configured" state."""
        row = self.postgres.one(
            """
            SELECT capacity_tonnes, last_audit_date
            FROM stock_dashboard_warehouseboardsettings
            WHERE company_code = %s AND warehouse = %s
            """,
            [self.scope.settings_company, self.scope.warehouse],
        ) or {}
        return {
            "warehouse": self.scope.warehouse,
            "capacity_tonnes": _f_or_none(row.get("capacity_tonnes")),
            "last_audit_date": formatting.iso(formatting.as_date(row.get("last_audit_date"))),
        }

    def _read_board_settings(self) -> Dict[str, Any]:
        """Company-level figures nothing derives: the owned fleet, the labour
        rate and the per-section staffing (stock_dashboard.LogisticsBoardSettings)."""
        row = self.postgres.one(
            """
            SELECT owned_vehicles, owned_vehicle_numbers, vehicles_out_of_service,
                   labour_rate_per_day,
                   warehouse_employees, warehouse_salary_monthly,
                   dispatch_employees, dispatch_salary_monthly,
                   transport_employees, transport_salary_monthly
            FROM stock_dashboard_logisticsboardsettings
            WHERE company_code = %s
            """,
            [self.scope.settings_company],
        ) or {}

        def daily(monthly):
            value = _f_or_none(monthly)
            return None if value is None else round(value / SALARY_DAYS_IN_MONTH, 2)

        def count(value):
            return None if value is None else _int(value)

        return {
            "owned_vehicles": count(row.get("owned_vehicles")),
            "owned_vehicle_list": self._plate_list(row.get("owned_vehicle_numbers")),
            "out_of_service_list": self._plate_list(row.get("vehicles_out_of_service")),
            "labour_rate_per_day": _f_or_none(row.get("labour_rate_per_day")),
            "warehouse_employees": count(row.get("warehouse_employees")),
            "warehouse_salary_daily": daily(row.get("warehouse_salary_monthly")),
            "dispatch_employees": count(row.get("dispatch_employees")),
            "dispatch_salary_daily": daily(row.get("dispatch_salary_monthly")),
            "transport_employees": count(row.get("transport_employees")),
            "transport_salary_daily": daily(row.get("transport_salary_monthly")),
        }

    @staticmethod
    def _plate_list(raw: Optional[str]) -> List[str]:
        """Registrations from a newline-separated block, normalised: a plate
        is typed differently every time, and "hr69 e 4548" is HR69E4548."""
        seen: List[str] = []
        for line in (raw or "").splitlines():
            plate = line.strip().upper().replace(" ", "").replace("-", "")
            if plate and plate not in seen:
                seen.append(plate)
        return seen

    def _read_non_moving(self, stock: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Finished goods idle for more than 40 days in this warehouse, split
        at 90, weighed against the stock read's item master.

        The factory's non-moving read (non_moving_rm.hana_reader) answers for
        every (item, warehouse) pair in the company and takes only ``age``
        and ``item_group``, so the warehouse is filtered HERE, as the page
        does. Its SQL is copied as it stands (the packing-material clock
        included, though group 102 never trips it); the production rule is
        left on, which is the board's standing default.
        """
        hana = self.hana
        OITW, OITM, OITB, OWHS, OINM = (hana.table(t) for t in ("OITW", "OITM", "OITB", "OWHS", "OINM"))
        production_types = ", ".join(str(t) for t in (TRANS_TYPE_GOODS_ISSUE, TRANS_TYPE_PRODUCTION_RECEIPT))
        moved = 'COALESCE(N."InQty", 0) <> 0 OR COALESCE(N."OutQty", 0) <> 0'
        in_window = f'N."DocDate" >= ADD_DAYS(CURRENT_DATE, -{CONSUMPTION_WINDOW_DAYS})'
        received = f'COALESCE(N."InQty", 0) > 0 AND N."TransType" = {TRANS_TYPE_GRPO}'
        sql = f"""
WITH Stock AS (
    SELECT
        M."ItemCode",
        M."ItemName",
        G."ItmsGrpNam" AS "ItemGroupName",
        '' AS "SubGroup",
        M."CreateDate",
        W."WhsCode",
        COALESCE(H."WhsName", W."WhsCode") AS "WhsName",
        COALESCE(W."OnHand", 0) AS "OnHand",
        SUM(COALESCE(W."OnHand", 0)) OVER (PARTITION BY M."ItemCode") AS "ItemOnHand",
        CASE
            WHEN COALESCE(W."AvgPrice", 0) <> 0 THEN W."AvgPrice"
            WHEN COALESCE(M."AvgPrice", 0) <> 0 THEN M."AvgPrice"
            ELSE COALESCE(M."LastPurPrc", 0)
        END AS "UnitCost",
        {LITRES_PER_UNIT} AS "LitresPerUnit",
        CASE
            WHEN M."ItmsGrpCod" = {PM_ITEM_GROUP} THEN 1
            WHEN UPPER(COALESCE(G."ItmsGrpNam", '')) = '{PM_ITEM_GROUP_NAME}' THEN 1
            ELSE 0
        END AS "IsPackingMaterial"
    FROM {OITW} W
    INNER JOIN {OITM} M
        ON M."ItemCode" = W."ItemCode"
    INNER JOIN {OITB} G
        ON G."ItmsGrpCod" = M."ItmsGrpCod"
    INNER JOIN {OWHS} H
        ON H."WhsCode" = W."WhsCode"
    WHERE COALESCE(W."OnHand", 0) > 0
      AND COALESCE(H."Inactive", 'N') <> 'Y'
      AND M."ItmsGrpCod" = ?
),
Movement AS (
    SELECT
        N."ItemCode",
        N."Warehouse",
        MAX(
            CASE WHEN {moved} THEN N."DocDate" END
        ) AS "LastMovementDate",
        MAX(
            CASE
                WHEN ({moved}) AND N."TransType" IN ({production_types})
                THEN N."DocDate"
            END
        ) AS "LastProductionDate",
        MAX(
            CASE
                WHEN ({moved}) AND COALESCE(N."TransType", 0) <> {TRANS_TYPE_TRANSFER_IN}
                THEN N."DocDate"
            END
        ) AS "LastNonTransferDate",
        MAX(
            CASE WHEN {received} THEN N."DocDate" END
        ) AS "LastGrpoDate",
        SUM(
            CASE WHEN {in_window} THEN COALESCE(N."OutQty", 0) ELSE 0 END
        ) AS "IssuedInWindow",
        SUM(
            CASE
                WHEN ({in_window}) AND N."TransType" = {TRANS_TYPE_GOODS_ISSUE}
                THEN COALESCE(N."OutQty", 0)
                ELSE 0
            END
        ) AS "ProductionIssuedInWindow"
    FROM {OINM} N
    WHERE N."ItemCode" IN (SELECT "ItemCode" FROM Stock)
    GROUP BY N."ItemCode", N."Warehouse"
),
ItemMovement AS (
    SELECT
        "ItemCode",
        MAX("LastProductionDate") AS "LastProductionDate",
        MAX("LastNonTransferDate") AS "LastNonTransferDate",
        MAX("LastGrpoDate") AS "LastGrpoDate",
        SUM("ProductionIssuedInWindow") AS "ProductionIssuedInWindow"
    FROM Movement
    GROUP BY "ItemCode"
),
ItemMovementSource AS (
    SELECT
        "ItemCode",
        MAX(CASE WHEN "ProductionRank" = 1 THEN "Warehouse" END) AS "ProductionWarehouse",
        MAX(CASE WHEN "NonTransferRank" = 1 THEN "Warehouse" END) AS "NonTransferWarehouse",
        MAX(CASE WHEN "GrpoRank" = 1 THEN "Warehouse" END) AS "GrpoWarehouse"
    FROM (
        SELECT
            "ItemCode",
            "Warehouse",
            ROW_NUMBER() OVER (
                PARTITION BY "ItemCode"
                ORDER BY
                    CASE WHEN "LastProductionDate" IS NULL THEN 1 ELSE 0 END,
                    "LastProductionDate" DESC,
                    "Warehouse"
            ) AS "ProductionRank",
            ROW_NUMBER() OVER (
                PARTITION BY "ItemCode"
                ORDER BY
                    CASE WHEN "LastNonTransferDate" IS NULL THEN 1 ELSE 0 END,
                    "LastNonTransferDate" DESC,
                    "Warehouse"
            ) AS "NonTransferRank",
            ROW_NUMBER() OVER (
                PARTITION BY "ItemCode"
                ORDER BY
                    CASE WHEN "LastGrpoDate" IS NULL THEN 1 ELSE 0 END,
                    "LastGrpoDate" DESC,
                    "Warehouse"
            ) AS "GrpoRank"
        FROM Movement
    )
    GROUP BY "ItemCode"
),
Anchored AS (
    SELECT
        S."ItemCode",
        S."ItemName",
        S."ItemGroupName",
        S."SubGroup",
        S."OnHand",
        S."UnitCost",
        S."LitresPerUnit",
        S."WhsCode",
        S."WhsName",
        S."IsPackingMaterial",
        CASE
            WHEN S."IsPackingMaterial" = 1
                THEN COALESCE(
                    I."LastProductionDate",
                    I."LastNonTransferDate",
                    S."CreateDate"
                )
            ELSE COALESCE(V."LastMovementDate", S."CreateDate")
        END AS "MovementDate",
        COALESCE(V."LastMovementDate", S."CreateDate") AS "WarehouseMovementDate",
        CASE
            WHEN S."IsPackingMaterial" = 1
                THEN CASE
                    WHEN I."LastProductionDate" IS NOT NULL THEN P."ProductionWarehouse"
                    WHEN I."LastNonTransferDate" IS NOT NULL THEN P."NonTransferWarehouse"
                END
            WHEN V."LastMovementDate" IS NOT NULL THEN S."WhsCode"
        END AS "MovementWarehouse",
        CASE
            WHEN S."IsPackingMaterial" = 1 THEN 'production'
            ELSE 'any'
        END AS "MovementBasis",
        CASE
            WHEN S."IsPackingMaterial" = 1
                THEN COALESCE(I."ProductionIssuedInWindow", 0)
            ELSE COALESCE(V."IssuedInWindow", 0)
        END AS "IssuedInWindow",
        CASE
            WHEN S."IsPackingMaterial" = 1 THEN S."ItemOnHand"
            ELSE S."OnHand"
        END AS "ConsumptionBase"
    FROM Stock S
    LEFT JOIN Movement V
        ON V."ItemCode" = S."ItemCode"
       AND V."Warehouse" = S."WhsCode"
    LEFT JOIN ItemMovement I
        ON I."ItemCode" = S."ItemCode"
    LEFT JOIN ItemMovementSource P
        ON P."ItemCode" = S."ItemCode"
),
Report AS (
    SELECT
        A."ItemCode",
        A."ItemName",
        A."ItemGroupName",
        A."OnHand" AS "Quantity",
        ROUND(A."OnHand" * A."LitresPerUnit", 3) AS "Litres",
        A."SubGroup",
        ROUND(A."OnHand" * A."UnitCost", 4) AS "Value",
        A."MovementDate" AS "LastMovementDate",
        CASE
            WHEN A."MovementDate" IS NULL THEN 0
            ELSE DAYS_BETWEEN(A."MovementDate", CURRENT_DATE)
        END AS "DaysSinceLastMovement",
        ROUND(A."IssuedInWindow" / A."ConsumptionBase" * 100, 2) AS "ConsumptionRatio",
        A."WhsCode",
        A."WhsName",
        A."MovementBasis",
        A."WarehouseMovementDate" AS "LastWarehouseMovementDate",
        CASE
            WHEN A."WarehouseMovementDate" IS NULL THEN 0
            ELSE DAYS_BETWEEN(A."WarehouseMovementDate", CURRENT_DATE)
        END AS "DaysSinceWarehouseMovement",
        COALESCE(A."MovementWarehouse", '') AS "MovementWhsCode",
        COALESCE(MH."WhsName", A."MovementWarehouse", '') AS "MovementWhsName"
    FROM Anchored A
    LEFT JOIN {OWHS} MH
        ON MH."WhsCode" = A."MovementWarehouse"
)
SELECT
    "ItemCode",
    "ItemName",
    "ItemGroupName",
    "Quantity",
    "Litres",
    "SubGroup",
    "Value",
    "LastMovementDate",
    "DaysSinceLastMovement",
    "ConsumptionRatio",
    "WhsCode",
    "WhsName",
    "MovementBasis",
    "LastWarehouseMovementDate",
    "DaysSinceWarehouseMovement",
    "MovementWhsCode",
    "MovementWhsName"
FROM Report
WHERE "DaysSinceLastMovement" > ?
ORDER BY "DaysSinceLastMovement" DESC, "Value" DESC, "ItemCode", "WhsCode"
"""
        raw_rows = hana.rows(sql, [FINISHED_GOODS_ITEM_GROUP, NON_MOVING_FROM_DAYS])
        warehouse = self.scope.warehouse.upper()
        rows = [
            {
                "item_code": r.get("ItemCode") or "",
                "quantity": _f(r.get("Quantity")),
                "value": _f(r.get("Value")),
                "days_since_last_movement": _int(r.get("DaysSinceLastMovement")),
            }
            for r in raw_rows
            if (r.get("WhsCode") or "").strip().upper() == warehouse
        ]
        # Weighed against the stock read's weight table (the feed itself
        # carries no unit of measure). A stock read that failed weighs nothing,
        # and every item lands in the unweighed count, which the tile shows.
        weights = (stock or {}).get("weights") or {}
        stock_rows = [
            {"item_code": code, "uom": "PCS", "gross_weight_per_case": kg, "pieces_per_box": 1}
            for code, kg in weights.items()
        ]
        recent = [r for r in rows if r["days_since_last_movement"] <= NON_MOVING_AGEING_DAYS]
        ageing = [r for r in rows if r["days_since_last_movement"] > NON_MOVING_AGEING_DAYS]
        everything = weigh_items(rows, stock_rows)
        return {
            "items": len(rows),
            "recent": len(recent),
            "ageing": len(ageing),
            "tonnes": everything["tonnes"],
            "recent_tonnes": weigh_items(recent, stock_rows)["tonnes"],
            "ageing_tonnes": weigh_items(ageing, stock_rows)["tonnes"],
            "unweighed": everything["unweighed"],
            "quantity": sum(r["quantity"] for r in rows),
            "value": sum(r["value"] for r in rows),
        }

    # ------------------------------------------------------ dispatch plans
    def _bill_weights(self, hana, doc_entries: Sequence[int], *, exclude_credited: bool, exclude_sap_dispatched: bool) -> Dict[int, float]:
        """Kilograms per SAP invoice, for the bills a plan window resolved to.

        The weight chain is the dispatch reader's own (dispatch_plans
        .hana_reader._build_bills_query): the line's ``Weight1``, else
        ``Weight2``, else gross case weight over the pack factor. The two
        exclusions are the pending tile's backstops: an invoice credited out
        is not waiting to leave, and ``U_Dipatch_Date`` is SAP's own stamp
        that the bill has gone, which the app's booking status can fail to
        flip (591 of 617 invoices on BH-BT). A bill the filters drop is
        simply absent from the result.
        """
        weights: Dict[int, float] = {}
        entries = [int(d) for d in dict.fromkeys(doc_entries)]
        if not entries:
            return weights
        OINV, INV1, OITM = hana.table("OINV"), hana.table("INV1"), hana.table("OITM")
        gross = 'IFNULL(I."U_Gross_Weight", 0)'
        pack = 'CASE WHEN IFNULL(I."SalFactor2", 0) > 0 THEN IFNULL(I."SalFactor2", 0) ELSE 1 END'
        for start in range(0, len(entries), BILL_LOOKUP_CHUNK):
            chunk = entries[start:start + BILL_LOOKUP_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            clauses = ['H."CANCELED" = \'N\'', f'H."DocEntry" IN ({placeholders})']
            if exclude_sap_dispatched:
                clauses.append('H."U_Dipatch_Date" IS NULL')
            if exclude_credited:
                clauses.append(
                    f'NOT EXISTS (SELECT 1 FROM {hana.table("RIN1")} CN'
                    f' JOIN {hana.table("ORIN")} CH ON CH."DocEntry" = CN."DocEntry"'
                    ' WHERE CN."BaseType" = 13 AND CN."BaseEntry" = H."DocEntry"'
                    ' AND IFNULL(CH."CANCELED", \'N\') = \'N\')'
                )
            header_filter = " AND ".join(clauses)
            sql = f"""
                WITH line_agg AS (
                    SELECT
                        L."DocEntry" AS doc_entry,
                        SUM(
                            CASE
                                WHEN IFNULL(L."Weight1", 0) > 0 THEN IFNULL(L."Weight1", 0)
                                WHEN IFNULL(L."Weight2", 0) > 0 THEN IFNULL(L."Weight2", 0)
                                WHEN {gross} > 0
                                    THEN IFNULL(L."Quantity", 0) * {gross} / {pack}
                                ELSE 0
                            END
                        ) AS total_weight
                    FROM {INV1} L
                    LEFT JOIN {OITM} I
                        ON I."ItemCode" = L."ItemCode"
                    WHERE L."DocEntry" IN (
                        SELECT H."DocEntry"
                        FROM {OINV} H
                        WHERE {header_filter}
                    )
                    GROUP BY L."DocEntry"
                )
                SELECT
                    H."DocEntry" AS doc_entry,
                    TO_NVARCHAR(H."DocNum") AS doc_num,
                    IFNULL(LA.total_weight, 0) AS total_weight
                FROM {OINV} H
                LEFT JOIN line_agg LA
                    ON LA.doc_entry = H."DocEntry"
                WHERE {header_filter}
            """
            # header_filter is bound once inside the CTE and once outside.
            for row in hana.rows(sql, [*chunk, *chunk]):
                weights[int(_col(row, "doc_entry"))] = _f(_col(row, "total_weight"))
        return weights

    def _selected_plans(self, company_id: int, *, date_from: date, date_to: date, statuses: Optional[Sequence[str]], limit: int) -> List[Dict[str, Any]]:
        """Plans dated to leave in the window, on the Plan page (selected),
        newest dispatch date first, capped. The factory caps the window
        BEFORE testing the selection; testing it in the query keeps the same
        rows in and lets the cap bite later."""
        sql = """
            SELECT p.sap_invoice_doc_entry AS doc_entry, p.sap_invoice_doc_num AS doc_num,
                   p.dispatch_date, p.booking_status, p.vehicle_id
            FROM dispatch_plans_dispatchplan p
            WHERE p.company_id = %s AND p.is_active
              AND p.dispatch_date >= %s AND p.dispatch_date <= %s
              AND EXISTS (
                  SELECT 1 FROM dispatch_plans_selecteddispatchbill s
                  WHERE s.company_id = p.company_id
                    AND s.sap_invoice_doc_entry = p.sap_invoice_doc_entry
                    AND s.is_active
              )
        """
        params: List[Any] = [company_id, date_from, date_to]
        if statuses:
            sql += " AND p.booking_status = ANY(%s)"
            params.append(list(statuses))
        sql += " ORDER BY p.dispatch_date DESC, p.id DESC LIMIT %s"
        params.append(limit)
        return self.postgres.rows(sql, params)

    def _read_pending_dispatch(self) -> Dict[str, Any]:
        """Bills dated to leave that have not gone, Oil and Mart side by side.

        One plan read per company AND per open booking state (the slices keep
        an all-time window under the row cap), then SAP for each bill's
        weight with the credited and SAP-dispatched bills dropped. Both
        un-booked and booked count: a bill with a truck against it still has
        not left. Deliberately NOT scoped to the warehouse: a plan covers
        whichever warehouse the bill was picked from, and Mart has no BH-BT.
        """
        ids = self._company_id_map()
        unread: List[str] = []
        by_company: List[Dict[str, Any]] = []
        all_bills: List[Dict[str, Any]] = []

        def read(code: str, hana) -> Dict[str, Any]:
            plans: Dict[int, Dict[str, Any]] = {}
            truncated = False
            if code in ids:
                for status in PENDING_STATUSES:
                    rows = self._selected_plans(
                        ids[code], date_from=PENDING_WINDOW[0], date_to=PENDING_WINDOW[1],
                        statuses=[status], limit=MAX_ROLLUP_ROWS,
                    )
                    truncated = truncated or len(rows) >= MAX_ROLLUP_ROWS
                    for row in rows:
                        plans[int(row["doc_entry"])] = row
            weights = self._bill_weights(
                hana, list(plans), exclude_credited=True, exclude_sap_dispatched=True
            )
            bills = []
            for doc_entry, weight in weights.items():
                plan = plans[doc_entry]
                if plan.get("dispatch_date") is None or plan.get("booking_status") == "DISPATCHED":
                    continue
                bills.append({
                    "doc_entry": doc_entry,
                    "company_code": code,
                    "weight_kg": weight,
                    "vehicle_id": plan.get("vehicle_id"),
                })
            return {"bills": bills, "truncated": truncated}

        results, unread = self._each_dispatch_company(read)
        for code in self.scope.dispatch_companies:
            result = results.get(code)
            if result is None:
                continue
            bills = result["bills"]
            by_company.append({
                "company_code": code,
                "label": company_label(code),
                "invoices": len(bills),
                "tonnes": sum(b["weight_kg"] for b in bills) / 1000,
                "truncated": result["truncated"],
            })
            all_bills.extend(bills)

        booked = [b for b in all_bills if b.get("vehicle_id") is not None]
        tonnes = sum(b["weight_kg"] for b in all_bills) / 1000
        return {
            "by_company": by_company,
            "invoices": len(all_bills),
            "tonnes": tonnes,
            "planned_bills": len(all_bills),
            "booked_bills": len(booked),
            "planned_tonnes": tonnes,
            "booked_tonnes": sum(b["weight_kg"] for b in booked) / 1000,
            "truncated": any(c["truncated"] for c in by_company),
            "unread": unread,
        }

    def _read_day_plan(self) -> Dict[str, Any]:
        """Every bill scheduled to leave today, across the board's companies,
        with its SAP weight. Every booking state is kept (the factory asks
        for ``booking_status=all``): the plan is the whole day's commitment
        and must not shrink as trucks clear the gate."""
        ids = self._company_id_map()

        def read(code: str, hana) -> List[Dict[str, Any]]:
            if code not in ids:
                return []
            rows = self._selected_plans(
                ids[code], date_from=self.today, date_to=self.today, statuses=None, limit=MAX_ROLLUP_ROWS
            )
            plans = {int(r["doc_entry"]): r for r in rows}
            weights = self._bill_weights(hana, list(plans), exclude_credited=False, exclude_sap_dispatched=False)
            return [
                {"doc_entry": d, "company_code": code, "weight_kg": w, "vehicle_id": plans[d].get("vehicle_id")}
                for d, w in weights.items()
            ]

        results, unread = self._each_dispatch_company(read)
        bills = [bill for code in self.scope.dispatch_companies for bill in results.get(code, [])]
        return {**day_plan(bills), "unread": unread}

    def _read_allocated(self) -> Dict[str, Any]:
        """What the production floor declared into this warehouse today: the
        Godown Movements register, consignments whose destination IS this
        warehouse, every company. One day, because the register is a
        declaration log with no received state to total."""
        row = self.postgres.one(
            """
            SELECT COUNT(DISTINCT m.id) AS movements,
                   COALESCE(SUM(l.pieces), 0) AS pieces,
                   COALESCE(SUM(l.pieces * l.litres_per_piece), 0) AS litres
            FROM warehouse_pf_movement m
            LEFT JOIN warehouse_pf_movement_line l ON l.movement_id = m.id
            WHERE m.is_active AND m.destination_kind = 'GODOWN'
              AND m.to_warehouse = %s
              AND m.movement_date >= %s AND m.movement_date <= %s
            """,
            [self.scope.warehouse, self.today, self.today],
        ) or {}
        return {
            "movements": _int(row.get("movements")),
            "pieces": _int(row.get("pieces")),
            "litres": _f(row.get("litres")),
        }

    # ------------------------------------------------------------ dispatch
    def _read_dispatch_summary(self) -> Dict[str, Any]:
        """Trucks that physically left the gate, by gate-out date, both
        companies added server-side (dispatch_plans.dashboard_service). Three
        windows off one grouped read: today, the month to date, and
        yesterday (a closed day, its only job the comparison)."""
        ids = self._dispatch_company_ids()
        start = min(self.month_start, self.yesterday)
        rows = self.postgres.rows(
            """
            SELECT g.gate_out_date AS day,
                   COUNT(*) AS trucks,
                   COUNT(DISTINCT g.dispatch_plan_id) AS bills,
                   COALESCE(SUM(g.total_weight), 0) AS weight,
                   COALESCE(SUM(g.total_litres), 0) AS litres,
                   COALESCE(SUM(g.total_boxes), 0) AS boxes
            FROM gate_core_salesdispatchgateout g
            WHERE g.company_id = ANY(%s) AND g.status = 'DISPATCHED'
              AND g.gate_out_date >= %s AND g.gate_out_date <= %s
            GROUP BY g.gate_out_date
            ORDER BY g.gate_out_date
            """,
            [ids, start, self.today],
        ) if ids else []
        by_day = {}
        for row in rows:
            day = formatting.as_date(row.get("day"))
            if day is None:
                continue
            by_day[day] = {
                "trucks": _int(row.get("trucks")),
                "bills": _int(row.get("bills")),
                "weight": _f(row.get("weight")),
                "litres": _f(row.get("litres")),
                "boxes": _f(row.get("boxes")),
            }
        empty = {"trucks": 0, "bills": 0, "weight": 0.0, "litres": 0.0, "boxes": 0.0}
        month_days = [d for d in by_day if self.month_start <= d <= self.today]
        # Zero-filled from the 1st: a day nothing moved is a gap in the row,
        # not a day that closes up.
        trend = []
        cursor = self.month_start
        while cursor <= self.today:
            found = by_day.get(cursor, empty)
            trend.append({
                "date": cursor.isoformat(),
                "tonnes": found["weight"] / 1000,
                "trucks": found["trucks"],
                "bills": found["bills"],
                "litres": found["litres"],
                "boxes": found["boxes"],
            })
            cursor += timedelta(days=1)
        today = by_day.get(self.today, empty)
        yesterday = by_day.get(self.yesterday, empty)
        return {
            "companies": [c for c in self.scope.dispatch_companies if c in self._company_id_map()],
            "today": {"tonnes": today["weight"] / 1000, "vehicles": today["trucks"], "invoices": today["bills"], "litres": today["litres"]},
            "yesterday": {"tonnes": yesterday["weight"] / 1000},
            "month": {
                "tonnes": sum(by_day[d]["weight"] for d in month_days) / 1000,
                "litres": sum(by_day[d]["litres"] for d in month_days),
                "trucks": sum(by_day[d]["trucks"] for d in month_days),
                # Days that had a dispatch: the divisor the average wants.
                "active_days": len(month_days),
            },
            "trend": trend,
        }

    def _read_partial_scans(self) -> Dict[str, Any]:
        """Dockings released without a full box scan, this month, both
        companies. Only APPROVED rows count; windowed on the review date
        (the request date where unreviewed) in the factory's own day."""
        ids = self._dispatch_company_ids()
        next_month = formatting.month_end(self.today) + timedelta(days=1)
        tz = formatting.tz().key
        row = (self.postgres.one(
            """
            SELECT COUNT(*) AS approvals,
                   COALESCE(SUM(expected_boxes), 0) AS expected_boxes,
                   COALESCE(SUM(scanned_boxes), 0) AS scanned_boxes,
                   COALESCE(SUM(GREATEST(expected_boxes - scanned_boxes, 0)), 0) AS shortfall_boxes
            FROM docking_admin_dockingpartialscanrequest
            WHERE company_id = ANY(%s) AND status = 'APPROVED'
              AND (COALESCE(reviewed_at, requested_at) AT TIME ZONE %s)::date >= %s
              AND (COALESCE(reviewed_at, requested_at) AT TIME ZONE %s)::date < %s
            """,
            [ids, tz, self.month_start, tz, next_month],
        ) if ids else None) or {}
        return {
            "approvals": _int(row.get("approvals")),
            "expected_boxes": _int(row.get("expected_boxes")),
            "scanned_boxes": _int(row.get("scanned_boxes")),
            "shortfall_boxes": _int(row.get("shortfall_boxes")),
        }

    # ----------------------------------------------------------- transport
    def _read_fleet(self, settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The owned fleet and what each truck is doing today
        (stock_dashboard.views.OwnedVehicleStatusAPI). Ownership is not a
        field on the vehicle master, so the fleet is the registration list on
        board settings, resolved against three registers, highest first:
        out of service (configured), an open branch transfer (trucks that
        never touch the gate), a sales dispatch, then today's gate arrivals."""
        if settings is None:
            raise RuntimeError("Board settings were not read, so the owned fleet is unknown.")
        plates = settings["owned_vehicle_list"]
        off_road = set(settings["out_of_service_list"])
        empty = {"owned": 0, "on_bst": 0, "on_dispatch": 0, "at_plant": 0, "out": 0, "free": 0, "out_of_service": 0}
        if not plates:
            return {"configured": False, "vehicles": [], "summary": empty}

        def normalise(text):
            return (text or "").strip().upper().replace(" ", "").replace("-", "")

        in_transit: Dict[str, Dict[str, str]] = {}
        for row in self.postgres.rows(
            """
            SELECT v.vehicle_number, t.entry_no, t.sap_doc_num, t.sap_from_warehouse, t.sap_to_warehouse
            FROM warehouse_bsttransfer t
            JOIN vehicle_management_vehicle v ON v.id = t.vehicle_id
            WHERE t.status = ANY(%s)
            ORDER BY t.id DESC
            """,
            [list(ENGAGED_BST_STATUSES)],
        ):
            plate = normalise(row.get("vehicle_number"))
            if not plate or plate in in_transit:
                continue
            route = " to ".join(p for p in (row.get("sap_from_warehouse"), row.get("sap_to_warehouse")) if (p or "").strip())
            in_transit[plate] = {"reference": row.get("entry_no") or row.get("sap_doc_num") or "", "detail": route}

        on_dispatch: Dict[str, Dict[str, str]] = {}
        for row in self.postgres.rows(
            """
            SELECT v.vehicle_number, p.sap_invoice_doc_num, p.customer_name
            FROM dispatch_plans_dispatchplan p
            JOIN vehicle_management_vehicle v ON v.id = p.vehicle_id
            WHERE p.is_active AND p.booking_status <> 'DISPATCHED'
            ORDER BY p.id DESC
            """
        ):
            plate = normalise(row.get("vehicle_number"))
            if plate and plate not in on_dispatch:
                on_dispatch[plate] = {"reference": row.get("sap_invoice_doc_num") or "", "detail": row.get("customer_name") or ""}
        # A gate-out is the stronger fact: the truck has actually left.
        for row in self.postgres.rows(
            """
            SELECT v.vehicle_number, g.sap_doc_num
            FROM gate_core_salesdispatchgateout g
            JOIN gate_core_vehiclearrival a ON a.id = g.arrival_id
            JOIN vehicle_management_vehicle v ON v.id = a.vehicle_id
            WHERE g.gate_out_date = %s
            """,
            [self.today],
        ):
            plate = normalise(row.get("vehicle_number"))
            if plate:
                on_dispatch[plate] = {"reference": str(row.get("sap_doc_num") or "").strip(), "detail": "left the gate today"}

        gate_state: Dict[str, str] = {}
        for row in self.postgres.rows(
            """
            SELECT v.vehicle_number, a.status
            FROM gate_core_vehiclearrival a
            JOIN vehicle_management_vehicle v ON v.id = a.vehicle_id
            WHERE a.gate_in_date = %s AND a.status <> 'CANCELLED'
            """,
            [self.today],
        ):
            plate = normalise(row.get("vehicle_number"))
            if not plate:
                continue
            at_plant = row.get("status") in ("INSIDE", "LOADING")
            if at_plant or gate_state.get(plate) != "AT_PLANT":
                gate_state[plate] = "AT_PLANT" if at_plant else "OUT"

        vehicles = []
        for plate in plates:
            if plate in off_road:
                state = "OUT_OF_SERVICE"
            elif plate in in_transit:
                state = "ON_BST"
            elif plate in on_dispatch:
                state = "ON_DISPATCH"
            else:
                state = gate_state.get(plate, "FREE")
            work = in_transit.get(plate, {}) if state == "ON_BST" else on_dispatch.get(plate, {}) if state == "ON_DISPATCH" else {}
            vehicles.append({"vehicle_no": plate, "state": state, "reference": work.get("reference", ""), "detail": work.get("detail", "")})

        def count(state):
            return sum(1 for v in vehicles if v["state"] == state)

        return {
            "configured": True,
            "vehicles": vehicles,
            "summary": {
                "owned": len(vehicles),
                "on_bst": count("ON_BST"),
                "on_dispatch": count("ON_DISPATCH"),
                "at_plant": count("AT_PLANT"),
                "out": count("OUT"),
                "free": count("FREE"),
                "out_of_service": count("OUT_OF_SERVICE"),
            },
        }

    def _read_transit(self) -> Dict[str, Any]:
        """Stock invoiced to a sister company that SAP has not booked in, by
        age (stock_dashboard.views.StockInTransitAPI). A load is on the road
        until the receiving company answers the sending company's invoice
        with a Goods Receipt PO quoting it in ``NumAtCard``: SAP's own
        evidence, not a status somebody sets by hand."""
        absent = self.scope.absent.get("stock_in_transit")
        bands = {
            "fresh": {"label": "Up to 3 days", "loads": 0, "tonnes": 0.0},
            "ageing": {"label": "4 - 7 days", "loads": 0, "tonnes": 0.0},
            "stale": {"label": "Over 7 days", "loads": 0, "tonnes": 0.0},
        }
        if absent:
            return {"absent": absent, "bands": bands, "totals": {"loads": 0, "tonnes": 0.0}, "loads": [], "unweighed_lines": 0}

        loads = []
        unweighed = 0
        for sender, customers, receiver in TRANSIT_ROUTES:
            sending = self.hana_for(sender)
            receiving = self.hana_for(receiver)
            if receiver != self.scope.warehouse_company:
                # The receiver is a secondary company here. Its SAP missing is
                # not the warehouse company's outage, so it must not latch SAP
                # off for the rest of the build (the freight account and the
                # cost per litre still read Oil, and say Mart is missing). The
                # tile itself cannot be answered without the receipts, so it
                # degrades on its own, as a data error.
                try:
                    receiving.table("OPDN")
                except SAPConnectionError as exc:
                    raise SAPDataError(
                        f"{company_label(receiver)}'s SAP could not be read, so receipts "
                        f"against these loads are unknown: {exc}"
                    ) from exc
            for row in self._unreceived_dispatches(sending, receiving, customers):
                days_out = row["days_out"]
                key = "fresh" if days_out <= TRANSIT_FRESH_DAYS else "ageing" if days_out <= TRANSIT_AGEING_DAYS else "stale"
                bands[key]["loads"] += 1
                bands[key]["tonnes"] += row["kilograms"] / 1000
                unweighed += row["unweighed_lines"]
                loads.append({**row, "tonnes": round(row["kilograms"] / 1000, 3), "band": key})
        for band in bands.values():
            band["tonnes"] = round(band["tonnes"], 2)
        return {
            "absent": None,
            "bands": bands,
            "totals": {"loads": sum(b["loads"] for b in bands.values()), "tonnes": round(sum(b["tonnes"] for b in bands.values()), 2)},
            "loads": loads,
            "unweighed_lines": unweighed,
        }

    def _unreceived_dispatches(self, sending, receiving, customer_codes: Sequence[str]) -> List[Dict[str, Any]]:
        """stock_dashboard.hana_reader.get_unreceived_intercompany_dispatches,
        with the receiving schema reached through its own reader."""
        codes = [c for c in {(c or "").strip() for c in customer_codes} if c]
        floors = [w for w in {(w or "").strip().upper() for w in TRANSIT_SHIPPING_WAREHOUSES} if w]
        if not codes or not floors:
            return []
        gross = self._gross_weight_expr("i")
        code_slots = ", ".join("?" for _ in codes)
        floor_slots = ", ".join("?" for _ in floors)
        net_qty = 'GREATEST(COALESCE(l."Quantity", 0) - COALESCE(cr."Qty", 0), 0)'
        weighed = f"""
            CASE
                WHEN COALESCE(l."Weight1", 0) > 0 AND COALESCE(l."Quantity", 0) > 0
                    THEN l."Weight1" * {net_qty} / l."Quantity"
                WHEN {gross} IS NOT NULL AND COALESCE(i."SalFactor2", 0) > 0
                    THEN {net_qty} * {gross} / i."SalFactor2"
                ELSE NULL
            END
        """
        sql = f"""
            WITH credited AS (
                SELECT
                    c."BaseEntry" AS "DocEntry",
                    c."BaseLine"  AS "LineNum",
                    SUM(c."Quantity") AS "Qty"
                FROM {sending.table("RIN1")} c
                JOIN {sending.table("ORIN")} n
                  ON n."DocEntry" = c."DocEntry" AND n."CANCELED" = 'N'
                WHERE c."BaseType" = 13
                GROUP BY c."BaseEntry", c."BaseLine"
            ),
            sent AS (
                SELECT
                    h."DocNum" AS "DocNum",
                    h."DocDate" AS "DocDate",
                    SUM(COALESCE({weighed}, 0)) AS "Kilograms",
                    SUM(CASE
                        WHEN {net_qty} > 0 AND {weighed} IS NULL THEN 1 ELSE 0
                    END) AS "Unweighed"
                FROM {sending.table("OINV")} h
                JOIN {sending.table("INV1")} l ON l."DocEntry" = h."DocEntry"
                JOIN {sending.table("OITM")} i ON i."ItemCode" = l."ItemCode"
                LEFT JOIN credited cr
                  ON cr."DocEntry" = l."DocEntry" AND cr."LineNum" = l."LineNum"
                WHERE h."CANCELED" = 'N'
                  AND h."CardCode" IN ({code_slots})
                  AND UPPER(l."WhsCode") IN ({floor_slots})
                  AND h."DocDate" >= ADD_DAYS(CURRENT_DATE, ?)
                GROUP BY h."DocNum", h."DocDate"
                HAVING SUM({net_qty}) > 0
            ),
            received AS (
                SELECT DISTINCT TO_NVARCHAR(TRIM(r."NumAtCard")) AS "Ref"
                FROM {receiving.table("OPDN")} r
                WHERE r."CANCELED" = 'N' AND r."NumAtCard" IS NOT NULL
            )
            SELECT
                s."DocNum",
                s."DocDate",
                DAYS_BETWEEN(s."DocDate", CURRENT_DATE) AS "DaysOut",
                s."Kilograms",
                s."Unweighed"
            FROM sent s
            LEFT JOIN received r ON r."Ref" = TO_NVARCHAR(s."DocNum")
            WHERE r."Ref" IS NULL
            ORDER BY s."DocDate" DESC, s."DocNum" DESC
        """
        params = [*codes, *floors, -abs(TRANSIT_LOOKBACK_DAYS)]
        return [
            {
                "doc_num": _int(row.get("DocNum")),
                "doc_date": formatting.iso(formatting.as_date(row.get("DocDate"))),
                "days_out": _int(row.get("DaysOut")),
                "kilograms": _f(row.get("Kilograms")),
                "unweighed_lines": _int(row.get("Unweighed")),
            }
            for row in sending.rows(sql, params)
        ]

    def _read_transporter_account(self) -> Dict[str, Any]:
        """What the plant owes its hauliers and what it paid, Oil and Mart
        added band by band (dispatch_plans.transporter_account_reader). A
        transporter is a vendor in the TRANSPORTER group; outstanding is
        ``DocTotal - PaidToDate`` on OPEN invoices; ageing runs from
        ``DocDate``. The per-vendor list is not read: no tile shows it."""

        def read(code: str, hana) -> Dict[str, Any]:
            return {
                "awaiting_invoice": self._awaiting_invoice(hana),
                "outstanding": self._outstanding(hana),
                "payments": self._payments(hana, FREIGHT_PAYMENT_DAYS),
            }

        results, unread = self._each_dispatch_company(read)
        read_accounts = [results[c] for c in self.scope.dispatch_companies if c in results]

        awaiting: Dict[int, Dict[str, Any]] = {}
        buckets: Dict[int, Dict[str, Any]] = {}
        for account in read_accounts:
            for bucket in account["awaiting_invoice"]["buckets"]:
                merged = awaiting.get(bucket["band"])
                if merged is None:
                    awaiting[bucket["band"]] = dict(bucket)
                    continue
                merged["documents"] += bucket["documents"]
                merged["amount"] += bucket["amount"]
                merged["oldest_days"] = _max_or_none(merged["oldest_days"], bucket["oldest_days"])
            for bucket in account["outstanding"]["buckets"]:
                merged = buckets.get(bucket["band"])
                if merged is None:
                    buckets[bucket["band"]] = dict(bucket)
                    continue
                merged["documents"] += bucket["documents"]
                merged["outstanding"] += bucket["outstanding"]
                merged["billed"] += bucket["billed"]
                merged["paid"] += bucket["paid"]
                merged["oldest_days"] = _max_or_none(merged["oldest_days"], bucket["oldest_days"])

        latest_dates = sorted(a["payments"]["latest_date"] for a in read_accounts if a["payments"]["latest_date"])
        oldest_awaiting = None
        oldest_outstanding = None
        for account in read_accounts:
            oldest_awaiting = _max_or_none(oldest_awaiting, account["awaiting_invoice"]["oldest_days"])
            oldest_outstanding = _max_or_none(oldest_outstanding, account["outstanding"]["oldest_days"])
        return {
            "awaiting_invoice": {
                "documents": sum(a["awaiting_invoice"]["documents"] for a in read_accounts),
                "amount": sum(a["awaiting_invoice"]["amount"] for a in read_accounts),
                "oldest_days": oldest_awaiting,
                "buckets": [awaiting[b] for b in sorted(awaiting)],
            },
            "outstanding": sum(a["outstanding"]["outstanding"] for a in read_accounts),
            "documents": sum(a["outstanding"]["documents"] for a in read_accounts),
            "billed": sum(a["outstanding"]["billed"] for a in read_accounts),
            "oldest_days": oldest_outstanding,
            "buckets": [buckets[b] for b in sorted(buckets)],
            "payments": {
                "window_days": FREIGHT_PAYMENT_DAYS,
                "payments": sum(a["payments"]["payments"] for a in read_accounts),
                "paid": sum(a["payments"]["paid"] for a in read_accounts),
                "latest_date": latest_dates[-1] if latest_dates else None,
            },
            "unread": unread,
        }

    @staticmethod
    def _band_case(column: str) -> str:
        return f"""
                CASE
                    WHEN DAYS_BETWEEN({column}, CURRENT_DATE) >= {AGE_BANDS[2]} THEN {AGE_BANDS[2]}
                    WHEN DAYS_BETWEEN({column}, CURRENT_DATE) >= {AGE_BANDS[1]} THEN {AGE_BANDS[1]}
                    WHEN DAYS_BETWEEN({column}, CURRENT_DATE) >= {AGE_BANDS[0]} THEN {AGE_BANDS[0]}
                    ELSE 0
                END"""

    def _awaiting_invoice(self, hana) -> Dict[str, Any]:
        """Freight received into SAP (service GRPOs, OPDN ``DocType = 'S'``)
        that nobody has raised an A/P invoice against. The amount is
        ``PDN1.OpenSum`` over the lines still open: a GRPO can be
        part-invoiced, and a header total would count money already invoiced.
        PRE-TAX, so not addable to the payment stage beside it."""
        band = self._band_case('D."DocDate"')
        sql = f"""
            SELECT
                {band} AS band,
                COUNT(DISTINCT D."DocEntry") AS docs,
                SUM(L."OpenSum") AS open_sum,
                MAX(DAYS_BETWEEN(D."DocDate", CURRENT_DATE)) AS oldest_days
            FROM {hana.table("OPDN")} D
            JOIN {hana.table("PDN1")} L
              ON L."DocEntry" = D."DocEntry" AND L."LineStatus" = 'O'
            JOIN {hana.table("OCRD")} C ON C."CardCode" = D."CardCode"
            JOIN {hana.table("OCRG")} G ON G."GroupCode" = C."GroupCode"
            WHERE G."GroupName" = ?
              AND G."GroupType" = 'S'
              AND D."DocStatus" = 'O'
              AND IFNULL(D."CANCELED", 'N') = 'N'
            GROUP BY {band}
        """
        buckets = {b: {"band": b, "documents": 0, "amount": 0.0, "oldest_days": None} for b in (0, *AGE_BANDS)}
        for row in hana.rows(sql, [TRANSPORTER_GROUP_NAME]):
            b = _int(_col(row, "band"))
            oldest = _col(row, "oldest_days")
            buckets[b] = {
                "band": b,
                "documents": _int(_col(row, "docs")),
                "amount": _f(_col(row, "open_sum")),
                "oldest_days": int(oldest) if oldest is not None else None,
            }
        ordered = [buckets[b] for b in (0, *AGE_BANDS)]
        oldest = [b["oldest_days"] for b in ordered if b["oldest_days"] is not None]
        return {
            "documents": sum(b["documents"] for b in ordered),
            "amount": sum(b["amount"] for b in ordered),
            "oldest_days": max(oldest) if oldest else None,
            "buckets": ordered,
        }

    def _outstanding(self, hana) -> Dict[str, Any]:
        """Open freight invoices (OPCH), split into exclusive age buckets."""
        band = self._band_case('H."DocDate"')
        sql = f"""
            SELECT
                {band} AS band,
                COUNT(*) AS docs,
                SUM(H."DocTotal" - H."PaidToDate") AS outstanding,
                SUM(H."DocTotal") AS billed,
                SUM(H."PaidToDate") AS paid,
                MAX(DAYS_BETWEEN(H."DocDate", CURRENT_DATE)) AS oldest_days
            FROM {hana.table("OPCH")} H
            JOIN {hana.table("OCRD")} C ON C."CardCode" = H."CardCode"
            JOIN {hana.table("OCRG")} G ON G."GroupCode" = C."GroupCode"
            WHERE G."GroupName" = ?
              AND G."GroupType" = 'S'
              AND H."DocStatus" = 'O'
              AND IFNULL(H."CANCELED", 'N') = 'N'
            GROUP BY {band}
        """
        buckets = {b: {"band": b, "documents": 0, "outstanding": 0.0, "billed": 0.0, "paid": 0.0, "oldest_days": None} for b in (0, *AGE_BANDS)}
        for row in hana.rows(sql, [TRANSPORTER_GROUP_NAME]):
            get = lambda key: _col(row, key)  # noqa: E731
            b = _int(get("band"))
            oldest = get("oldest_days")
            buckets[b] = {
                "band": b,
                "documents": _int(get("docs")),
                "outstanding": _f(get("outstanding")),
                "billed": _f(get("billed")),
                "paid": _f(get("paid")),
                "oldest_days": int(oldest) if oldest is not None else None,
            }
        ordered = [buckets[b] for b in (0, *AGE_BANDS)]
        oldest = [b["oldest_days"] for b in ordered if b["oldest_days"] is not None]
        return {
            "documents": sum(b["documents"] for b in ordered),
            "outstanding": sum(b["outstanding"] for b in ordered),
            "billed": sum(b["billed"] for b in ordered),
            "paid_against_open": sum(b["paid"] for b in ordered),
            "oldest_days": max(oldest) if oldest else None,
            "buckets": ordered,
        }

    def _payments(self, hana, days: int) -> Dict[str, Any]:
        """Money paid out to transporters in the window (OVPM, vendor
        payments only). ``Canceled`` with one L is that table's spelling."""
        sql = f"""
            SELECT
                COUNT(*) AS payments,
                SUM(V."DocTotal") AS paid,
                MAX(V."DocDate") AS latest
            FROM {hana.table("OVPM")} V
            JOIN {hana.table("OCRD")} C ON C."CardCode" = V."CardCode"
            JOIN {hana.table("OCRG")} G ON G."GroupCode" = C."GroupCode"
            WHERE G."GroupName" = ?
              AND G."GroupType" = 'S'
              AND V."DocType" = 'S'
              AND IFNULL(V."Canceled", 'N') = 'N'
              AND V."DocDate" >= ADD_DAYS(CURRENT_DATE, ?)
        """
        row = hana.one(sql, [TRANSPORTER_GROUP_NAME, -days]) or {}
        get = lambda key: _col(row, key)  # noqa: E731
        return {
            "window_days": days,
            "payments": _int(get("payments")),
            "paid": _f(get("paid")),
            "latest_date": formatting.iso(formatting.as_date(get("latest"))),
        }

    def _read_grpo_queue(self) -> Dict[str, Any]:
        """The service-GRPO receipt queue, counted across both companies by
        how long it has waited (grpo.views_service_pending_summary). The whole
        backlog grouped by bilty, never a page and never one month: on the
        12th nothing dispatched can be older than 11 days, so a month bound
        makes the 45-day band structurally zero."""
        ids = self._company_id_map()
        buckets = {b: {"band": b, "documents": 0, "amount": 0.0, "unpriced": 0} for b in (0, *AGE_BANDS)}
        documents = amount = unpriced = undated = ready = 0
        oldest_days = None
        unread: List[str] = []
        for code in self.scope.dispatch_companies:
            if code not in ids:
                unread.append(code)
                continue
            for plan in self._pending_service_grpos(ids[code]):
                documents += 1
                if plan["ready"]:
                    ready += 1
                value = plan["amount"]
                if value > 0:
                    amount += value
                else:
                    unpriced += 1
                if plan["dispatch_date"] is None:
                    undated += 1
                    continue
                age = (self.today - plan["dispatch_date"]).days
                oldest_days = age if oldest_days is None else max(oldest_days, age)
                band = 0
                for floor in AGE_BANDS:
                    if age >= floor:
                        band = floor
                buckets[band]["documents"] += 1
                if value > 0:
                    buckets[band]["amount"] += value
                else:
                    buckets[band]["unpriced"] += 1
        return {
            "documents": documents,
            "amount": amount,
            "unpriced": unpriced,
            "undated": undated,
            "ready": ready,
            "oldest_days": oldest_days,
            "buckets": [buckets[b] for b in (0, *AGE_BANDS)],
            "unread": unread,
        }

    def _pending_service_grpos(self, company_id: int) -> List[Dict[str, Any]]:
        """grpo.services.GRPOService.get_pending_service_grpo_entries(all_months=True):
        BOOKED and DISPATCHED plans, grouped by bilty (one bilty covering N
        invoices is ONE pending GRPO), minus every group with a posted GRPO.
        Freight is not a blocker; a plan without one is counted unpriced."""
        rows = self.postgres.rows(
            """
            SELECT p.id, p.booking_status, p.dispatch_date, p.bilty_no, p.bilty_date,
                   p.linked_vehicle_entry_id, p.vehicle_id, p.transporter_id,
                   v.vehicle_number, p.freight, p.total_freight, p.bilty_attachment,
                   (
                       EXISTS (
                           SELECT 1 FROM grpo_servicegrpoposting sp
                           WHERE sp.dispatch_plan_id = p.id AND sp.status = 'POSTED'
                       )
                       OR EXISTS (
                           SELECT 1 FROM grpo_servicegrpolineposting sl
                           JOIN grpo_servicegrpoposting sp2 ON sp2.id = sl.service_grpo_posting_id
                           WHERE sl.dispatch_plan_id = p.id AND sp2.status = 'POSTED'
                       )
                   ) AS posted
            FROM dispatch_plans_dispatchplan p
            LEFT JOIN vehicle_management_vehicle v ON v.id = p.vehicle_id
            WHERE p.company_id = %s AND p.is_active
              AND p.booking_status IN ('BOOKED', 'DISPATCHED')
            ORDER BY p.updated_at DESC, p.created_at DESC
            """,
            [company_id],
        )
        grouped: Dict[Any, Dict[str, Any]] = {}
        posted_keys = set()
        for row in rows:
            bilty = (row.get("bilty_no") or "").strip()
            if not bilty:
                key = ("dispatch-plan", row["id"])
            else:
                key = (
                    "bilty", bilty, formatting.iso(formatting.as_date(row.get("bilty_date"))),
                    row.get("linked_vehicle_entry_id") or 0, row.get("vehicle_id") or 0,
                    row.get("transporter_id") or 0, (row.get("vehicle_number") or "").strip().upper(),
                )
            if row.get("posted"):
                posted_keys.add(key)
                grouped.pop(key, None)
                continue
            if key in posted_keys or key in grouped:
                continue
            total_freight = row.get("total_freight")
            freight = total_freight if total_freight is not None else row.get("freight")
            grouped[key] = {
                "id": row["id"],
                "booking_status": row.get("booking_status"),
                "dispatch_date": formatting.as_date(row.get("dispatch_date")),
                "amount": round(_f(freight), 2),
                "ready": bool(bilty) and bool((row.get("bilty_attachment") or "").strip()),
            }
        return list(grouped.values())

    def _read_freight_rate(self) -> Dict[str, Any]:
        """Outbound freight posted in the month, Oil and Mart added BEFORE
        dividing (dispatch_plans.freight_rate_service). SAP alone: service
        GRPOs (OPDN ``DocType = 'S'``) for TRANSPORTER vendors posting to the
        outward-freight account, ``SUM(PDN1.LineTotal)`` pre-tax. Windowed on
        the GRPO's own DocDate, so early in a month the rate reads LOW until
        the paperwork catches up."""

        def read(code: str, hana) -> Dict[str, Any]:
            sql = f"""
            SELECT
                D."CardCode",
                MAX(D."CardName") AS card_name,
                COUNT(DISTINCT D."DocEntry") AS docs,
                SUM(L."LineTotal") AS freight
            FROM {hana.table("OPDN")} D
            JOIN {hana.table("PDN1")} L ON L."DocEntry" = D."DocEntry"
            JOIN {hana.table("OCRD")} C ON C."CardCode" = D."CardCode"
            JOIN {hana.table("OCRG")} G ON G."GroupCode" = C."GroupCode"
            WHERE G."GroupName" = ?
              AND G."GroupType" = 'S'
              AND D."DocType" = 'S'
              AND IFNULL(D."CANCELED", 'N') = 'N'
              AND L."AcctCode" = ?
              AND D."DocDate" >= ?
              AND D."DocDate" <= ?
            GROUP BY D."CardCode"
            ORDER BY SUM(L."LineTotal") DESC
            """
            vendors = []
            for row in hana.rows(sql, [TRANSPORTER_GROUP_NAME, OUTWARD_FREIGHT_ACCOUNT, self.month_start, self.today]):
                get = lambda key: _col(row, key)  # noqa: E731
                vendors.append({
                    "card_code": get("CardCode") or "",
                    "transporter_name": (get("card_name") or "").strip() or (get("CardCode") or "Unassigned"),
                    "documents": _int(get("docs")),
                    "amount": _f(get("freight")),
                })
            return {"amount": sum(v["amount"] for v in vendors), "documents": sum(v["documents"] for v in vendors), "vendors": vendors}

        results, unread = self._each_dispatch_company(read)
        read_rates = [results[c] for c in self.scope.dispatch_companies if c in results]
        # Same haulier under both companies is one row, keyed on the NAME.
        by_name: Dict[str, Dict[str, Any]] = {}
        for rate in read_rates:
            for vendor in rate["vendors"]:
                merged = by_name.get(vendor["transporter_name"])
                if merged is None:
                    by_name[vendor["transporter_name"]] = dict(vendor)
                else:
                    merged["amount"] += vendor["amount"]
                    merged["documents"] += vendor["documents"]
        return {
            "amount": sum(r["amount"] for r in read_rates),
            "documents": sum(r["documents"] for r in read_rates),
            "vendors": len(by_name),
            "by_transporter": sorted(by_name.values(), key=lambda v: v["amount"], reverse=True)[:MAX_VENDOR_ROWS],
            "date_from": self.month_start.isoformat(),
            "date_to": self.today.isoformat(),
            "unread": unread,
        }

    # ----------------------------------------------------------- workforce
    def _read_expense(self) -> Dict[str, Any]:
        """Today's labour off the factory-expense board (factory_expense
        .services.labour_costs), every active company: the gate's own head
        count per department, priced from the Cost Master's ``factory-labour``
        rate resolved most-specific-first per entry. An entry whose
        department has no rate is still COUNTED; its people land in the
        warning so the board can say the rate is missing instead of implying
        the labour was free."""
        companies = self.postgres.rows("SELECT id, code FROM company_company WHERE is_active")
        company_ids = [int(c["id"]) for c in companies]
        if not company_ids:
            return {"labour_departments": [], "labour_today": 0.0, "labour_headcount": 0, "unpriced": 0, "labour_warning": "No company is active in the factory database."}

        rates = self.postgres.rows(
            """
            SELECT r.id, r.scope, r.company_id, r.department_id, r.basis, r.rate, r.effective_from
            FROM cost_master_costrate r
            JOIN cost_master_costtype t ON t.id = r.cost_type_id
            WHERE t.code = %s AND t.is_active AND r.is_active
              AND r.effective_from <= %s
              AND (r.company_id IS NULL OR r.company_id = ANY(%s))
            """,
            [LABOUR_COST_TYPE_CODE, self.today, company_ids],
        )
        shared = [r for r in rates if r.get("company_id") is None]
        by_company = {cid: list(shared) for cid in company_ids}
        for rate in rates:
            if rate.get("company_id") is not None and rate["company_id"] in by_company:
                by_company[rate["company_id"]].append(rate)

        entries = self.postgres.rows(
            """
            SELECT e.company_id, e.department_id, d.name AS department, e.count_in
            FROM labour_gate_labourgateentry e
            LEFT JOIN accounts_department d ON d.id = e.department_id
            WHERE e.work_date = %s AND e.is_active AND e.company_id = ANY(%s)
            """,
            [self.today, company_ids],
        )

        departments: Dict[str, Dict[str, Any]] = {}
        total_cost = 0.0
        total_heads = 0
        unpriced = 0
        flat_charged = set()
        for entry in entries:
            rate = self._resolve_rate(by_company.get(entry.get("company_id"), []), entry.get("department_id"), self.today)
            headcount = _int(entry.get("count_in"))
            cost = round(self._price_labour(rate, headcount), 2) if rate else 0.0
            if rate is None and headcount:
                unpriced += headcount
            if rate is not None and rate.get("basis") == "PER_DAY":
                # Flat daily charge: once for the day per company.
                flat_key = (self.today, entry.get("company_id"))
                if flat_key in flat_charged:
                    cost = 0.0
                else:
                    flat_charged.add(flat_key)
            total_cost += cost
            total_heads += headcount
            # The gate's intake arrives with no department ("Unallocated") and
            # the HOD's re-split of the same people as further rows; both are
            # kept, and the caller must never add the grand total on top.
            name = entry.get("department") or "Unallocated"
            bucket = departments.setdefault(name, {"department": name, "headcount": 0, "cost": 0.0})
            bucket["headcount"] += headcount
            bucket["cost"] += cost

        warning = None
        if unpriced:
            warning = (
                f"{unpriced} labourers have no rate in this period — set "
                f"'{LABOUR_COST_TYPE_CODE}' in Admin › Cost Master."
            )
        return {
            "labour_departments": sorted(departments.values(), key=lambda r: r["cost"], reverse=True),
            "labour_today": round(total_cost, 2),
            "labour_headcount": total_heads,
            "unpriced": unpriced,
            "labour_warning": warning,
        }

    @staticmethod
    def _resolve_rate(rates: Sequence[Dict[str, Any]], department_id, on_date: date) -> Optional[Dict[str, Any]]:
        """The one Cost Master row that prices a thing on ``on_date``
        (factory_expense.rates.resolve): most specific scope first, a
        company-specific row over the company-agnostic one, latest start."""
        best = None
        best_rank = None
        for rate in rates:
            effective = formatting.as_date(rate.get("effective_from"))
            if effective is None or effective > on_date:
                continue
            scope = rate.get("scope")
            if scope == "FACTORY":
                pass
            elif scope == "COMPANY":
                if rate.get("company_id") is None:
                    continue
            elif scope == "DEPARTMENT":
                if department_id is None or rate.get("department_id") != department_id:
                    continue
            else:
                continue
            rank = (SCOPE_SPECIFICITY[scope], 1 if rate.get("company_id") is not None else 0, effective, _int(rate.get("id")))
            if best_rank is None or rank > best_rank:
                best, best_rank = rate, rank
        return best

    @staticmethod
    def _price_labour(rate: Dict[str, Any], headcount: int) -> float:
        """What one gate entry costs, according to the rate's own basis.
        PER_PERSON_DAY multiplies by the head count; PER_DAY is a flat charge
        that does not care how many turned up."""
        amount = _f(rate.get("rate"))
        basis = rate.get("basis")
        if basis == "PER_PERSON_DAY":
            return amount * headcount
        if basis == "PER_DAY":
            return amount if headcount else 0.0
        if basis == "PER_MONTH":
            return amount / 30 if headcount else 0.0
        return 0.0

    def _read_employee_roll(self) -> Dict[str, Any]:
        """The permanent staff on the roll, both companies added
        (employee_hierarchy.views.EmployeeMetaAPI). On-roll, not on shift:
        the directory counts everyone still employed, including somebody on
        leave. Never added to the labour count without saying so."""
        ids = self._company_id_map()
        headcount = 0
        departments: List[Dict[str, Any]] = []
        unread: List[str] = []
        for code in self.scope.dispatch_companies:
            if code not in ids:
                unread.append(code)
                continue
            headcount += _int(self.postgres.value(
                "SELECT COUNT(*) FROM employee_hierarchy_employee WHERE company_id = %s AND employment_status = ANY(%s)",
                [ids[code], list(IN_SERVICE_STATUSES)],
                default=0,
            ))
            for row in self.postgres.rows(
                """
                SELECT d.code, d.name,
                       COUNT(e.id) FILTER (WHERE e.employment_status = ANY(%s)) AS employee_count
                FROM employee_hierarchy_department d
                LEFT JOIN employee_hierarchy_employee e ON e.department_id = d.id
                WHERE d.company_id = %s
                GROUP BY d.id, d.code, d.name
                """,
                [list(IN_SERVICE_STATUSES), ids[code]],
            ):
                departments.append({
                    "company_code": code,
                    "code": row.get("code") or "",
                    "name": row.get("name") or "",
                    "headcount": _int(row.get("employee_count")),
                })
        return {"headcount": headcount, "departments": departments, "unread": unread}

    # ----------------------------------------------------------- compose
    @staticmethod
    def _labour_for_section(departments: Sequence[Dict[str, Any]], names: Sequence[str]) -> Dict[str, Any]:
        """The subset of labour assigned to one card, matched case-insensitively
        (the org chart says *Despatch* where every route says *Dispatch*). An
        empty list means the card has no assignment yet, which is reported
        rather than rendered as zero."""
        if not names:
            return {"headcount": 0, "cost": 0.0, "assigned": False}
        wanted = {n.strip().lower() for n in names}
        matched = [d for d in departments if (d.get("department") or "").strip().lower() in wanted]
        return {
            "headcount": sum(max(_int(d.get("headcount")), 0) for d in matched),
            "cost": sum(max(_f(d.get("cost")), 0.0) for d in matched),
            "assigned": True,
        }

    @staticmethod
    def _employees_for_section(departments: Sequence[Dict[str, Any]], names: Sequence[str]) -> Optional[int]:
        """On-roll employees belonging to one card, matched on the department's
        name OR code. None when nothing matches: an unknown section is an
        unknown head count, not an empty one."""
        if not names:
            return None
        wanted = {n.strip().lower() for n in names}
        matched = [
            d for d in departments
            if (d.get("name") or "").strip().lower() in wanted or (d.get("code") or "").strip().lower() in wanted
        ]
        if not matched:
            return None
        return sum(max(_int(d.get("headcount")), 0) for d in matched)

    @staticmethod
    def _strip(*, employees, employee_cost_per_day, labour, labour_cost, labour_assigned, rate_configured, labour_read) -> Dict[str, Any]:
        """One Employees/Labour strip, saying why a figure is missing. Three
        things can hollow it out and all look like ₹0: no salary typed, no
        labour rate configured, nobody assigned. Each says so instead."""
        notes = []
        if employees is None:
            notes.append("Employee headcount not set")
        elif employee_cost_per_day is None:
            notes.append("Salary not set")
        if not labour_read:
            notes.append("Labour not read")
        elif not labour_assigned:
            notes.append("No departments assigned")
        elif not rate_configured:
            notes.append("No labour rate configured")
        labour_known = labour_read and labour is not None
        labour_value = labour if labour_known else None
        both_known = employees is not None and labour_value is not None
        on_shift = (employees + labour_value) if both_known else (labour_value if labour_value is not None else employees)
        total = (employees + labour_value) if both_known else 0
        return {
            "employees": employees,
            "employee_cost_per_day": employee_cost_per_day,
            "labour": labour_value,
            "labour_cost_per_day": (labour_cost if (labour_known and rate_configured) else None),
            "note": " · ".join(notes) if notes else None,
            # OpsPeople's geometry.
            "on_shift": on_shift,
            "employee_cost_text": money(employee_cost_per_day) + " a day" if employee_cost_per_day is not None else "Cost unavailable",
            "labour_cost_text": (
                money(labour_cost) + " a day" if (labour_known and rate_configured and labour_cost is not None) else "Cost unavailable"
            ),
            "split": (
                {"employees": round(employees / total * 100, 2), "labour": round(labour_value / total * 100, 2)}
                if both_known and total > 0 else None
            ),
        }

    def _compose_workforce(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """Employees and labour, per card and in total (the hook's ``workforce``).

        The head count on a card is the figure typed on the settings screen,
        and the directory's own answer only where nothing has been typed: the
        settings screen gives no sign it is being overridden, and the
        directory covers one company and repeats names across floors. A rate
        typed on the settings screen wins over Cost Master, which holds a
        ``factory-labour`` rate but has none seeded.
        """
        expense = p.get("expense")
        settings = p.get("board_settings") or {}
        roll = p.get("employee_roll")
        scope = self.scope
        departments = expense["labour_departments"] if expense else None
        labour_read = expense is not None
        configured_rate = settings.get("labour_rate_per_day")
        labour_warning = expense["labour_warning"] if expense else None
        rate_configured = configured_rate is not None or (labour_read and not labour_warning)
        gate_total = sum(max(_int(d["headcount"]), 0) for d in departments) if departments is not None else None
        roll_departments = roll["departments"] if roll else []
        roll_headcount = roll["headcount"] if roll else None

        def strip(section: str) -> Dict[str, Any]:
            labour = self._labour_for_section(departments or [], scope.section_departments[section])
            on_roll = settings.get(f"{section}_employees")
            if on_roll is None:
                on_roll = self._employees_for_section(roll_departments, scope.section_employee_departments[section])
            labour_cost = labour["headcount"] * configured_rate if configured_rate is not None else labour["cost"]
            return self._strip(
                employees=on_roll,
                employee_cost_per_day=settings.get(f"{section}_salary_daily"),
                labour=labour["headcount"],
                labour_cost=labour_cost,
                labour_assigned=labour["assigned"],
                rate_configured=rate_configured,
                labour_read=labour_read,
            )

        if gate_total is not None:
            total_labour_cost = gate_total * configured_rate if configured_rate is not None else _f(expense["labour_today"])
        else:
            total_labour_cost = None
        total = self._strip(
            employees=roll_headcount,
            employee_cost_per_day=None,
            labour=gate_total,
            labour_cost=total_labour_cost,
            labour_assigned=True,
            rate_configured=rate_configured,
            labour_read=labour_read,
        )

        # Labour priced off the gate's head count, plus the three configured
        # section salaries. Null, not zero, when neither half is priced.
        if configured_rate is not None and gate_total is not None:
            labour_cost = gate_total * configured_rate
        elif labour_read and not labour_warning:
            labour_cost = _f(expense["labour_today"])
        else:
            labour_cost = None
        salaries = [settings.get(f"{s}_salary_daily") for s in ("warehouse", "dispatch", "transport")]
        salaries = [s for s in salaries if s is not None]
        employee_cost = sum(salaries) if salaries else None
        total_cost = None if (labour_cost is None and employee_cost is None) else (labour_cost or 0.0) + (employee_cost or 0.0)

        return {
            "total": total,
            "total_cost_per_day": total_cost,
            "warehouse": strip("warehouse"),
            "dispatch": strip("dispatch"),
            "transport": strip("transport"),
            "roll": {
                "headcount": roll_headcount,
                "unread": roll["unread"] if roll else [],
            },
        }

    def _compose_topbar(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """OpsTopbar: the live pill, the identity, two chips, two totals."""
        scope = self.scope
        dispatch = p.get("dispatch")
        workforce = p["workforce"]
        companies = dispatch["companies"] if dispatch else list(scope.dispatch_companies)
        active_days = dispatch["month"]["active_days"] if dispatch else None
        roll = workforce["roll"]
        company_count = len(companies) or len(scope.dispatch_companies)
        total_labour = workforce["total"]["labour"]
        return {
            "title": scope.title,
            "scope": f"{scope.warehouse} · {' | '.join(companies)}" if companies else f"{scope.warehouse} terminal",
            "chips": [
                {"label": "Month", "value": f"{MONTH_NAMES[self.month_start.month - 1]} {self.month_start.year}"},
                {
                    "label": "Dispatching days",
                    "value": "not read" if active_days is None else (f"{active_days} so far" if active_days > 0 else "none yet"),
                },
            ],
            "totals": [
                {
                    "caption": "Employees on roll",
                    "value": whole(roll["headcount"]) if roll["headcount"] is not None else None,
                    "missing": roll["headcount"] is None,
                    "sub": (
                        f"{' and '.join(company_label(c) for c in roll['unread'])} unread"
                        if roll["unread"]
                        else ("1 company" if company_count == 1 else f"{company_count} companies")
                    ),
                },
                {
                    "caption": "Workforce cost today",
                    "value": page_money(workforce["total_cost_per_day"]) if workforce["total_cost_per_day"] is not None else None,
                    "missing": workforce["total_cost_per_day"] is None,
                    "sub": "head count unavailable" if total_labour is None else f"{whole(total_labour)} labour on shift",
                },
            ],
        }

    @staticmethod
    def _tile(key: str, name: str, section: Optional[str], **fields) -> Dict[str, Any]:
        tile = {
            "key": key,
            "name": name,
            "section": section,
            "tag": None,
            "sub": "",
            "value": None,
            "unit": None,
            "missing": None,
            "tall": False,
            "viz": None,
            "notes": [],
        }
        tile.update(fields)
        if tile["missing"]:
            # A tile with nothing behind it never wears a condition colour.
            tile["tag"] = {"label": (tile["tag"] or {}).get("label", "no source"), "tone": "nil"}
        return tile

    @staticmethod
    def _band(key: str, domain: str, title: str, scope: str, tiles: List[Dict[str, Any]], people) -> Dict[str, Any]:
        return {
            "key": key,
            "domain": domain,
            "title": title,
            "scope": scope,
            "columns": f"repeat({len(tiles)}, minmax(0, 1fr))",
            "tiles": tiles,
            "people": people,
        }

    def _warehouse_band(self, p: Dict[str, Any]) -> Dict[str, Any]:
        scope = self.scope
        stock = p.get("stock")
        settings = p.get("warehouse_settings")
        non_moving = p.get("non_moving")
        pending = p.get("pending_dispatch")
        capacity = settings["capacity_tonnes"] if settings else None
        tiles = []

        # Stock on hand: tonnage against the rated capacity someone typed in.
        # Pallet slots answer a different question and are the fallback, not
        # ported here (see the module docstring).
        if stock is not None:
            has_tonnage = stock["weighed_items"] > 0
            fill_pct = (stock["tonnes"] / capacity * 100) if (capacity and capacity > 0 and has_tonnage) else None
            last_audit = settings["last_audit_date"] if settings else None
            sub = " · ".join(s for s in (
                f"{whole(capacity)} T capacity" if capacity is not None else None,
                f"last audit {day_label(last_audit)}" if last_audit else None,
            ) if s)
            notes = []
            viz = None
            if fill_pct is not None:
                viz = meter([
                    {"fill": "main", "pct": fill_pct, "label": "Stored"},
                    {"fill": "mute", "pct": max(0.0, 100 - fill_pct), "label": "Free", "figure": f"{whole(max(0.0, capacity - stock['tonnes']))} T"},
                ])
            elif has_tonnage and settings is not None and capacity is None:
                notes.append("No rated capacity is set, and the pallet-slot fallback is not read on this wall.")
            if settings is None:
                tag = {"label": "capacity not read", "tone": "nil"}
            elif fill_pct is not None:
                tag = {"label": f"{dec(fill_pct, 0)}% full", "tone": "neut"}
            else:
                tag = {"label": "capacity not set", "tone": "nil"}
            tiles.append(self._tile(
                "stock", "Stock on hand", "stock",
                tag=tag, sub=sub,
                value=whole(stock["tonnes"]) if has_tonnage else None, unit="tonnes",
                missing=None if has_tonnage else "No weighable stock rows in this warehouse",
                viz=viz, notes=notes,
                figures={"tonnes": stock["tonnes"], "fill_pct": fill_pct, "capacity_tonnes": capacity},
            ))
        else:
            tiles.append(self._tile("stock", "Stock on hand", "stock"))

        # Non-moving: share of the warehouse the idle stock is sitting on,
        # against the rated capacity where set, else the stock on hand.
        if non_moving is not None:
            basis = capacity if (capacity and capacity > 0) else (stock["tonnes"] if stock and stock["tonnes"] > 0 else None)
            space_pct = (non_moving["tonnes"] / basis * 100) if basis else None
            if space_pct is None:
                tag = {"label": f"{whole(non_moving['items'])} items", "tone": "neut"}
            else:
                tag = {"label": f"{dec(space_pct)}% of space", "tone": "warn" if space_pct >= 10 else "neut"}
            total = non_moving["tonnes"]
            notes = []
            if non_moving["unweighed"] > 0:
                notes.append(f"{whole(non_moving['unweighed'])} of {whole(non_moving['items'])} items have no case weight in SAP, so this is a floor.")
            tiles.append(self._tile(
                "non_moving", "Non-moving stock", "non_moving",
                tag=tag, sub=f"{NON_MOVING_FROM_DAYS}+ days",
                value=dec(total), unit="tonnes",
                viz=meter([
                    {"fill": "light", "pct": (non_moving["recent_tonnes"] / total * 100) if total > 0 else 0, "label": f"{NON_MOVING_FROM_DAYS}–{NON_MOVING_AGEING_DAYS} days", "figure": f"{dec(non_moving['recent_tonnes'])} T"},
                    {"fill": "main", "pct": (non_moving["ageing_tonnes"] / total * 100) if total > 0 else 0, "label": f"{NON_MOVING_AGEING_DAYS}+ days", "figure": f"{dec(non_moving['ageing_tonnes'])} T"},
                ]),
                notes=notes,
                figures={"tonnes": total, "space_pct": space_pct},
            ))
        else:
            tiles.append(self._tile("non_moving", "Non-moving stock", "non_moving"))

        # Pending dispatch: the total is the headline; the split is what
        # somebody at the board acts on, because the two companies load from
        # different floors.
        if pending is not None:
            notes = []
            if pending["truncated"]:
                notes.append("Feed truncated — it drops the oldest bills first, so this understates.")
            if pending["unread"]:
                notes.append(f"Bills missing {' and '.join(company_label(c) for c in pending['unread'])} — could not be read.")
            tiles.append(self._tile(
                "pending", "Pending dispatch", "pending_dispatch",
                tag={"label": f"{whole(pending['invoices'])} invoices", "tone": "neut"},
                value=dec(pending["tonnes"]), unit="tonnes",
                viz=duo([{"k": c["label"], "v": f"{dec(c['tonnes'])} T"} for c in pending["by_company"]]),
                notes=notes,
                figures={"tonnes": pending["tonnes"], "invoices": pending["invoices"]},
            ))
        else:
            tiles.append(self._tile("pending", "Pending dispatch", "pending_dispatch"))

        # Allocated stock: pieces and litres, never weight. The register
        # carries no per-item weight.
        if "allocated" not in scope.hidden:
            allocated = p.get("allocated")
            if allocated is not None:
                tiles.append(self._tile(
                    "allocated", "Allocated stock", "allocated",
                    tag=(
                        {"label": f"{whole(allocated['movements'])} consignments", "tone": "neut"}
                        if allocated["movements"] > 0 else {"label": "none today", "tone": "ok"}
                    ),
                    value=whole(allocated["pieces"]), unit="pieces",
                    viz=duo([{"k": "Pieces", "v": whole(allocated["pieces"])}, {"k": "Litres", "v": whole(allocated["litres"])}]),
                    figures={"pieces": allocated["pieces"], "litres": allocated["litres"]},
                ))
            else:
                tiles.append(self._tile("allocated", "Allocated stock", "allocated"))

        return self._band("warehouse", "warehouse", "Warehouse", scope.warehouse, tiles, p["workforce"]["warehouse"])

    def _dispatch_band(self, p: Dict[str, Any]) -> Dict[str, Any]:
        scope = self.scope
        dispatch = p.get("dispatch")
        plan = p.get("day_plan")
        pending = p.get("pending_dispatch")
        tiles = []
        companies = dispatch["companies"] if dispatch else list(scope.dispatch_companies)

        # Dispatched today, against yesterday (the day plan is already the
        # bar underneath). Green for up and amber for down: condition colours.
        if dispatch is not None:
            tonnes = dispatch["today"]["tonnes"]
            vs = day_on_day(tonnes, dispatch["yesterday"]["tonnes"])
            if vs is None:
                tag = {"label": "no day to compare", "tone": "nil"}
            elif vs["direction"] == "flat":
                tag = {"label": "level with yesterday", "tone": "neut"}
            else:
                arrow = "↑" if vs["direction"] == "up" else "↓"
                tag = {"label": f"{arrow} {dec(vs['pct'], 0)}% vs yesterday", "tone": "ok" if vs["direction"] == "up" else "warn"}
            viz = None
            notes = []
            plan_pct = None
            if plan is None:
                notes.append("Could not read today's dispatch plan.")
            elif not (plan["bills"] > 0 and plan["tonnes"] > 0):
                notes.append("No bill is booked onto a truck for today, so there is no day plan to measure against.")
            else:
                # Capped at 100: a day that beats its plan fills the bar and
                # says so in the figure beside it.
                plan_pct = min(100.0, tonnes / plan["tonnes"] * 100)
                viz = meter([
                    {"fill": "main", "pct": plan_pct, "label": "Dispatched"},
                    {"fill": "mute", "pct": 100 - plan_pct, "label": "Day plan", "figure": f"{dec(plan['tonnes'], 0)} T"},
                ])
                if plan["unread"]:
                    notes.append(f"Plan missing {' and '.join(company_label(c) for c in plan['unread'])} — could not read it.")
                else:
                    text = f"Booked bills {whole(plan['bills'])}"
                    if plan["unbooked_bills"] > 0:
                        text += f" · Awaiting a truck {whole(plan['unbooked_bills'])}"
                    notes.append(text)
            tiles.append(self._tile(
                "today", "Dispatched today", "dispatch",
                tag=tag, value=dec(tonnes), unit="tonnes", viz=viz, notes=notes,
                figures={"tonnes": tonnes, "vs_yesterday": vs, "plan_pct": plan_pct, "vehicles": dispatch["today"]["vehicles"], "invoices": dispatch["today"]["invoices"]},
            ))

            # Month to date, averaged over the days that actually dispatched.
            month = dispatch["month"]
            average = (month["tonnes"] / month["active_days"]) if month["active_days"] > 0 else None
            target = MONTHLY_TARGET_TONNES if MONTHLY_TARGET_TONNES > 0 else None
            target_pct = (month["tonnes"] / target * 100) if target else None
            sub = f"{day_label(self.month_start)} – {day_label(self.today)}"
            if average is not None:
                sub += f" · avg {dec(average)} T day"
            # The last seven calendar days, ending today, from the zero-filled
            # series; bars are a share of the best day shown.
            window = dispatch["trend"][-7:]
            peak = max((d["tonnes"] for d in window), default=0.0)
            tiles.append(self._tile(
                "month", "Month to date", "dispatch",
                tag=({"label": "no target set", "tone": "nil"} if target_pct is None else {"label": f"{dec(target_pct, 0)}% of target", "tone": "neut"}),
                sub=sub, value=whole(month["tonnes"]), unit="tonnes",
                viz=(bars([
                    {"label": d["date"][8:10], "pct": (d["tonnes"] / peak * 100) if peak > 0 else 0, "value": dec(d["tonnes"], 0) if d["tonnes"] > 0 else None}
                    for d in window
                ]) if window else None),
                notes=[] if window else ["No dispatching days yet this month."],
                figures={"tonnes": month["tonnes"], "active_days": month["active_days"], "average_per_active_day": average, "target_pct": target_pct},
            ))
        else:
            tiles.append(self._tile("today", "Dispatched today", "dispatch"))
            tiles.append(self._tile("month", "Month to date", "dispatch"))

        # Planned against booked, scaled against planned (booked is a subset).
        if pending is not None:
            planned, booked = pending["planned_bills"], pending["booked_bills"]
            if planned == 0:
                tag = {"label": "nothing planned", "tone": "ok"}
            elif booked < planned:
                tag = {"label": f"{whole(planned - booked)} awaiting a truck", "tone": "warn"}
            else:
                tag = {"label": "all booked", "tone": "ok"}
            tiles.append(self._tile(
                "planned", "Planned against booked", "pending_dispatch",
                tag=tag, tall=True,
                viz=pair(
                    [
                        {"label": "Planned", "figure": f"{whole(planned)} bills", "pct": 100, "fill": "mute"},
                        {"label": "Booked", "figure": f"{whole(booked)} bills", "pct": (booked / planned * 100) if planned > 0 else 0, "fill": "main"},
                    ],
                    note={"fill": "main", "label": "Booked", "figure": f"{dec(pending['booked_tonnes'])} T of {dec(pending['planned_tonnes'])} T"},
                ),
                figures={"planned_bills": planned, "booked_bills": booked},
            ))
        else:
            tiles.append(self._tile("planned", "Planned against booked", "pending_dispatch", tall=True))

        # Sent without barcodes. A floor that barcodes nothing produces an
        # empty register, so the tile leaves the band where the practice does
        # not exist (hidden) rather than reading "none this month".
        if "unscanned" not in scope.hidden:
            unscanned = p.get("unscanned")
            if unscanned is not None:
                expected = unscanned["expected_boxes"]
                tiles.append(self._tile(
                    "unscanned", "Sent without barcodes", "unscanned",
                    tag=({"label": "none this month", "tone": "ok"} if unscanned["approvals"] == 0 else {"label": f"{whole(unscanned['approvals'])} approvals", "tone": "bad"}),
                    value=whole(unscanned["shortfall_boxes"]), unit="boxes",
                    viz=meter([
                        {"fill": "main", "pct": (unscanned["shortfall_boxes"] / expected * 100) if expected > 0 else 0},
                        {"fill": "mute", "pct": (unscanned["scanned_boxes"] / expected * 100) if expected > 0 else 100, "label": "Scanned", "figure": whole(unscanned["scanned_boxes"])},
                    ]),
                    figures={"shortfall_boxes": unscanned["shortfall_boxes"], "approvals": unscanned["approvals"]},
                ))
            else:
                tiles.append(self._tile("unscanned", "Sent without barcodes", "unscanned"))

        return self._band("dispatch", "dispatch", "Dispatch", " | ".join(companies), tiles, p["workforce"]["dispatch"])

    def _transport_band(self, p: Dict[str, Any]) -> Dict[str, Any]:
        scope = self.scope
        fleet = p.get("fleet")
        settings = p.get("board_settings")
        account = p.get("transporter_account")
        queue = p.get("grpo_queue")
        freight = p.get("freight_rate")
        dispatch = p.get("dispatch")
        transit = p.get("transit")
        tiles = []

        # Owned vehicles: working over owned, "3/4", rather than the size
        # alone. Only meaningful once the registrations are entered.
        if fleet is not None:
            summary = fleet["summary"]
            configured = fleet["configured"]
            owned = summary["owned"] if configured else (settings["owned_vehicles"] if settings else None)
            on_duty = summary["on_bst"] + summary["on_dispatch"] + summary["at_plant"] + summary["out"]
            share = (lambda n: (n / owned * 100) if owned else 0.0)
            if not configured and owned is None:
                tag = {"label": "not set", "tone": "nil"}
            elif not configured:
                tag = {"label": "count only", "tone": "neut"}
            elif summary["out_of_service"] > 0:
                tag = {"label": f"{whole(summary['out_of_service'])} out of service", "tone": "bad"}
            else:
                tag = {"label": f"{whole(on_duty)} on duty", "tone": "neut"}
            viz = None
            notes = []
            if configured:
                idle = summary["free"] + summary["out_of_service"]
                busy = summary["on_dispatch"] + summary["at_plant"] + summary["out"]
                viz = meter([
                    {"fill": "main", "pct": share(summary["on_bst"]), "label": "On transfer", "figure": whole(summary["on_bst"])},
                    {"fill": "light", "pct": share(busy), "label": "On dispatch", "figure": whole(busy)},
                    {"fill": "mute", "pct": share(idle), "label": "Idle", "figure": whole(idle)},
                ])
                if summary["out_of_service"] > 0:
                    notes.append(f"{whole(summary['out_of_service'])} of the idle trucks are out of service.")
            elif owned is not None:
                notes.append("Only a count is configured — list the registrations to see which trucks are working.")
            tiles.append(self._tile(
                "fleet", "Owned vehicles", "fleet",
                tag=tag,
                value=(None if owned is None else (f"{whole(on_duty)}/{whole(owned)}" if configured else whole(owned))),
                unit="in use" if configured else "vehicles",
                missing="List the owned registrations in board settings" if owned is None else None,
                viz=viz, notes=notes,
                figures={"owned": owned, "on_duty": on_duty, "configured": configured},
            ))
        else:
            tiles.append(self._tile("fleet", "Owned vehicles", "fleet"))

        # Transport account: three stages, each aged from its own clock. The
        # GRPO column stays on the app's own queue (the only documents not in
        # SAP yet) and draws counts only; the two SAP columns carry money.
        reasons = self._reasons
        sap_unavailable = None if account else (reasons.get("transporter_account") or "Could not read the freight account from SAP")
        grpo_unavailable = None
        if queue is None:
            grpo_unavailable = reasons.get("grpo_queue") or "Could not read the receipt queue"
        elif len(queue["unread"]) >= len(scope.dispatch_companies):
            grpo_unavailable = "Could not read the receipt queue"
        columns = [
            {
                **funnel_column(
                    "GRPO",
                    [{"band": b["band"], "documents": b["documents"], "amount": b["amount"], "unpriced": b["unpriced"]} for b in queue["buckets"]] if queue else None,
                    FUNNEL_AGE_BANDS, grpo_unavailable,
                ),
                "counts_only": True,
            },
            funnel_column(
                "AP",
                [{"band": b["band"], "documents": b["documents"], "amount": b["amount"]} for b in account["awaiting_invoice"]["buckets"]] if account else None,
                FUNNEL_AGE_BANDS, sap_unavailable,
            ),
            funnel_column(
                "PAYMENT",
                [{"band": b["band"], "documents": b["documents"], "amount": b["outstanding"]} for b in account["buckets"]] if account else None,
                FUNNEL_AGE_BANDS, sap_unavailable,
            ),
        ]
        if account is None:
            tag = {"label": "SAP not read", "tone": "nil"}
        elif account["oldest_days"] is None:
            tag = {"label": "nothing outstanding", "tone": "ok"}
        else:
            oldest = account["oldest_days"]
            tag = {"label": f"oldest {whole(oldest)} days", "tone": "bad" if oldest >= 90 else "warn" if oldest >= 30 else "neut"}
        notes = []
        missing_parts = []
        if account and account["unread"]:
            missing_parts.append(f"Freight account missing {' and '.join(company_label(c) for c in account['unread'])}")
        if queue and queue["unread"] and grpo_unavailable is None:
            missing_parts.append(f"receipt queue missing {' and '.join(company_label(c) for c in queue['unread'])}")
        if missing_parts:
            notes.append(" · ".join(missing_parts) + " — could not be read.")
        tiles.append(self._tile(
            "account", "Transport account", None,
            tag=tag, tall=True, viz=matrix(columns), notes=notes,
            figures={
                "funnel": columns,
                "outstanding": account["outstanding"] if account else None,
                "oldest_days": account["oldest_days"] if account else None,
            },
        ))

        # Cost per litre: freight from SAP over the month's FULL dispatched
        # litres, two independent totals divided once, plus the configured
        # loading half. The pill says how many documents the rate rests on.
        if freight is not None:
            litres = dispatch["month"]["litres"] if dispatch else None
            rate = cost_per_litre(freight["amount"], litres or 0.0, litres or 0.0, LOADING_COST_PER_LITRE) if litres is not None else None
            if dispatch is None:
                missing = "Dispatched litres were not read, so there is nothing to divide by."
            elif rate is None:
                missing = "No bilty carries a freight figure this month, so there is nothing to divide."
            else:
                missing = None
            tiles.append(self._tile(
                "cost", "Cost per litre", "freight_rate",
                tag=({"label": "no source", "tone": "nil"} if rate is None else {"label": f"{whole(freight['documents'])} freight GRPOs", "tone": "neut"}),
                sub=("Freight and litres from the same bilties" if rate is None else f"{day_label(self.month_start)} – {day_label(self.today)} · {whole(rate['covered_litres'])} L dispatched"),
                value=None if rate is None else f"₹{rate['total']:.2f}",
                unit=None if rate is None else "per litre",
                missing=missing,
                viz=(None if rate is None else duo([{"k": "Freight", "v": f"₹{rate['freight']:.2f}"}, {"k": "Loading", "v": f"₹{rate['loading']:.2f}"}])),
                figures={"rate": rate, "freight_amount": freight["amount"], "documents": freight["documents"]},
            ))
        else:
            tiles.append(self._tile("cost", "Cost per litre", "freight_rate"))

        # Stock in transit: dispatched, no SAP receipt.
        if transit is not None:
            if transit["absent"]:
                tiles.append(self._tile(
                    "transit", "Stock in transit", "transit",
                    tag={"label": "no route", "tone": "nil"}, sub="Dispatched, no SAP receipt", missing=transit["absent"],
                ))
            else:
                bands_ = transit["bands"]
                total = transit["totals"]["tonnes"]
                share = (lambda t: (t / total * 100) if total > 0 else 0.0)
                if bands_["stale"]["loads"] > 0:
                    tag = {"label": f"{whole(bands_['stale']['loads'])} over 7 days", "tone": "bad"}
                else:
                    tag = {"label": f"{whole(transit['totals']['loads'])} loads", "tone": "neut"}
                notes = []
                if transit["unweighed_lines"] > 0:
                    notes.append(f"{whole(transit['unweighed_lines'])} lines have no case weight, so this is a floor.")
                tiles.append(self._tile(
                    "transit", "Stock in transit", "transit",
                    tag=tag, sub="Dispatched, no SAP receipt", value=dec(total), unit="tonnes",
                    viz=meter([
                        {"fill": "mute", "pct": share(bands_["fresh"]["tonnes"]), "label": "Up to 3 days", "figure": f"{dec(bands_['fresh']['tonnes'])} T"},
                        {"fill": "light", "pct": share(bands_["ageing"]["tonnes"]), "label": "4 - 7 days", "figure": f"{dec(bands_['ageing']['tonnes'])} T"},
                        {"fill": "main", "pct": share(bands_["stale"]["tonnes"]), "label": "Over 7 days", "figure": f"{dec(bands_['stale']['tonnes'])} T"},
                    ]),
                    notes=notes,
                    figures={"tonnes": total, "loads": transit["totals"]["loads"]},
                ))
        else:
            tiles.append(self._tile("transit", "Stock in transit", "transit"))

        return self._band("transport", "transport", "Transportation", "", tiles, p["workforce"]["transport"])
