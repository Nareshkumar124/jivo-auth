"""
The plant control board: the whole plant on one wall screen, in the order
material moves through it. A port of the factory's ``plant_board`` (its
``services.py``, ``hana_reader.py``, ``constants.py``, ``non_moving.py``,
``stacking.py`` and ``workforce.py``) plus the slices of the services it
composes (the packing-material requirement sheet, the Stock Benchmark and
Non-Moving dashboards, the plan module), payload for payload where the wall
shows it.

FOUR BANDS, TOP TO BOTTOM
-------------------------
Purchase (what we bought), Store (what the packaging stores hold), Production
(what the lines made and what stands on BH-PF), Shifting (what left the
floor). An admin reads down it and the day assembles itself: BH-PF filling up
is what happens when Production outruns Shifting, and the two are adjacent for
exactly that reason.

ONE SECTION PER TILE, NOT PER BAND
----------------------------------
The factory degrades a whole band when any read inside it fails, so a SAP
outage blanks the blowing tile and the packaging trucks too, although both are
read from the factory's own database. Here every tile is its own section and
says what it reads (``needs_sap`` / ``needs_factory``), so a SAP outage costs
exactly the tiles that read SAP. The figures inside each tile are the
factory's; only the blast radius is smaller.

Reads shared by two tiles are made once per build (``_once``): the plan's
packing requirement feeds both "Plan this month" and "Open POs", and a
failure is remembered too, so the second tile reports the same reason rather
than paying for the same timeout again.

WHAT IS AND IS NOT READ
-----------------------
SAP (``self.hana``, ``?`` placeholders, every table through
``self.hana.table``): the plan (``OFCT``/``FCT1``), its BOM explosion
(``OITT``/``ITT1``), packaging movements and stock (``OINM``, ``OITW``,
``OITM``, ``OITB``, ``OWHS``), purchase orders and goods receipts
(``OPOR``/``POR1``). The factory database (``self.postgres``, ``%s``): blowing
runs, production runs and segments, waste logs and material prices, gate PO
receipts, PF stock movements, BST box scans, and the board's own settings
(store capacity and audit dates, the pallet footprint, the workforce figures
typed on the settings page).

Figures only the factory's drill-down panels show (the open-PO rows, the
goods-receipt cross-check, the oil yield, the per-BST shipment list, the
over-purchase split) are not read: the panels are not ported, and a wall that
re-reads every minute should not pay for queries nobody sees.

"TODAY" IS THE FACTORY'S DAY
----------------------------
``self.today`` is the factory's local date, and every timestamp compared with
it (a gate entry, a dispatch, a waste row typed without a run) is cast to that
zone in SQL, which is what Django's ``__date`` lookups did in the factory.

TONS = LITRES / 1000
--------------------
Applied to oil and finished goods only. Packing material carries no litre
volume in SAP (``U_IsLitre`` is 'N' on the whole range) and is shown in
pieces. Tonnes are rounded to three places, as the factory's ``_tons`` does,
so a ratio of two tonnages reads the same here as on the factory's wall.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import formatting
from ..sections import SectionBuilder
from ..sources import FactoryQueryError, SAPConnectionError, SAPDataError
from ..sources.hana import LITRES_PER_UNIT
from .stacking import MEASURED_ON as STACKING_MEASURED_ON
from .stacking import pieces_per_pallet

logger = logging.getLogger(__name__)

#: How often the wall re-reads. Production moves in minutes, not seconds, and
#: every tile re-runs on each refresh, so this is also the SAP query budget.
REFRESH_SECONDS = 60

# ---------------------------------------------------------------------------
# Warehouses (the factory's plant_board/constants.py and packing_material's)
# ---------------------------------------------------------------------------

#: The production-finished floor: what the Production band reports on.
PRODUCTION_FLOOR = "BH-PF"

#: The packaging stores the benchmark, non-moving and stock-space tiles cover.
#: Named by the business; BH-NM is a store like any other here (leaving it out
#: did not make its stock disappear, it made the board understate it).
STORE_WAREHOUSES = ["BH-PM", "BH-BS", "BH-NM", "BH-PC"]

#: Where the line issues packing material from (``consumption_warehouses``).
CONSUMPTION_WAREHOUSES = ["BH-PC"]

#: The stores that FEED the floor: the stock stores less the consumption one.
#: What the requirement sheet's ``On hand`` counts (``supply_warehouses``).
SUPPLY_WAREHOUSES = [code for code in ("BH-PC", "BH-BS", "BH-PM", "BH-NM") if code not in CONSUMPTION_WAREHOUSES]

#: The floor the packaging stores stand on, as the business measured it. The
#: first block covers three warehouses as ONE space and cannot be split, so
#: the board totals the blocks and names them rather than inventing an area
#: per warehouse. "FR" was in the business's table and is excluded on their
#: instruction; no such warehouse exists in SAP.
PACKAGING_FLOOR_BLOCKS = [
    {"key": "shared", "label": "BH-PM, BH-NM, BH-BS", "warehouses": ["BH-PM", "BH-NM", "BH-BS"], "sqft": 16900},
    {"key": "pm", "label": "BH-PM", "warehouses": ["BH-PM"], "sqft": 15000},
    {"key": "pc", "label": "BH-PC", "warehouses": ["BH-PC"], "sqft": 7000},
]
PACKAGING_FLOOR_SQFT = sum(block["sqft"] for block in PACKAGING_FLOOR_BLOCKS)

#: The floor one pallet stands on, as the factory measured it. A default, not
#: a placeholder: the settings page can override it per company, and an
#: explicit null there (somebody cleared it) is honoured.
DEFAULT_SQFT_PER_PALLET = 15.0

# ---------------------------------------------------------------------------
# SAP codes
# ---------------------------------------------------------------------------

#: ``OITB.ItmsGrpCod`` 102 'FINISHED' and 105 'PACKAGING MATERIAL'.
FINISHED_ITEM_GROUP = 102
PACKAGING_ITEM_GROUP = 105
#: Matched on the NAME where the factory's own reads do (the benchmark and
#: the store stock). SAP's spelling, letter for letter: a near miss matches
#: nothing and the tile reads zero.
PACKAGING_GROUP_NAME = "PACKAGING MATERIAL"

#: ``OINM.TransType``: 59 production receipt, 60 goods issue, 67 transfer.
TRANS_TYPE_PRODUCTION_RECEIPT = 59
TRANS_TYPE_GOODS_ISSUE = 60
TRANS_TYPE_TRANSFER_IN = 67
#: Real outbound use for the benchmark's "recently used": delivery, goods
#: issue, production order.
CONSUMPTION_TRANS_TYPES = (15, 60, 202)
#: ``ITT1.Type`` 4 is an inventory item (290 a resource, never a material).
BOM_LINE_TYPE_ITEM = 4
BOM_TREE_TYPE_PRODUCTION = "P"

#: Item-group name tokens, the factory's ``classify_material``.
PACKAGING_TOKENS = ("PACKAG",)
RAW_TOKENS = ("RAW", "OIL")

# ---------------------------------------------------------------------------
# Board policy
# ---------------------------------------------------------------------------

#: Standing-age buckets on BH-PF, in days since stock last LEFT. Under the
#: first bound is fresh: the floor turns over about every 3.3 days.
AGE_BUCKET_LOWER = 4
AGE_BUCKET_UPPER = 7

#: Days of trailing detail a trend carries.
TREND_DAYS = 7
#: Rows any one list carries. The board is a wall, not a report.
MAX_LISTED_ROWS = 12
#: How many plan headers are read to find the current one.
PLAN_LIST_LIMIT = 24
#: Code lists are chunked for HANA, which takes a long ``IN`` badly.
CHUNK = 400

#: The Non-Moving dashboard's thresholds: over 45 idle days is non-moving,
#: 30-45 slow-moving, under 30 recently moved (and not counted).
NON_MOVING_SLOW_DAYS = 30
NON_MOVING_DEAD_DAYS = 45
#: The Stock Benchmark dashboard's "recently used" window.
SLOW_MOVING_DAYS = 30

# ---------------------------------------------------------------------------
# Shifting routes
# ---------------------------------------------------------------------------

SHIFTING_TO_BOTTLING = "BH-BT"
#: The retired Gupta code: no longer a route, kept so a load still posted to
#: it is named inside the Elsewhere row rather than anonymous.
SHIFTING_TO_GUPTA = "GP-FG"
SHIFTING_TO_GUPTA_MART = "GP-FGM"
#: Not a warehouse: an invoice-sourced BST is a cross-company SALE.
SHIFTING_DISPATCH = "DISPATCH"
SHIFTING_ELSEWHERE = "ELSEWHERE"

#: The routes both tiles read down, in a fixed order that is not a ranking.
SHIFTING_ROUTES = [SHIFTING_TO_BOTTLING, SHIFTING_DISPATCH]
#: The declaration tile has no standing dispatch row; a declared dispatch
#: folds into Elsewhere, which appears only when it carries something.
SHIFTING_DECLARED_ROUTES = [SHIFTING_TO_BOTTLING]

SHIFTING_ROUTE_NAMES = {
    SHIFTING_TO_BOTTLING: "Bhakharpur New Basement",
    SHIFTING_TO_GUPTA: "Gupta Godown Basement",
    SHIFTING_TO_GUPTA_MART: "Gupta Godown (Mart)",
    SHIFTING_DISPATCH: "Sold on to Mart",
    SHIFTING_ELSEWHERE: "Other destinations",
}

# ---------------------------------------------------------------------------
# Workforce
# ---------------------------------------------------------------------------

#: The plant's departments and the band each reports under. A statement about
#: the factory, so it lives in code; the head count and wage bill are typed
#: on the factory's settings page. Purchase has no department on purpose.
WORKFORCE_DEPARTMENTS = [
    {"key": "oil_production_company", "label": "Oil Production Company", "band": "production", "kind": "employee"},
    {"key": "oil_production_outside", "label": "Oil Production Outside", "band": "production", "kind": "labour"},
    {"key": "bottle_blowing_company", "label": "Bottle Blowing Company", "band": "store", "kind": "employee"},
    {"key": "bottle_blowing_outside", "label": "Bottle Blowing Outside", "band": "store", "kind": "labour"},
    {"key": "pm_warehouse", "label": "Pm Warehouse", "band": "store", "kind": "employee"},
    {"key": "fg_shifting", "label": "Fg Shifting", "band": "shifting", "kind": "employee"},
]
WORKFORCE_BANDS = ["purchase", "store", "production", "shifting"]
WORKFORCE_KINDS = ["employee", "labour"]
BUILT_IN_DEPARTMENTS = {entry["key"]: entry for entry in WORKFORCE_DEPARTMENTS}

#: The sections, in build order (the factory's band order, tile by tile). The
#: order matters beyond tidiness: the header's Check chip shows the FIRST
#: warning, and the factory raises them in this order.
SECTIONS = (
    "plan",
    "purchase_plan",
    "purchased",
    "benchmark",
    "open_pos",
    "stock_space",
    "non_moving",
    "pm_in",
    "blowing",
    "today_lines",
    "monthly_planning",
    "floor_stock",
    "wastage",
    "declared",
    "shipped",
    "workforce",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _f(value: Any) -> float:
    return formatting.f(value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _tons(litres: Any) -> float:
    """Litres to tonnes on the business's rule, to three places like the
    factory's ``_tons``, so ratios of two tonnages agree with its wall."""
    return round(_f(litres) / formatting.LITRES_PER_TON, 3)


def _per_day(monthly: Optional[float], days: int) -> Optional[float]:
    """A month's wage bill as a daily run rate, or nothing at all."""
    if monthly is None or not days:
        return None
    return round(monthly / days, 2)


def _chunks(values: Sequence[Any], size: int = CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _placeholders(count: int) -> str:
    return ", ".join(["?"] * count)


def classify_material(item_group: Optional[str]) -> str:
    """PACKAGING / RAW / OTHER from the SAP item group name (the factory's
    ``planning_purchase.classify_material``), so a material that reads as
    packaging on the requirement sheet cannot read as raw material here."""
    group = (item_group or "").upper()
    if any(token in group for token in PACKAGING_TOKENS):
        return "PACKAGING"
    if any(token in group for token in RAW_TOKENS):
        return "RAW"
    return "OTHER"


def movement_status(days: int) -> str:
    """``non-moving``, ``slow-moving`` or ``recent``, on the Non-Moving
    dashboard's thresholds."""
    if days > NON_MOVING_DEAD_DAYS:
        return "non-moving"
    if days >= NON_MOVING_SLOW_DAYS:
        return "slow-moving"
    return "recent"


def marker(name: str) -> str:
    """The line every statement opens with. It names the read in
    ``pg_stat_activity`` / HANA's plan cache, and it is what the tests'
    fakes match on."""
    return f"-- plant: {name}\n"


class _Failed:
    """A shared read that failed, remembered so the second tile asking for it
    reports the same reason instead of paying for it again."""

    def __init__(self, error: BaseException):
        self.error = error


class PlantBoardService(SectionBuilder):
    """The whole plant board, for one company, in one read."""

    REFRESH_SECONDS = REFRESH_SECONDS

    def __init__(
        self,
        company_code: str,
        *,
        postgres,
        hana_for: Callable[[str], Any],
        today: Optional[date] = None,
    ):
        """Nothing is read here. The company row, the plan and every shared
        read are resolved inside the first section that needs them, so a
        source that is down degrades sections with its reason instead of
        breaking the constructor."""
        self.company_code = company_code
        self.postgres = postgres
        self.hana = hana_for(company_code)
        self.today = today or formatting.today()
        self._shared: Dict[str, Any] = {}
        self._litre_cache: Dict[str, float] = {}
        self._company_id: Optional[int] = None
        self._plan: Optional[Dict[str, Any]] = None
        self._plan_read = False
        self._init_sections()

    # ------------------------------------------------------------------ build

    def build(self) -> Dict[str, Any]:
        """Every tile, each absent-able on its own."""
        sap = {"needs_factory": False}
        factory = {"needs_sap": False}

        payload: Dict[str, Any] = {}
        payload["plan"] = self.section("plan", self._resolve_plan, **sap)
        self._plan = payload["plan"]
        self._plan_read = "plan" not in self._degraded

        # Band 1: Purchase. SAP end to end: the plan's packing requirement,
        # this month's purchase orders, the benchmark, the open-order book.
        payload["purchase_plan"] = self.section("purchase_plan", self._purchase_plan, **sap)
        payload["purchased"] = self.section("purchased", self._purchased, **sap)
        payload["benchmark"] = self.section("benchmark", self._benchmark, **sap)
        payload["open_pos"] = self.section("open_pos", self._open_pos, **sap)

        # Band 2: Store. The building's figures are typed on the factory's
        # settings page, the stock is SAP's, the trucks and the blowing line
        # are the factory database's own.
        payload["stock_space"] = self.section("stock_space", self._stock_space)
        payload["non_moving"] = self.section("non_moving", self._non_moving, **sap)
        payload["pm_in"] = self.section("pm_in", self._pm_in, **factory)
        payload["blowing"] = self.section("blowing", self._blowing, **factory)

        # Band 3: Production. Today off the lines' register; the month, the
        # floor and its age off SAP's journal; waste off the register, split
        # by SAP's item groups.
        payload["today_lines"] = self.section("today_lines", self._today_on_the_lines, **factory)
        payload["monthly_planning"] = self.section("monthly_planning", self._monthly_planning, **sap)
        payload["floor_stock"] = self.section("floor_stock", self._floor_stock, **sap)
        payload["wastage"] = self.section("wastage", self._wastage)

        # Band 4: Shifting. Its registers are the factory database's; only
        # the shipped tonnage asks SAP, and losing SAP costs it the unit, not
        # the tile.
        payload["declared"] = self.section("declared", self._declared, **factory)
        payload["shipped"] = self.section("shipped", self._shipped, **factory)

        # Typed on the factory's settings page, so it survives anything SAP does.
        payload["workforce"] = self.section("workforce", self._workforce, **factory)

        payload["meta"] = {
            "company": self.company_code,
            "company_code": self.company_code,
            "date": self.today.isoformat(),
            "as_of": self.today.isoformat(),
            "plan": payload["plan"],
            "refresh_seconds": self.REFRESH_SECONDS,
            "generated_at": formatting.now().isoformat(),
            "tonnage_basis": (
                f"{formatting.LITRES_PER_TON} litres = 1 ton, applied to oil and finished goods "
                "only. Packing material carries no litre volume in SAP and is shown in pieces."
            ),
            **self.section_meta(),
        }
        return payload

    # --------------------------------------------------------------- helpers

    def _once(self, key: str, read: Callable[[], Any]) -> Any:
        """A read shared by several tiles, made once per build. A failure is
        kept and raised again, so every tile that needed it says the same
        thing and only the first pays for it."""
        if key not in self._shared:
            try:
                self._shared[key] = read()
            except Exception as exc:  # noqa: BLE001 - re-raised to each section
                self._shared[key] = _Failed(exc)
        held = self._shared[key]
        if isinstance(held, _Failed):
            raise held.error
        return held

    def _tz(self) -> str:
        return formatting.tz().key

    def _company(self) -> int:
        """The factory's id for this company. Looked up once; an unknown code
        is a reason on every factory tile rather than a board of zeros."""
        if self._company_id is None:
            row = self.postgres.one(marker("company") + "SELECT id FROM company_company WHERE code = %s", [self.company_code])
            if not row:
                raise LookupError(f"No company with code {self.company_code!r} in the factory database.")
            self._company_id = int(row["id"])
        return self._company_id

    def _hana_rows(self, name: str, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        return self.hana.rows(marker(name) + sql, list(params or []))

    # ------------------------------------------------------------------ plan

    def _resolve_plan(self) -> Optional[Dict[str, Any]]:
        """The plan month the board reports against.

        The business fixed the window as the SAP plan month rather than the
        calendar month, so the plan header IS the period. The current plan is
        the newest whose dates contain today; failing that the newest is used
        and a warning says so. ``None`` is SAP holding no plan at all, which
        is an answer, not a failure.
        """
        t = self.hana.table
        rows = self._hana_rows(
            "plans",
            f"""SELECT H."AbsID", H."Code", H."Name", H."StartDate", H."EndDate"
FROM {t("OFCT")} H
ORDER BY H."AbsID" DESC
LIMIT {int(PLAN_LIST_LIMIT)}""",
        )
        if not rows:
            return None

        def is_current(row: Dict[str, Any]) -> bool:
            start = formatting.as_date(row.get("StartDate"))
            end = formatting.as_date(row.get("EndDate"))
            return bool(start and end and start <= self.today <= end)

        current = next((row for row in rows if is_current(row)), None)
        if current is None:
            current = rows[0]
            self.warn(
                "No SAP plan covers today; showing the most recent plan "
                f"({current.get('Name') or current.get('Code')})."
            )

        start = formatting.as_date(current.get("StartDate"))
        end = formatting.as_date(current.get("EndDate"))
        elapsed_end = min(self.today, end) if end else self.today
        return {
            "abs_id": current.get("AbsID"),
            "code": current.get("Code") or "",
            "name": current.get("Name") or "",
            "start_date": formatting.iso(start),
            "end_date": formatting.iso(end),
            "is_current": is_current(current),
            # Plan-to-date, which is what "so far" is measured over.
            "days_total": (end - start).days + 1 if (start and end) else None,
            "days_elapsed": (elapsed_end - start).days + 1 if start else None,
        }

    def _require_plan(self) -> Dict[str, Any]:
        """The plan, or the reason a tile measured against it cannot be read."""
        if not self._plan_read:
            raise LookupError("The SAP plan could not be read, and this tile is measured against it.")
        if not self._plan or not self._plan.get("abs_id"):
            raise LookupError("SAP holds no production plan for this company.")
        return self._plan

    def _plan_window(self) -> Tuple[date, date]:
        """Plan start through today, or the calendar month without a plan."""
        plan = self._plan
        if plan and plan.get("start_date"):
            start = date.fromisoformat(plan["start_date"])
            end = date.fromisoformat(plan["end_date"]) if plan.get("end_date") else self.today
            return start, min(self.today, end)
        return self.today.replace(day=1), self.today

    def _issue_window(self, plan: Dict[str, Any]) -> Tuple[date, date]:
        """The requirement sheet's window: the plan's first day to the earlier
        of today and its last day (``packing_material.issue_window``)."""
        start = formatting.as_date(plan.get("start_date")) or self.today
        end = formatting.as_date(plan.get("end_date")) or self.today
        return start, min(end, self.today)

    # ----------------------------------------------- the packing requirement

    def _requirement_lines(self) -> List[Dict[str, Any]]:
        """Packing material the plan requires, per component: the BOM
        explosion, priced at the item master.

        The plan lines are summed per item FIRST (a weekly plan carries one
        line per bucket and would otherwise multiply the component out), and
        ``ITT1.Quantity`` is divided by ``OITT.Qauntity`` (SAP's spelling), the
        batch the recipe is written for. The unit price is the requirement
        sheet's: the moving average, else the last purchase price, both in the
        INVENTORY unit, read in the same statement rather than as a second
        read of the whole packaging master.
        """

        def read() -> List[Dict[str, Any]]:
            plan = self._require_plan()
            t = self.hana.table
            rows = self._hana_rows(
                "requirement",
                f"""SELECT
    C."Code" AS "ItemCode",
    ROUND(SUM(P."PlanQty" * C."Quantity" / NULLIF(T."Qauntity", 0)), 3) AS "PlanningQty",
    COUNT(DISTINCT P."ItemCode") AS "SkuCount",
    MAX(CASE WHEN COALESCE(CI."AvgPrice", 0) <> 0 THEN CI."AvgPrice"
             ELSE COALESCE(CI."LastPurPrc", 0) END) AS "UnitPrice"
FROM (
    SELECT L."ItemCode", SUM(L."Quantity") AS "PlanQty"
    FROM {t("FCT1")} L
    WHERE L."AbsID" = ?
    GROUP BY L."ItemCode"
) P
INNER JOIN {t("OITT")} T
    ON T."Code" = P."ItemCode" AND T."TreeType" = '{BOM_TREE_TYPE_PRODUCTION}'
INNER JOIN {t("ITT1")} C
    ON C."Father" = T."Code" AND C."Type" = {BOM_LINE_TYPE_ITEM}
INNER JOIN {t("OITM")} CI
    ON CI."ItemCode" = C."Code"
WHERE CI."ItmsGrpCod" = {PACKAGING_ITEM_GROUP}
  AND C."Quantity" / NULLIF(T."Qauntity", 0) IS NOT NULL
GROUP BY C."Code"
""",
                [int(plan["abs_id"])],
            )
            return [
                {
                    "item_code": row.get("ItemCode") or "",
                    "planning_qty": _f(row.get("PlanningQty")),
                    "sku_count": _int(row.get("SkuCount")),
                    "unit_price": _f(row.get("UnitPrice")),
                }
                for row in rows
                if row.get("ItemCode")
            ]

        return self._once("requirement_lines", read)

    def _requirement(self) -> Dict[str, Any]:
        """The requirement sheet's rows and totals, for the plan's items.

        One row per component: planning, what reached the floor (received
        into BH-PC over the plan-to-date), what the feeding stores hold, and
        what is open on order. Nothing is floored at zero; ``req`` goes
        negative for a shortage, which is the point of the sheet. Rows and
        totals keep the sheet's own rounding, so a rupee figure here is the
        figure on that page.
        """

        def read() -> Dict[str, Any]:
            plan = self._require_plan()
            lines = self._requirement_lines()
            window_from, window_to = self._issue_window(plan)
            t = self.hana.table

            received = {
                row.get("ItemCode"): _f(row.get("ReceivedQty"))
                for row in self._hana_rows(
                    "pm_received",
                    f"""SELECT O."ItemCode", ROUND(COALESCE(SUM(O."InQty"), 0), 3) AS "ReceivedQty"
FROM {t("OINM")} O
INNER JOIN {t("OITM")} I ON I."ItemCode" = O."ItemCode"
WHERE I."ItmsGrpCod" = {PACKAGING_ITEM_GROUP}
  AND O."DocDate" >= ?
  AND O."DocDate" <= ?
  AND O."Warehouse" IN ({_placeholders(len(CONSUMPTION_WAREHOUSES))})
GROUP BY O."ItemCode"
HAVING ROUND(COALESCE(SUM(O."InQty"), 0), 3) > 0""",
                    [window_from, window_to, *CONSUMPTION_WAREHOUSES],
                )
            }

            on_hand = {
                row.get("ItemCode"): _f(row.get("OnHand"))
                for row in self._hana_rows(
                    "pm_on_hand",
                    f"""SELECT W."ItemCode", ROUND(COALESCE(SUM(W."OnHand"), 0), 3) AS "OnHand"
FROM {t("OITW")} W
INNER JOIN {t("OITM")} M ON M."ItemCode" = W."ItemCode"
WHERE M."ItmsGrpCod" = {PACKAGING_ITEM_GROUP}
  AND W."WhsCode" IN ({_placeholders(len(SUPPLY_WAREHOUSES))})
GROUP BY W."ItemCode"
""",
                    SUPPLY_WAREHOUSES,
                )
            }

            # Open whenever it was raised and wherever it is due. Both the
            # header and the line must be open: a line can be closed by hand
            # on an order that is not.
            open_po = {
                row.get("ItemCode"): row
                for row in self._hana_rows(
                    "pm_open_po",
                    f"""SELECT L."ItemCode",
       ROUND(COALESCE(SUM(L."OpenQty"), 0), 3) AS "OpenQty",
       MIN(L."ShipDate") AS "EarliestDue",
       COUNT(*) AS "OpenLines"
FROM {t("OPOR")} H
INNER JOIN {t("POR1")} L ON L."DocEntry" = H."DocEntry"
INNER JOIN {t("OITM")} M ON M."ItemCode" = L."ItemCode"
WHERE H."DocStatus" = 'O'
  AND L."LineStatus" = 'O'
  AND M."ItmsGrpCod" = {PACKAGING_ITEM_GROUP}
GROUP BY L."ItemCode"
HAVING ROUND(COALESCE(SUM(L."OpenQty"), 0), 3) <> 0""",
                )
            }

            rows: List[Dict[str, Any]] = []
            for line in lines:
                code = line["item_code"]
                planning = line["planning_qty"]
                issued = received.get(code, 0.0)
                held = on_hand.get(code, 0.0)
                req = held - (planning - issued)
                po = open_po.get(code) or {}
                open_qty = _f(po.get("OpenQty"))
                earliest_due = formatting.as_date(po.get("EarliestDue"))
                rows.append(
                    {
                        "item_code": code,
                        "unit_price": line["unit_price"],
                        "planning_qty": round(planning, 3),
                        "issued_pc_qty": round(issued, 3),
                        "on_hand_qty": round(held, 3),
                        "req_qty": round(req, 3),
                        "open_po_qty": round(open_qty, 3),
                        "po_lines": _int(po.get("OpenLines")),
                        "req_after_po_qty": round(req + open_qty, 3),
                        # Open and its due date already gone by. On Oil every
                        # open packing line was past due on 9 September 2026.
                        "po_overdue": bool(open_qty and earliest_due and earliest_due < self.today),
                    }
                )

            def value(key: str) -> float:
                """Per row, then summed: prices differ by three orders of
                magnitude, so one blended rate would mean nothing."""
                return round(sum(row[key] * row["unit_price"] for row in rows), 2)

            totals = {
                "item_count": len(rows),
                "planning_qty": round(sum(row["planning_qty"] for row in rows), 3),
                "planning_value": value("planning_qty"),
                "issued_pc_qty": round(sum(row["issued_pc_qty"] for row in rows), 3),
                "issued_value": value("issued_pc_qty"),
                "on_hand_qty": round(sum(row["on_hand_qty"] for row in rows), 3),
                "on_hand_value": value("on_hand_qty"),
                "open_po_qty": round(sum(row["open_po_qty"] for row in rows), 3),
                "open_po_value": value("open_po_qty"),
                # Components the item master holds no price for: they add
                # nothing to the rupees above, so each one understates them.
                "unpriced_count": sum(1 for row in rows if not row["unit_price"]),
                # Short rows leaning on an order that is already late.
                "po_overdue_count": sum(1 for row in rows if row["po_overdue"] and row["req_qty"] < 0),
                "short_after_po_count": sum(1 for row in rows if row["req_after_po_qty"] < 0),
                "short_after_po_value": round(
                    sum(max(0.0, -row["req_after_po_qty"]) * row["unit_price"] for row in rows), 2
                ),
            }
            return {
                "rows": rows,
                "totals": totals,
                "window_from": window_from.isoformat(),
                "window_to": window_to.isoformat(),
            }

        return self._once("requirement", read)

    # ------------------------------------------------- band 1: purchase tiles

    def _purchase_plan(self) -> Dict[str, Any]:
        """Plan this month: the packing material the plan needs, priced, in
        the three states it can be in. Never tons: no packaging item is a
        litre item."""
        sheet = self._requirement()
        totals = sheet["totals"]
        planning = totals["planning_value"]
        issued = totals["issued_value"]
        on_hand = totals["on_hand_value"]
        return {
            "planning_qty": totals["planning_qty"],
            "planning_unit": "PCS",
            "planning_value": planning,
            "issued_value": issued,
            "on_hand_value": on_hand,
            # What still has to be BOUGHT: the plan less what the floor has
            # already drawn and what the stores hold against it.
            "to_purchase_value": round(max(0.0, planning - issued - on_hand), 2),
            "unpriced_count": totals["unpriced_count"],
            "reached_floor_qty": totals["issued_pc_qty"],
            "on_hand_qty": totals["on_hand_qty"],
            "short_count": totals["short_after_po_count"],
            "short_value": totals["short_after_po_value"],
            "item_count": totals["item_count"],
            "window_from": sheet["window_from"],
            "window_to": sheet["window_to"],
            "stores": STORE_WAREHOUSES,
        }

    def _purchased(self) -> Dict[str, Any]:
        """Purchase orders RAISED plan-to-date on the plan's own packing items.

        Three figures off the same PO lines, so they tie: ordered = received
        + still open. Received is SAP's ``Quantity - OpenQty``, so a line
        closed by hand reads as received; the closed-line count rides along.
        Scoped to the plan's items: preform for the blowing line and packs
        for SKUs the plan does not schedule are real buying, but not this
        plan's. (An empty item list reads every packaging PO, as the
        factory's reader does.)
        """
        self._require_plan()
        window_from, window_to = self._plan_window()
        codes = sorted({line["item_code"] for line in self._requirement_lines()})
        t = self.hana.table

        rows: List[Dict[str, Any]] = []
        for chunk in list(_chunks(codes)) or [None]:
            where = ""
            params: List[Any] = [window_from, window_to]
            if chunk:
                where = f'\n  AND L."ItemCode" IN ({_placeholders(len(chunk))})'
                params.extend(chunk)
            rows.extend(
                self._hana_rows(
                    "purchase_orders",
                    f"""SELECT
    H."DocEntry" AS "DocEntry",
    L."ItemCode" AS "ItemCode",
    COUNT(*) AS "Lines",
    ROUND(COALESCE(SUM(L."Quantity"), 0), 3) AS "OrderedQty",
    ROUND(COALESCE(SUM(L."OpenQty"), 0), 3) AS "OpenQty",
    ROUND(COALESCE(SUM(L."Quantity" * L."Price"), 0), 2) AS "OrderedValue",
    ROUND(COALESCE(SUM(L."OpenQty" * L."Price"), 0), 2) AS "OpenValue",
    SUM(CASE WHEN L."LineStatus" <> 'O' THEN 1 ELSE 0 END) AS "ClosedLines"
FROM {t("OPOR")} H
INNER JOIN {t("POR1")} L ON L."DocEntry" = H."DocEntry"
INNER JOIN {t("OITM")} M ON M."ItemCode" = L."ItemCode"
WHERE M."ItmsGrpCod" = {PACKAGING_ITEM_GROUP}
  AND H."DocDate" >= ?
  AND H."DocDate" <= ?{where}
GROUP BY H."DocEntry", L."ItemCode"
""",
                    params,
                )
            )

        orders = {row.get("DocEntry") for row in rows}
        ordered_qty = sum(_f(row.get("OrderedQty")) for row in rows)
        open_qty = sum(_f(row.get("OpenQty")) for row in rows)
        ordered_value = sum(_f(row.get("OrderedValue")) for row in rows)
        open_value = sum(_f(row.get("OpenValue")) for row in rows)
        return {
            "po_count": len(orders),
            "po_lines": sum(_int(row.get("Lines")) for row in rows),
            "ordered_qty": round(ordered_qty, 3),
            "po_open_qty": round(open_qty, 3),
            # Never negative: a store cannot receive a negative amount.
            "po_received_qty": round(max(0.0, ordered_qty - open_qty), 3),
            "ordered_value": round(ordered_value, 2),
            "po_open_value": round(open_value, 2),
            "po_received_value": round(max(0.0, ordered_value - open_value), 2),
            "po_closed_lines": sum(_int(row.get("ClosedLines")) for row in rows),
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
        }

    def _benchmark(self) -> Dict[str, Any]:
        """The Stock Benchmark dashboard's own count, for the same stores.

        THE FILTER IS THE POINT: the four stores, PACKAGING MATERIAL by name,
        recently used (a consumption in the last 30 days) and a benchmark set.
        That is what that page opens with, so somebody reading this tile who
        then opens it finds the same count. Grouped across the stores per
        item, as the page does whenever two or more stores are selected.
        """
        t = self.hana.table
        consumption = ", ".join(str(code) for code in CONSUMPTION_TRANS_TYPES)
        row = (
            self._hana_rows(
                "benchmark",
                f"""SELECT
    COUNT(*) AS "Total",
    SUM(CASE WHEN g.min_stock > 0 AND g.on_hand >= g.min_stock THEN 1 ELSE 0 END) AS "Healthy",
    SUM(CASE WHEN g.min_stock > 0 AND g.on_hand < g.min_stock
              AND g.on_hand >= g.min_stock * 0.6 THEN 1 ELSE 0 END) AS "Low",
    SUM(CASE WHEN g.min_stock > 0 AND g.on_hand < g.min_stock * 0.6 THEN 1 ELSE 0 END) AS "Critical"
FROM (
    SELECT
        SUM(w."OnHand") AS on_hand,
        SUM(w."MinStock") AS min_stock,
        MIN(CASE WHEN mov."LastConsumptionDate" IS NULL THEN NULL
                 ELSE DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE) END) AS days_since
    FROM {t("OITW")} w
    JOIN {t("OITM")} m ON w."ItemCode" = m."ItemCode"
    LEFT JOIN {t("OITB")} grp ON m."ItmsGrpCod" = grp."ItmsGrpCod"
    LEFT JOIN (
        SELECT n."ItemCode", MAX(n."DocDate") AS "LastConsumptionDate"
        FROM {t("OINM")} n
        WHERE n."OutQty" > 0 AND n."TransType" IN ({consumption})
        GROUP BY n."ItemCode"
    ) mov ON mov."ItemCode" = w."ItemCode"
    WHERE w."WhsCode" IN ({_placeholders(len(STORE_WAREHOUSES))})
      AND UPPER(IFNULL(grp."ItmsGrpNam", '')) = UPPER(?)
    GROUP BY w."ItemCode"
) g
WHERE g.min_stock > 0
  AND g.days_since IS NOT NULL
  AND g.days_since <= {SLOW_MOVING_DAYS}""",
                [*STORE_WAREHOUSES, PACKAGING_GROUP_NAME],
            )
            or [{}]
        )[0]

        healthy = _int(row.get("Healthy"))
        low = _int(row.get("Low"))
        critical = _int(row.get("Critical"))
        return {
            # The page's own "Total Items": the three statuses added.
            "sku_count": healthy + low + critical,
            "healthy_count": healthy,
            "below_benchmark_count": low + critical,
            "low_count": low,
            "critical_count": critical,
            "stores": STORE_WAREHOUSES,
            "benchmark_basis": (
                f"Stock Benchmark filters: {', '.join(STORE_WAREHOUSES)} · {PACKAGING_GROUP_NAME} · "
                "recently used · benchmark set."
            ),
        }

    def _open_pos(self) -> Dict[str, Any]:
        """What is still coming: the requirement sheet's open book on the
        plan's items, split by the age of the order behind it.

        The split is applied as a PROPORTION of the sheet's own open quantity
        rather than summed from SAP directly, so the two halves add back to
        the headline by construction. An item SAP reports no open line for
        keeps its whole quantity in the older half rather than vanishing.
        """
        sheet = self._requirement()
        rows = sheet["rows"]
        totals = sheet["totals"]
        window_from, _ = self._plan_window()
        codes = sorted({row["item_code"] for row in rows})
        t = self.hana.table
        cutoff = window_from.isoformat()

        by_item: Dict[str, Dict[str, float]] = {}
        for chunk in list(_chunks(codes)) or [None]:
            where = ""
            params: List[Any] = []
            if chunk:
                where = f'\n  AND L."ItemCode" IN ({_placeholders(len(chunk))})'
                params.extend(chunk)
            for row in self._hana_rows(
                "open_po_age",
                f"""SELECT
    L."ItemCode" AS "ItemCode",
    H."DocDate" AS "DocDate",
    COUNT(*) AS "Lines",
    ROUND(COALESCE(SUM(L."OpenQty"), 0), 3) AS "OpenQty"
FROM {t("OPOR")} H
INNER JOIN {t("POR1")} L ON L."DocEntry" = H."DocEntry"
INNER JOIN {t("OITM")} M ON M."ItemCode" = L."ItemCode"
WHERE M."ItmsGrpCod" = {PACKAGING_ITEM_GROUP}
  AND L."LineStatus" = 'O'
  AND L."OpenQty" > 0{where}
GROUP BY L."ItemCode", H."DocDate"
""",
                params,
            ):
                code = row.get("ItemCode") or ""
                if not code:
                    continue
                recent = (formatting.iso(formatting.as_date(row.get("DocDate"))) or "") >= cutoff
                held = by_item.setdefault(code, {"recent_qty": 0.0, "older_qty": 0.0, "recent_lines": 0, "older_lines": 0})
                held["recent_qty" if recent else "older_qty"] += _f(row.get("OpenQty"))
                held["recent_lines" if recent else "older_lines"] += _int(row.get("Lines"))

        recent_qty = recent_value = older_qty = older_value = 0.0
        recent_lines = older_lines = 0
        for row in rows:
            open_qty = row["open_po_qty"]
            if open_qty <= 0:
                continue
            age = by_item.get(row["item_code"]) or {}
            recent_lines += int(age.get("recent_lines") or 0)
            older_lines += int(age.get("older_lines") or 0)
            known = _f(age.get("recent_qty")) + _f(age.get("older_qty"))
            share = _f(age.get("recent_qty")) / known if known > 0 else 0.0
            recent = open_qty * share
            older = open_qty - recent
            recent_qty += recent
            older_qty += older
            recent_value += recent * row["unit_price"]
            older_value += older * row["unit_price"]

        return {
            "open_po_qty": totals["open_po_qty"],
            "open_po_value": totals["open_po_value"],
            # How many of the plan's items have anything on order at all.
            "open_po_count": sum(1 for row in rows if row["open_po_qty"] > 0),
            # Counted only where the row is short: an over-issued item needs
            # nothing, however overdue its order.
            "open_po_overdue_count": totals["po_overdue_count"],
            "open_po_recent_qty": round(recent_qty, 3),
            "open_po_recent_value": round(recent_value, 2),
            "open_po_recent_lines": recent_lines,
            "open_po_older_qty": round(older_qty, 3),
            "open_po_older_value": round(older_value, 2),
            "open_po_older_lines": older_lines,
            "raised_from": cutoff,
        }

    # ---------------------------------------------------- band 2: store tiles

    def _stock_space(self) -> Dict[str, Any]:
        """Rated capacity and the last physical count per store (typed on the
        factory's settings page, because SAP holds neither), and the floor the
        stores have against the stock standing on it.

        The audit date reported is the OLDEST of the configured stores: the
        point of the figure is to find the store nobody has been to. Capacity
        only totals when every store has one; a partial total would make the
        stores look fuller than they are.
        """
        settings_rows = {
            row.get("warehouse"): row
            for row in self.postgres.rows(
                marker("store_settings")
                + """SELECT warehouse, capacity_tonnes, last_audit_date
FROM stock_dashboard_warehouseboardsettings
WHERE company_code = %s AND warehouse = ANY(%s)
ORDER BY id""",
                [self.company_code, list(STORE_WAREHOUSES)],
            )
        }

        stores: List[Dict[str, Any]] = []
        capacities: List[float] = []
        audited: List[Tuple[str, date]] = []
        for code in STORE_WAREHOUSES:
            row = settings_rows.get(code) or {}
            capacity = formatting.f_or_none(row.get("capacity_tonnes"))
            audit = formatting.as_date(row.get("last_audit_date"))
            if capacity is not None:
                capacities.append(capacity)
            if audit is not None:
                audited.append((code, audit))
            stores.append(
                {
                    "warehouse": code,
                    "capacity_tonnes": capacity,
                    "last_audit_date": formatting.iso(audit),
                    "audit_days_ago": (self.today - audit).days if audit else None,
                }
            )

        oldest = min(audited, key=lambda item: item[1]) if audited else None
        return {
            "stores": stores,
            "area": self._floor_area(),
            "capacity_tonnes": round(sum(capacities), 2) if len(capacities) == len(STORE_WAREHOUSES) else None,
            "capacity_configured": len(capacities),
            "store_count": len(STORE_WAREHOUSES),
            "last_audit_date": oldest[1].isoformat() if oldest else None,
            "last_audit_warehouse": oldest[0] if oldest else "",
            "audit_days_ago": (self.today - oldest[1]).days if oldest else None,
            "audit_configured": len(audited),
        }

    def _sqft_per_pallet(self) -> Optional[float]:
        """The floor one pallet stands on: the measured default unless this
        company has said otherwise. An explicit null means somebody cleared
        it and is honoured. A settings table the factory has not migrated yet
        is tolerated: the setting is not what this tile is about."""
        try:
            row = self.postgres.one(
                marker("plant_settings")
                + "SELECT sqft_per_pallet FROM stock_dashboard_plantboardsettings WHERE company_code = %s",
                [self.company_code],
            )
        except FactoryQueryError as exc:
            logger.warning("Plant board: space settings unreadable: %s", exc)
            return DEFAULT_SQFT_PER_PALLET
        if row is None:
            return DEFAULT_SQFT_PER_PALLET
        return formatting.f_or_none(row.get("sqft_per_pallet"))

    def _floor_area(self) -> Dict[str, Any]:
        """Pieces -> pallets -> square feet, in two measured steps.

        SAP holds no volume or dimensions for packaging, so the factory
        bridges it: pieces per pallet PER ITEM from its stacking sheet (300
        five-litre bottles fill a pallet, 60,000 caps fill one, so a blended
        rate would be dominated by the caps), and the floor one pallet takes.
        An item with no pallet figure is counted in the pieces and left out
        of the floor, and named: every one makes the stores read emptier.
        """
        t = self.hana.table
        rows = self._hana_rows(
            "packaging_stock",
            f"""SELECT
    W."WhsCode" AS "Warehouse",
    W."ItemCode" AS "ItemCode",
    SUM(W."OnHand") AS "Pieces",
    SUM(W."OnHand" * IFNULL(M."LastPurPrc", 0)) AS "Value",
    MAX(CASE WHEN IFNULL(M."LastPurPrc", 0) = 0 THEN 1 ELSE 0 END) AS "Unpriced"
FROM {t("OITW")} W
JOIN {t("OITM")} M ON M."ItemCode" = W."ItemCode"
JOIN {t("OITB")} G ON G."ItmsGrpCod" = M."ItmsGrpCod"
WHERE G."ItmsGrpNam" = ?
  AND W."OnHand" > 0
  AND W."WhsCode" IN ({_placeholders(len(STORE_WAREHOUSES))})
GROUP BY W."WhsCode", W."ItemCode"
""",
            [PACKAGING_GROUP_NAME, *STORE_WAREHOUSES],
        )
        per_pallet = pieces_per_pallet()

        held: Dict[str, Dict[str, float]] = {}
        unmeasured_codes: set = set()
        unmeasured_pieces = 0.0
        for row in rows:
            slot = held.setdefault(
                row.get("Warehouse") or "",
                {"pieces": 0.0, "value": 0.0, "items": 0, "unpriced_items": 0, "pallets": 0.0},
            )
            pieces = _f(row.get("Pieces"))
            slot["pieces"] += pieces
            slot["value"] += _f(row.get("Value"))
            slot["items"] += 1
            slot["unpriced_items"] += 1 if row.get("Unpriced") else 0
            factor = per_pallet.get(row.get("ItemCode") or "")
            if factor:
                slot["pallets"] += pieces / factor
            else:
                unmeasured_codes.add(row.get("ItemCode") or "")
                unmeasured_pieces += pieces

        pieces = sum(slot["pieces"] for slot in held.values())
        value = sum(slot["value"] for slot in held.values())
        unpriced = sum(slot["unpriced_items"] for slot in held.values())
        if unpriced:
            self.warn(
                f"{unpriced} packaging items in the stores carry no purchase price, "
                "so the stock value understates what is held."
            )
        pallets = sum(slot["pallets"] for slot in held.values())

        sqft_per_pallet = self._sqft_per_pallet()
        occupied = free = occupied_pct = None
        if sqft_per_pallet:
            occupied = round(pallets * sqft_per_pallet, 1)
            # Free space floors at zero; the percentage is left uncapped so an
            # overfill is still visible.
            free = round(max(0.0, PACKAGING_FLOOR_SQFT - occupied), 1)
            occupied_pct = round(occupied / PACKAGING_FLOOR_SQFT * 100, 1)

        if unmeasured_codes:
            self.warn(
                f"{len(unmeasured_codes)} packaging items have no pallet figure, "
                "so the floor in use is understated."
            )

        return {
            "sqft": PACKAGING_FLOOR_SQFT,
            "blocks": [dict(block, warehouses=list(block["warehouses"])) for block in PACKAGING_FLOOR_BLOCKS],
            "store_count": len(STORE_WAREHOUSES),
            "held_pieces": round(pieces, 2),
            "held_value": round(value, 2),
            "held_items": sum(slot["items"] for slot in held.values()),
            "unpriced_items": unpriced,
            "pallets": round(pallets, 1),
            "sqft_per_pallet": sqft_per_pallet,
            "stacking_measured_on": STACKING_MEASURED_ON,
            "occupied_sqft": occupied,
            "free_sqft": free,
            "occupied_pct": occupied_pct,
            "unmeasured_items": len(unmeasured_codes),
            "unmeasured_pieces": round(unmeasured_pieces, 2),
            "by_store": [
                {
                    "warehouse": code,
                    "pieces": round(held.get(code, {}).get("pieces", 0.0), 2),
                    "value": round(held.get(code, {}).get("value", 0.0), 2),
                    "items": held.get(code, {}).get("items", 0),
                    "pallets": round(held.get(code, {}).get("pallets", 0.0), 1),
                    "occupied_sqft": (
                        round(held.get(code, {}).get("pallets", 0.0) * sqft_per_pallet, 1) if sqft_per_pallet else None
                    ),
                }
                for code in STORE_WAREHOUSES
            ],
        }

    def _non_moving(self) -> Dict[str, Any]:
        """Idle packaging in the stores, as the Non-Moving dashboard counts it.

        Packing material is aged on PRODUCTION alone (an issue to or receipt
        from a production order), asked of the ITEM across every warehouse:
        a pallet restacked between godowns writes a transfer, and a transfer
        must not reset the clock. Stock never consumed falls back to its last
        non-transfer movement, then to the item's creation date.

        Then the page's own pipeline: one line per SKU keeping the FRESHEST
        movement of the group (an item consumed in one store must not read as
        dead because a pallet of it sits in another), and the recently moved
        set aside. No percentage: with no age floor almost every SKU has an
        idle day, so a share would read near 100% every day.
        """
        t = self.hana.table
        production = f"{TRANS_TYPE_GOODS_ISSUE}, {TRANS_TYPE_PRODUCTION_RECEIPT}"
        moved = 'COALESCE(N."InQty", 0) <> 0 OR COALESCE(N."OutQty", 0) <> 0'
        rows = self._hana_rows(
            "non_moving",
            f"""WITH Stock AS (
    SELECT
        W."ItemCode",
        W."WhsCode",
        M."CreateDate",
        COALESCE(W."OnHand", 0) AS "OnHand",
        CASE
            WHEN COALESCE(W."AvgPrice", 0) <> 0 THEN W."AvgPrice"
            WHEN COALESCE(M."AvgPrice", 0) <> 0 THEN M."AvgPrice"
            ELSE COALESCE(M."LastPurPrc", 0)
        END AS "UnitCost"
    FROM {t("OITW")} W
    INNER JOIN {t("OITM")} M ON M."ItemCode" = W."ItemCode"
    INNER JOIN {t("OWHS")} H ON H."WhsCode" = W."WhsCode"
    WHERE COALESCE(W."OnHand", 0) > 0
      AND COALESCE(H."Inactive", 'N') <> 'Y'
      AND M."ItmsGrpCod" = ?
      AND W."WhsCode" IN ({_placeholders(len(STORE_WAREHOUSES))})
),
ItemMovement AS (
    SELECT
        N."ItemCode",
        MAX(CASE WHEN ({moved}) AND N."TransType" IN ({production}) THEN N."DocDate" END) AS "LastProductionDate",
        MAX(CASE WHEN ({moved}) AND COALESCE(N."TransType", 0) <> {TRANS_TYPE_TRANSFER_IN}
                 THEN N."DocDate" END) AS "LastNonTransferDate"
    FROM {t("OINM")} N
    WHERE N."ItemCode" IN (SELECT "ItemCode" FROM Stock)
    GROUP BY N."ItemCode"
)
SELECT
    S."ItemCode",
    S."WhsCode",
    S."OnHand" AS "Quantity",
    ROUND(S."OnHand" * S."UnitCost", 4) AS "Value",
    CASE
        WHEN COALESCE(I."LastProductionDate", I."LastNonTransferDate", S."CreateDate") IS NULL THEN 0
        ELSE DAYS_BETWEEN(COALESCE(I."LastProductionDate", I."LastNonTransferDate", S."CreateDate"), CURRENT_DATE)
    END AS "Days"
FROM Stock S
LEFT JOIN ItemMovement I ON I."ItemCode" = S."ItemCode"
""",
            [PACKAGING_ITEM_GROUP, *STORE_WAREHOUSES],
        )

        scope = {code.upper() for code in STORE_WAREHOUSES}
        by_item: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            code = row.get("ItemCode")
            if not code or (row.get("WhsCode") or "").strip().upper() not in scope:
                continue
            days = _int(row.get("Days"))
            slot = by_item.get(code)
            if slot is None:
                by_item[code] = {"days": days, "value": _f(row.get("Value")), "quantity": _f(row.get("Quantity"))}
                continue
            slot["value"] += _f(row.get("Value"))
            slot["quantity"] += _f(row.get("Quantity"))
            slot["days"] = min(slot["days"], days)

        counts = {"slow-moving": 0, "non-moving": 0}
        values = {"slow-moving": 0.0, "non-moving": 0.0}
        idle: List[Tuple[int, float]] = []
        recent = 0
        for slot in by_item.values():
            status = movement_status(slot["days"])
            if status == "recent":
                recent += 1
                continue
            counts[status] += 1
            values[status] += slot["value"]
            idle.append((slot["days"], slot["value"]))

        return {
            "item_count": len(idle),
            "total_value": round(sum(value for _, value in idle), 2),
            "slow_moving_count": counts["slow-moving"],
            "non_moving_count": counts["non-moving"],
            "slow_moving_value": round(values["slow-moving"], 2),
            "non_moving_value": round(values["non-moving"], 2),
            "oldest_days": max((days for days, _ in idle), default=0),
            "recent_count": recent,
            "warehouses": sorted(scope),
        }

    def _pm_in(self) -> Dict[str, Any]:
        """Trucks that brought packing material in today.

        There is no packing-material gate entry type, so a packaging truck
        arrives as a raw-material entry and is identified by where its PO
        lines land: a packaging PO lands in a packaging store. Counted
        DISTINCT on the vehicle entry: one truck carrying three POs is one.

        Received is written at the gate; accepted and rejected only when the
        GRPO is posted to SAP. The gap is stock in the building SAP does not
        have yet, normally most of a day's intake, not a quality question.
        """
        row = (
            self.postgres.one(
                marker("pm_in")
                + """SELECT COUNT(DISTINCT pr.vehicle_entry_id) AS vehicles,
       COUNT(DISTINCT i.po_receipt_id) AS pos,
       COUNT(i.id) AS lines,
       COALESCE(SUM(i.received_qty), 0) AS received,
       COALESCE(SUM(i.accepted_qty), 0) AS accepted,
       COALESCE(SUM(i.rejected_qty), 0) AS rejected
FROM raw_material_gatein_poitemreceipt i
JOIN raw_material_gatein_poreceipt pr ON pr.id = i.po_receipt_id
JOIN driver_management_vehicleentry ve ON ve.id = pr.vehicle_entry_id
WHERE ve.company_id = %s
  AND (ve.entry_time AT TIME ZONE %s)::date = %s
  AND i.warehouse_code = ANY(%s)""",
                [self._company(), self._tz(), self.today, list(STORE_WAREHOUSES)],
            )
            or {}
        )
        received = _f(row.get("received"))
        accepted = _f(row.get("accepted"))
        rejected = _f(row.get("rejected"))
        return {
            "count": _int(row.get("vehicles")),
            "po_count": _int(row.get("pos")),
            "line_count": _int(row.get("lines")),
            "received_qty": received,
            "accepted_qty": accepted,
            "rejected_qty": rejected,
            "awaiting_grpo_qty": round(max(0.0, received - accepted - rejected), 3),
        }

    def _blowing(self) -> Dict[str, Any]:
        """Bottles blown this calendar month, what they cost, and the week.

        The Blowing dashboard's own figures: the machine counter, rejects and
        the cost summary's ``net_cost`` (scrap credited back, resin included)
        over the calendar month, no status filter. The calendar month and
        not the plan month, so the two screens agree. The rate per bottle is
        total cost over costed bottles, the weighted figure. Averages divide
        by the days that actually BLEW: a Sunday is not a bad day.
        """
        window_from = self.today.replace(day=1)
        window_to = self.today
        days = self.postgres.rows(
            marker("blowing_days")
            + """SELECT r.date AS day,
       COUNT(r.id) AS runs,
       COALESCE(SUM(r.total_counter_production), 0) AS bottles,
       COALESCE(SUM(r.rejection_pcs), 0) AS rejected,
       COUNT(c.id) AS costed_runs,
       COALESCE(SUM(c.net_cost), 0) AS net_cost,
       COALESCE(SUM(c.preform_cost), 0) AS preform_cost,
       COALESCE(SUM(c.good_bottles), 0) AS good_bottles
FROM blowing_blowingrun r
LEFT JOIN blowing_blowingruncost c ON c.run_id = r.id
WHERE r.company_id = %s AND r.date >= %s AND r.date <= %s
GROUP BY r.date
ORDER BY r.date""",
            [self._company(), window_from, window_to],
        )

        by_day: Dict[date, int] = {}
        made = rejected = costed_bottles = run_count = costed_runs = 0
        net = preform = 0.0
        for row in days:
            day = formatting.as_date(row.get("day"))
            if day is None:
                continue
            bottles = _int(row.get("bottles"))
            by_day[day] = by_day.get(day, 0) + bottles
            made += bottles
            rejected += _int(row.get("rejected"))
            run_count += _int(row.get("runs"))
            costed_runs += _int(row.get("costed_runs"))
            net += _f(row.get("net_cost"))
            preform += _f(row.get("preform_cost"))
            costed_bottles += _int(row.get("good_bottles"))

        uncosted = run_count - costed_runs
        if uncosted:
            self.warn(f"{uncosted} of {run_count} blowing runs this month have no cost row yet.")

        # Days that BLEW: a date with any run on it, as the factory counts them.
        active_days = len(by_day)
        total_cost = net

        trend: List[Dict[str, Any]] = []
        day = max(window_from, window_to - timedelta(days=TREND_DAYS - 1))
        while day <= window_to:
            trend.append({"date": day.isoformat(), "bottles": by_day.get(day, 0)})
            day += timedelta(days=1)

        return {
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "daily": trend,
            "runs": run_count,
            "uncosted_runs": uncosted,
            "active_days": active_days,
            "bottles_made": made,
            "bottles_rejected": rejected,
            "bottles_good": max(0, made - rejected),
            "avg_bottles_per_day": round(made / active_days, 1) if active_days else None,
            "cost": round(total_cost, 2),
            "preform_cost": round(preform, 2),
            "conversion_cost": round(max(0.0, total_cost - preform), 2),
            "avg_cost_per_day": round(total_cost / active_days, 2) if active_days else None,
            "cost_per_bottle": round(total_cost / costed_bottles, 4) if costed_bottles else None,
            **self._blowing_running(),
        }

    def _blowing_running(self) -> Dict[str, Any]:
        """What the lines turning right now have cost so far.

        RUNNING MEANS AN OPEN SEGMENT, never the run's status: a run sits at
        IN_PROGRESS while the machine stands idle. Summed across runs; the
        money is resin plus blowing, as of the last costing, a floor on the
        real figure and never an overstatement.
        """
        row = (
            self.postgres.one(
                marker("blowing_running")
                + """SELECT COUNT(r.id) AS runs,
       COALESCE(SUM(r.total_counter_production), 0) AS bottles,
       COALESCE(SUM(c.preform_cost), 0) AS preform_cost,
       COALESCE(SUM(c.blowing_cost), 0) AS blowing_cost,
       COUNT(r.id) - COUNT(c.id) AS uncosted,
       COUNT(CASE WHEN r.date < %s THEN r.id END) AS started_earlier
FROM blowing_blowingrun r
LEFT JOIN blowing_blowingruncost c ON c.run_id = r.id
WHERE r.company_id = %s
  AND EXISTS (
      SELECT 1 FROM blowing_blowingsegment s
      WHERE s.blowing_run_id = r.id AND s.end_time IS NULL AND s.is_active
  )""",
                [self.today, self._company()],
            )
            or {}
        )
        preform = _f(row.get("preform_cost"))
        blowing = _f(row.get("blowing_cost"))
        return {
            "running_runs": _int(row.get("runs")),
            "running_bottles": _int(row.get("bottles")),
            "running_preform_cost": preform,
            "running_blowing_cost": blowing,
            "running_cost": round(preform + blowing, 2),
            "running_uncosted": _int(row.get("uncosted")),
            # Open from an earlier day: a line through midnight, or a segment
            # nobody closed. Either way not "today".
            "running_started_earlier": _int(row.get("started_earlier")),
        }

    # ----------------------------------------------- band 3: production tiles

    def _today_on_the_lines(self) -> Dict[str, Any]:
        """What the supervisor planned for today and what the lines packed.

        From Production Execution, not SAP: SAP holds no single-day plan and
        learns of output after the shift. Summed across every live run dated
        today (discarded runs excluded). DRAFTS COUNT AS PLANNED: a run is
        planned the evening before. A run still on the floor has no total
        yet, so its segments stand in. CASES lead because cases are what the
        supervisor types; pieces are converted per run by its own snapshot
        factor, and a run with none is left out of the pieces and named.
        """
        runs = self.postgres.rows(
            marker("today_runs")
            + """SELECT pr.id, pr.line_id, pr.status, pr.required_qty, pr.total_production,
       pr.pieces_per_case,
       (SELECT COALESCE(SUM(s.produced_cases), 0)
          FROM production_execution_productionsegment s
         WHERE s.production_run_id = pr.id) AS segment_cases
FROM production_execution_productionrun pr
WHERE pr.company_id = %s AND pr.date = %s AND NOT pr.is_deleted
ORDER BY pr.id""",
            [self._company(), self.today],
        )
        if not runs:
            return {
                "date": self.today.isoformat(),
                "planned_qty": 0.0,
                "produced_qty": 0.0,
                "planned_cases": 0.0,
                "produced_cases": 0.0,
                "attainment_pct": None,
                "runs": 0,
                "lines": 0,
                "completed_runs": 0,
                "unconverted_runs": 0,
            }

        planned_pieces = produced_pieces = planned_cases = produced_cases = 0.0
        completed = unconverted = 0
        for run in runs:
            if run.get("status") == "COMPLETED":
                completed += 1
            planned = _f(run.get("required_qty"))
            made = _f(run.get("total_production"))
            if made <= 0:
                made = _f(run.get("segment_cases"))
            planned_cases += planned
            produced_cases += made
            per_case = _f(run.get("pieces_per_case"))
            if not per_case:
                if planned > 0 or made > 0:
                    unconverted += 1
                continue
            planned_pieces += planned * per_case
            produced_pieces += made * per_case

        return {
            "date": self.today.isoformat(),
            "planned_qty": round(planned_pieces, 3),
            "produced_qty": round(produced_pieces, 3),
            "planned_cases": round(planned_cases, 3),
            "produced_cases": round(produced_cases, 3),
            "attainment_pct": round(produced_pieces / planned_pieces * 100, 1) if planned_pieces > 0 else None,
            "runs": len(runs),
            "lines": len({run.get("line_id") for run in runs}),
            "completed_runs": completed,
            "unconverted_runs": unconverted,
        }

    def _monthly_planning(self) -> Dict[str, Any]:
        """The month's plan against the whole floor's output, in tonnes.

        Both sides are SAP's: the plan off ``FCT1`` in the item's inventory
        unit, the output off ``OINM`` TransType 59 into BH-PF in the same
        unit, so the comparison needs no pack factor. OUTPUT IS THE WHOLE
        FLOOR, planned or not: reading it through the plan's items made the
        factory's tile report 1,290 t of a month the plant finished at
        1,515 t. What the plan cannot speak for is reported beside it.

        The plan lines are read per ITEM. Every live plan is monthly (one
        line per item); on a weekly plan the factory credited each bucket
        line with the item's whole output, which this does not repeat.
        """
        plan = self._require_plan()
        t = self.hana.table
        lines = self._hana_rows(
            "plan_lines",
            f"""SELECT L."ItemCode",
       SUM(L."Quantity") AS "PlannedQty",
       MAX(IFNULL(M."SalFactor2", 1)) AS "PiecesPerCase",
       MAX({LITRES_PER_UNIT}) AS "LitresPerUnit"
FROM {t("FCT1")} L
LEFT JOIN {t("OITM")} M ON M."ItemCode" = L."ItemCode"
WHERE L."AbsID" = ?
GROUP BY L."ItemCode"
""",
            [int(plan["abs_id"])],
        )

        period_start = formatting.as_date(plan.get("start_date")) or self.today.replace(day=1)
        period_end = formatting.as_date(plan.get("end_date")) or self.today
        codes = sorted({row.get("ItemCode") for row in lines if row.get("ItemCode")})
        produced: Dict[str, float] = {}
        for chunk in _chunks(codes):
            for row in self._hana_rows(
                "plan_produced",
                f"""SELECT N."ItemCode", SUM(N."InQty") AS "ProducedQty"
FROM {t("OINM")} N
WHERE N."TransType" = {TRANS_TYPE_PRODUCTION_RECEIPT}
  AND N."InQty" > 0
  AND N."DocDate" >= ?
  AND N."DocDate" <= ?
  AND N."ItemCode" IN ({_placeholders(len(chunk))})
GROUP BY N."ItemCode"
""",
                [period_start, period_end, *chunk],
            ):
                produced[row.get("ItemCode")] = _f(row.get("ProducedQty"))

        planned_qty = produced_qty = planned_cases = produced_cases = 0.0
        planned_litres = planned_items_litres = 0.0
        unweighed_lines = 0
        for row in lines:
            qty = _f(row.get("PlannedQty"))
            per_case = _int(row.get("PiecesPerCase")) or 1
            per_litre = _f(row.get("LitresPerUnit"))
            made = produced.get(row.get("ItemCode"), 0.0)
            line_litres = round(qty * per_litre, 3) if per_litre > 0 else 0.0
            planned_qty += qty
            produced_qty += made
            planned_cases += qty if per_case <= 1 else round(qty / per_case, 2)
            produced_cases += made if per_case <= 1 else round(made / per_case, 2)
            planned_litres += line_litres
            planned_items_litres += round(made * per_litre, 3) if per_litre > 0 else 0.0
            # Planned in pieces with no litre volume: in neither ton figure.
            if qty > 0 and not line_litres:
                unweighed_lines += 1

        window_from, window_to = self._plan_window()
        floor_days = self._floor_production(window_from, window_to)
        produced_litres = sum(row["litres"] for row in floor_days.values())
        floor_pieces = sum(row["pieces"] for row in floor_days.values())
        daily = []
        day = window_from
        while day <= window_to:
            daily.append({"date": day.isoformat(), "qty": round(floor_days.get(day, {}).get("pieces", 0.0), 2)})
            day += timedelta(days=1)
        active_days = [row for row in daily if row["qty"] > 0]

        planned_tons = _tons(planned_litres)
        produced_tons = _tons(produced_litres)
        return {
            "planned_qty": round(planned_qty, 3),
            "produced_qty": round(floor_pieces, 3),
            "planned_cases": round(planned_cases, 2),
            "produced_cases": round(produced_cases, 2),
            # On PIECES: each ratio is a sum over items with different pack
            # factors, so converting first would re-weight the mix.
            "attainment_pct": round(floor_pieces / planned_qty * 100, 1) if planned_qty > 0 else None,
            "planned_tons": planned_tons,
            "produced_tons": produced_tons,
            "produced_planned_qty": round(produced_qty, 3),
            "produced_planned_tons": _tons(planned_items_litres),
            "attainment_planned_pct": round(produced_qty / planned_qty * 100, 1) if planned_qty > 0 else None,
            # Output the plan never listed. Never negative: a plan actual above
            # the floor's own receipts is a question, not a surplus.
            "produced_unplanned_tons": max(round(produced_tons - _tons(planned_items_litres), 2), 0),
            # The tile reads in tons, so its percentage is on the tons.
            "attainment_tons_pct": round(produced_tons / planned_tons * 100, 1) if planned_tons > 0 else None,
            "unweighed_lines": unweighed_lines,
            "avg_qty_per_active_day": round(floor_pieces / len(active_days), 1) if active_days else None,
            "active_days": len(active_days),
            "elapsed_days": plan.get("days_elapsed"),
            "daily": daily[-MAX_LISTED_ROWS:],
        }

    def _floor_production(self, window_from: date, window_to: date) -> Dict[date, Dict[str, float]]:
        """Everything received onto BH-PF, per day, from SAP's journal: every
        item on the floor, not the plan's list."""
        t = self.hana.table
        by_day: Dict[date, Dict[str, float]] = {}
        for row in self._hana_rows(
            "floor_production",
            f"""SELECT N."DocDate" AS "Day",
       SUM(N."InQty") AS "Pieces",
       SUM(N."InQty" * ({LITRES_PER_UNIT})) AS "Litres"
FROM {t("OINM")} N
JOIN {t("OITM")} M ON M."ItemCode" = N."ItemCode"
WHERE N."TransType" = {TRANS_TYPE_PRODUCTION_RECEIPT}
  AND N."Warehouse" = ?
  AND IFNULL(N."InQty", 0) > 0
  AND N."DocDate" >= ? AND N."DocDate" <= ?
GROUP BY N."DocDate"
ORDER BY N."DocDate"
""",
            [PRODUCTION_FLOOR, window_from, window_to],
        ):
            day = formatting.as_date(row.get("Day"))
            if day is None:
                continue
            held = by_day.setdefault(day, {"pieces": 0.0, "litres": 0.0})
            held["pieces"] += _f(row.get("Pieces"))
            held["litres"] += _f(row.get("Litres"))
        return by_day

    def _floor_stock(self) -> Dict[str, Any]:
        """What BH-PF holds, what it is worth, and how long it has stood.

        Aged on when stock last LEFT, never on any movement: BH-PF is the
        floor goods are produced INTO, so a receipt is arrival. An item that
        has never left is ``never_shipped``, its own (worse) bucket.

        The stock read is the Production Control board's occupancy read, so
        the litres per piece is ``OITM.SalPackUn`` as that read returns it
        (the factory's logistics board, ported beside this one, reads it the
        same way); an item with none is counted out loud in ``unweighed``.
        """
        t = self.hana.table
        rows = self._hana_rows(
            "floor_stock",
            f"""SELECT
    w."ItemCode",
    COALESCE(w."OnHand", 0) AS "OnHand",
    COALESCE(i."SalFactor2", 0) AS "PiecesPerBox",
    COALESCE(i."SalPackUn", 0) AS "LitresPerPiece",
    COALESCE(w."StockValue", 0) AS "StockValue"
FROM {t("OITW")} w
JOIN {t("OITM")} i ON i."ItemCode" = w."ItemCode"
WHERE w."WhsCode" = ?
  AND COALESCE(w."OnHand", 0) <> 0
ORDER BY COALESCE(w."OnHand", 0) DESC""",
            [PRODUCTION_FLOOR],
        )
        last_out = {
            row.get("ItemCode"): formatting.as_date(row.get("LastOut"))
            for row in self._hana_rows(
                "last_out",
                f"""SELECT N."ItemCode", MAX(N."DocDate") AS "LastOut"
FROM {t("OINM")} N
WHERE N."Warehouse" = ?
  AND IFNULL(N."OutQty", 0) > 0
GROUP BY N."ItemCode"
""",
                [PRODUCTION_FLOOR],
            )
            if row.get("ItemCode")
        }

        def bucket() -> Dict[str, Any]:
            return {"value": 0.0, "pieces": 0.0, "litres": 0.0, "items": 0, "unweighed": 0}

        fresh, mid, old, never = bucket(), bucket(), bucket(), bucket()
        litres = 0.0
        unweighed = 0
        for row in rows:
            pieces = _f(row.get("OnHand"))
            if pieces <= 0:
                continue
            per_piece = _f(row.get("LitresPerPiece"))
            row_litres = pieces * per_piece
            litres += row_litres
            if per_piece <= 0:
                unweighed += 1
            out_date = last_out.get(row.get("ItemCode"))
            if out_date is None:
                slot = never
            else:
                age = (self.today - out_date).days
                slot = fresh if age < AGE_BUCKET_LOWER else (mid if age <= AGE_BUCKET_UPPER else old)
            slot["value"] += _f(row.get("StockValue"))
            slot["pieces"] += pieces
            slot["litres"] += row_litres
            slot["items"] += 1
            if per_piece <= 0:
                slot["unweighed"] += 1

        for slot in (fresh, mid, old, never):
            slot["value"] = round(slot["value"], 2)
            slot["pieces"] = round(slot["pieces"], 2)
            slot["tons"] = _tons(slot["litres"])
            slot["litres"] = round(slot["litres"], 3)

        return {
            "warehouse": PRODUCTION_FLOOR,
            "stock_value": round(sum(_f(row.get("StockValue")) for row in rows), 2),
            "total_pieces": round(sum(_f(row.get("OnHand")) for row in rows), 3),
            "tons": _tons(litres),
            "item_count": len(rows),
            "unconfigured_items": sum(1 for row in rows if not _f(row.get("PiecesPerBox"))),
            "unweighed_items": unweighed,
            "age": {
                "fresh": fresh,
                f"d{AGE_BUCKET_LOWER}_{AGE_BUCKET_UPPER}": mid,
                f"d{AGE_BUCKET_UPPER}_plus": old,
                "never_shipped": never,
            },
            "age_basis": "Days since stock last left this warehouse, not since any movement.",
        }

    def _wastage(self) -> Dict[str, Any]:
        """What the floor wrote down as waste, day by day, in money.

        DATED BY THE RUN, not by when the row was typed: waste is written up
        one to eight days after the shift, so a typing date piles two shifts
        onto whichever morning somebody did the paperwork. A standalone row
        has no run and keeps its typed date (in the factory's zone).

        VALUED, BECAUSE UNITS DO NOT ADD UP: the register holds pieces, kilos
        and metres side by side. Each row is priced at the unit price on the
        SAME run's material line for that code (the price that run was costed
        at), else the newest price seen for the code; a row priced at neither
        is counted, not dropped. Packing material and raw material are told
        apart by SAP's item group, the requirement sheet's own rule.

        The headline is the newest day anything WAS logged against, named and
        aged: today is almost always empty, and a wall headlining it would
        report a clean shift every evening on the strength of missing
        paperwork. The window is the plan month (the calendar month without
        a plan); the factory's oil-yield figure is drill-only and not read.
        """
        window_from, window_to = self._plan_window()
        tz = self._tz()
        company = self._company()
        logs = self.postgres.rows(
            marker("waste_logs")
            + """SELECT w.material_code, w.wastage_qty, w.wastage_approval_status, w.uom,
       w.production_run_id, pr.date AS run_date,
       (w.created_at AT TIME ZONE %s)::date AS typed_on
FROM production_execution_wastelog w
LEFT JOIN production_execution_productionrun pr ON pr.id = w.production_run_id
WHERE w.company_id = %s
  AND (
        (w.production_run_id IS NOT NULL AND pr.date >= %s AND pr.date <= %s)
     OR (w.production_run_id IS NULL
         AND (w.created_at AT TIME ZONE %s)::date >= %s
         AND (w.created_at AT TIME ZONE %s)::date <= %s)
  )""",
            [tz, company, window_from, window_to, tz, window_from, tz, window_to],
        )

        codes = sorted({row.get("material_code") for row in logs if row.get("material_code")})
        kinds = self._classify(codes)
        exact_price, last_price = self._waste_prices(codes)

        pm_qty = pm_value = rm_qty = rm_value = 0.0
        unclassified = approved = standalone = unpriced = 0
        by_day: Dict[date, Dict[str, Any]] = {}
        for row in logs:
            qty = _f(row.get("wastage_qty"))
            if row.get("wastage_approval_status") == "FULLY_APPROVED":
                approved += 1
            day = formatting.as_date(row.get("run_date"))
            if day is None:
                standalone += 1
                day = formatting.as_date(row.get("typed_on"))
            if day is None:
                continue
            code = row.get("material_code") or ""
            price = exact_price.get((row.get("production_run_id"), code)) or last_price.get(code)
            value = qty * _f(price) if price else 0.0

            kind = kinds.get(code)
            if kind == "PACKAGING":
                pm_qty += qty
                pm_value += value
                bucket = "pm"
            elif kind == "RAW":
                rm_qty += qty
                rm_value += value
                bucket = "rm"
            else:
                unclassified += 1
                bucket = "other"

            slot = by_day.setdefault(day, self._blank_waste_day())
            slot["logs"] += 1
            uom = (row.get("uom") or "").strip().upper() or "-"
            slot[bucket][uom] = slot[bucket].get(uom, 0.0) + qty
            slot["value"][bucket] += value
            if not price:
                unpriced += 1
                slot["unpriced"] += 1

        latest = max((day for day, slot in by_day.items() if slot["logs"]), default=None)
        first = max(window_from, window_to - timedelta(days=TREND_DAYS - 1))
        daily = []
        day = first
        while day <= window_to:
            daily.append(self._waste_day(by_day.get(day), day))
            day += timedelta(days=1)

        return {
            "pm_logged_value": round(pm_value, 2),
            "rm_logged_value": round(rm_value, 2),
            "logged_unpriced_count": unpriced,
            "pm_logged_qty": round(pm_qty, 3),
            "pm_log_count": len(logs),
            "pm_approved_count": approved,
            "pm_unclassified_count": unclassified,
            "oil_logged_qty": round(rm_qty, 3),
            "logged_daily": daily,
            "logged_today": self._waste_day(by_day.get(self.today), self.today),
            "logged_latest": self._waste_day(by_day.get(latest), latest) if latest else None,
            "logged_latest_date": latest.isoformat() if latest else None,
            "logged_days_behind": (self.today - latest).days if latest else None,
            "logged_standalone_count": standalone,
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
        }

    @staticmethod
    def _blank_waste_day() -> Dict[str, Any]:
        return {"pm": {}, "rm": {}, "other": {}, "value": {"pm": 0.0, "rm": 0.0, "other": 0.0}, "logs": 0, "unpriced": 0}

    def _waste_day(self, slot: Optional[Dict[str, Any]], day: date) -> Dict[str, Any]:
        """One day of the register: what it cost, and what it was, each
        quantity in its own unit and never summed across units."""
        slot = slot or self._blank_waste_day()
        value = slot["value"]

        def split(kinds: Dict[str, float], headline_uom: str):
            head = round(kinds.get(headline_uom, 0.0), 3)
            rest = [
                {"uom": uom, "qty": round(qty, 3)}
                for uom, qty in sorted(kinds.items())
                if uom != headline_uom and round(qty, 3)
            ]
            return head, rest

        pm_pieces, pm_other = split(slot["pm"], "PCS")
        rm_litres, rm_other = split(slot["rm"], "LTR")
        return {
            "date": day.isoformat(),
            "logs": slot["logs"],
            "pm_value": round(value["pm"], 2),
            "rm_value": round(value["rm"], 2),
            "other_value": round(value["other"], 2),
            "total_value": round(value["pm"] + value["rm"] + value["other"], 2),
            "unpriced": slot["unpriced"],
            "pm_pieces": pm_pieces,
            "pm_other": pm_other,
            "rm_litres": rm_litres,
            "rm_other": rm_other,
        }

    def _waste_prices(self, codes: List[str]):
        """Two price lookups for the materials in the waste rows: per (run,
        material), which should hit, and the newest price for the code
        anywhere, which stands in for a run whose line carries none. One
        read, oldest run first, so the last write into ``last`` is newest."""
        if not codes:
            return {}, {}
        exact: Dict[Tuple[Any, str], float] = {}
        last: Dict[str, float] = {}
        for line in self.postgres.rows(
            marker("waste_prices")
            + """SELECT u.production_run_id, u.material_code, u.unit_price
FROM production_execution_productionmaterialusage u
JOIN production_execution_productionrun pr ON pr.id = u.production_run_id
WHERE pr.company_id = %s
  AND u.material_code = ANY(%s)
  AND u.unit_price IS NOT NULL
  AND u.unit_price <> 0
ORDER BY pr.date, u.id""",
            [self._company(), list(codes)],
        ):
            price = _f(line.get("unit_price"))
            exact[(line.get("production_run_id"), line.get("material_code"))] = price
            last[line.get("material_code")] = price
        return exact, last

    def _classify(self, codes: List[str]) -> Dict[str, str]:
        """RAW / PACKAGING / OTHER per code, from the SAP item group. A code
        SAP does not know comes back absent and is counted as unclassified."""
        if not codes:
            return {}
        t = self.hana.table
        out: Dict[str, str] = {}
        for chunk in _chunks(codes):
            for row in self._hana_rows(
                "classify",
                f"""SELECT M."ItemCode", IFNULL(G."ItmsGrpNam", '') AS "ItemGroup"
FROM {t("OITM")} M
LEFT JOIN {t("OITB")} G ON G."ItmsGrpCod" = M."ItmsGrpCod"
WHERE M."ItemCode" IN ({_placeholders(len(chunk))})""",
                list(chunk),
            ):
                out[row.get("ItemCode")] = classify_material(row.get("ItemGroup"))
        return out

    # ------------------------------------------------- band 4: shifting tiles

    def _declared(self) -> Dict[str, Any]:
        """Today's declared consignments off BH-PF, by destination: the
        godown keeper's own note on the Godown Stock Movements page.

        Scoped on the SOURCE company and floor. No SAP call: the register
        snapshots litres per piece onto each line as it is typed, so its
        tonnage stands whatever HANA is doing. Retracted declarations (the
        register deactivates rather than deletes) are excluded and counted.
        """
        lines = self.postgres.rows(
            marker("declared")
            + """SELECT m.id AS movement_id, m.destination_kind, m.to_warehouse, m.is_active,
       l.item_code, l.pieces, l.pieces_per_box, l.litres_per_piece
FROM warehouse_pf_movement m
LEFT JOIN warehouse_pf_movement_line l ON l.movement_id = m.id
WHERE m.company_id = %s AND m.from_warehouse = %s AND m.movement_date = %s
ORDER BY m.id, l.item_code""",
            [self._company(), PRODUCTION_FLOOR, self.today],
        )

        rows: List[Dict[str, Any]] = []
        live: set = set()
        retracted: set = set()
        retracted_pieces = 0.0
        for line in lines:
            movement = line.get("movement_id")
            has_line = line.get("item_code") is not None
            pieces = _int(line.get("pieces")) if has_line else 0
            if not line.get("is_active"):
                retracted.add(movement)
                retracted_pieces += pieces
                continue
            live.add(movement)
            if not has_line:
                continue
            route = (
                SHIFTING_DISPATCH
                if line.get("destination_kind") == "DISPATCH"
                else (line.get("to_warehouse") or "").strip() or SHIFTING_ELSEWHERE
            )
            per_piece = line.get("litres_per_piece")
            per_box = _int(line.get("pieces_per_box"))
            rows.append(
                {
                    "route": route,
                    "pieces": float(pieces),
                    # None, not zero: a carton is not zero litres, it is not
                    # measured in litres.
                    "litres": None if per_piece is None else pieces * _f(per_piece),
                    "boxes": pieces // per_box if per_box else 0,
                    "item_code": line.get("item_code"),
                }
            )

        totals = self._fold_routes(rows, weighed=True, routes=SHIFTING_DECLARED_ROUTES)
        totals["transfers"] = len(live)
        totals["retracted_movements"] = len(retracted)
        totals["retracted_pieces"] = round(retracted_pieces, 2)
        return totals

    def _shipped(self) -> Dict[str, Any]:
        """Boxes on a BST dispatched today, by where they went.

        Dated on the DISPATCH stamp, so a load scanned last night and sent
        this morning is today's. Scoped on the source company and floor
        (BH-BT ships to Mart too), cancelled transfers excluded. Rejected
        boxes did physically leave and are counted in, then disclosed.

        The scans are the factory database's; only the litres are SAP's. If
        SAP cannot answer, the tonnage is withheld (null, never zero) and the
        pieces still stand: losing a unit is smaller than losing the tile.
        """
        groups = self.postgres.rows(
            marker("shipped")
            + """SELECT t.id AS transfer_id, t.sap_to_warehouse, t.source_type, s.item_code,
       COALESCE(SUM(s.quantity), 0) AS pieces,
       COUNT(s.id) AS boxes,
       COALESCE(SUM(CASE WHEN s.receive_status = 'REJECTED' THEN s.quantity END), 0) AS rejected_pieces,
       COUNT(CASE WHEN s.receive_status = 'REJECTED' THEN s.id END) AS rejected_boxes
FROM warehouse_bstboxscan s
JOIN warehouse_bsttransfer t ON t.id = s.transfer_id
WHERE t.company_id = %s
  AND t.sap_from_warehouse = %s
  AND t.status <> 'CANCELLED'
  AND (t.dispatched_at AT TIME ZONE %s)::date = %s
GROUP BY t.id, t.sap_to_warehouse, t.source_type, s.item_code
ORDER BY t.id, s.item_code""",
            [self._company(), PRODUCTION_FLOOR, self._tz(), self.today],
        )

        litres_per_piece, weighed = self._bst_litres([row.get("item_code") for row in groups])

        def as_rows(pieces_key: str, boxes_key: str) -> List[Dict[str, Any]]:
            out = []
            for row in groups:
                boxes = _int(row.get(boxes_key))
                if not boxes:
                    continue
                pieces = _f(row.get(pieces_key))
                per_piece = litres_per_piece.get(row.get("item_code"), 0.0)
                out.append(
                    {
                        "route": self._bst_route(row.get("sap_to_warehouse"), row.get("source_type")),
                        "pieces": pieces,
                        "litres": (pieces * per_piece) if per_piece else None,
                        "boxes": boxes,
                        "item_code": row.get("item_code"),
                    }
                )
            return out

        totals = self._fold_routes(as_rows("pieces", "boxes"), weighed=weighed)
        rejected = self._fold_routes(as_rows("rejected_pieces", "rejected_boxes"), weighed=weighed)
        totals["transfers"] = len({row.get("transfer_id") for row in groups})
        totals["rejected_pieces"] = rejected["total_pieces"]
        totals["rejected_tons"] = rejected["total_tons"]
        return totals

    def _bst_litres(self, item_codes: Iterable[Any]) -> Tuple[Dict[str, float], bool]:
        """Litres in one piece per item code, and whether SAP answered.

        Through the litre gate (``U_IsLitre``): a box scan stores pieces, and
        an item without the flag is 0 rather than guessed at. Memoised for
        the build. A build that already found SAP down asks nothing; a
        connection failure here latches SAP off like any other, but costs
        this tile only its unit.
        """
        codes = sorted({code for code in item_codes if code})
        missing = [code for code in codes if code not in self._litre_cache]
        if missing and not self._sap_down:
            try:
                t = self.hana.table
                for chunk in _chunks(missing):
                    for row in self._hana_rows(
                        "litres",
                        f"""SELECT M."ItemCode", ({LITRES_PER_UNIT}) AS "Litres"
FROM {t("OITM")} M
WHERE M."ItemCode" IN ({_placeholders(len(chunk))})""",
                        list(chunk),
                    ):
                        self._litre_cache[row.get("ItemCode")] = _f(row.get("Litres"))
                # An item SAP holds no volume for is an answer, not a gap.
                for code in missing:
                    self._litre_cache.setdefault(code, 0.0)
                missing = []
            except SAPConnectionError as exc:
                self._sap_down = str(exc)
                logger.warning("Plant board: shifting tonnage unavailable: %s", exc)
            except SAPDataError as exc:
                logger.warning("Plant board: shifting tonnage unavailable: %s", exc)
        # Weighed only when every code has an answer: a half-read tonnage is
        # worse than none, because it looks like a light day.
        if missing:
            return {}, False
        return {code: self._litre_cache.get(code, 0.0) for code in codes}, True

    @staticmethod
    def _bst_route(to_warehouse: Any, source_type: Any) -> str:
        """An INVOICE-sourced BST is a cross-company sale: Dispatch. A stock
        transfer's column is its destination warehouse."""
        if source_type == "INVOICE":
            return SHIFTING_DISPATCH
        return (to_warehouse or "").strip() or SHIFTING_ELSEWHERE

    def _fold_routes(self, rows: List[Dict[str, Any]], weighed: bool = True, routes=None) -> Dict[str, Any]:
        """Put line-level rows onto the band's routes, in a fixed order.

        THE NAMED ROUTES ARE ALWAYS PRESENT, at zero if need be: the wall
        reads the same rows down both tiles, and a row that vanishes on a
        quiet day shuffles the ones below it. Anything not named folds into
        one Elsewhere row that appears only when it carries something and
        prints the codes it folded, so nothing is dropped.
        """
        named = list(routes or SHIFTING_ROUTES)

        def blank(route: str) -> Dict[str, Any]:
            return {
                "route": route,
                "name": SHIFTING_ROUTE_NAMES.get(route, route),
                "is_dispatch": route == SHIFTING_DISPATCH,
                "pieces": 0.0,
                "litres": 0.0,
                "boxes": 0,
                "items": set(),
            }

        buckets: Dict[str, Dict[str, Any]] = {route: blank(route) for route in named}
        unweighed: set = set()
        for row in rows:
            slot = buckets.setdefault(row["route"], blank(row["route"]))
            pieces = _f(row["pieces"])
            slot["pieces"] += pieces
            slot["boxes"] += _int(row.get("boxes"))
            slot["items"].add(row["item_code"])
            if row["litres"] is None:
                if weighed and pieces > 0:
                    unweighed.add(row["item_code"])
            else:
                slot["litres"] += float(row["litres"])

        ordered = [buckets[route] for route in named]
        extra = [slot for key, slot in buckets.items() if key not in named and slot["pieces"] > 0]
        if extra:
            ordered.append(
                {
                    "route": SHIFTING_ELSEWHERE,
                    "name": SHIFTING_ROUTE_NAMES[SHIFTING_ELSEWHERE],
                    "is_dispatch": False,
                    "pieces": sum(slot["pieces"] for slot in extra),
                    "litres": sum(slot["litres"] for slot in extra),
                    "boxes": sum(slot["boxes"] for slot in extra),
                    "items": {code for slot in extra for code in slot["items"]},
                    "codes": sorted({slot["route"] for slot in extra}),
                }
            )

        out = []
        for slot in ordered:
            out.append(
                {
                    **{key: value for key, value in slot.items() if key != "items"},
                    "pieces": round(slot["pieces"], 2),
                    "litres": round(slot["litres"], 3) if weighed else None,
                    "tons": _tons(slot["litres"]) if weighed else None,
                    "item_count": len(slot["items"]),
                }
            )

        total_litres = sum(slot["litres"] for slot in ordered)
        return {
            "boxes": sum(row["boxes"] for row in out),
            "total_pieces": round(sum(slot["pieces"] for slot in ordered), 2),
            "total_tons": _tons(total_litres) if weighed else None,
            # False only when SAP could not be reached: the tile then leads
            # with pieces and says why, rather than a zero that looks quiet.
            "tonnage_available": weighed,
            "routes": out,
            "unweighed_items": len(unweighed),
        }

    # -------------------------------------------------------------- workforce

    def _workforce(self) -> Dict[str, Any]:
        """Head count and wage bill per band, as typed on the factory's
        settings page (no system holds it).

        The six built-in departments first and in their order, then any the
        plant added (a saved row whose key is no built-in and carries a band
        of its own). MONTHLY IN, DAILY OUT: the wage bill is divided by the
        CALENDAR days in the month, because a wage is paid for the Sunday too.
        UNSET IS NOT ZERO: a band whose departments have no figure is null,
        drawn as a rule, and the departments with nothing typed are named.
        """
        saved: Dict[str, Dict[str, Any]] = {}
        for row in self.postgres.rows(
            marker("workforce")
            + """SELECT department, label, band, kind, employees, salary_monthly
FROM stock_dashboard_plantboardworkforce
WHERE company_code = %s
ORDER BY id""",
            [self.company_code],
        ):
            saved[row.get("department")] = row

        catalogue: List[Dict[str, Any]] = []
        for entry in WORKFORCE_DEPARTMENTS:
            catalogue.append({**entry, "saved": saved.get(entry["key"])})
        for key, row in saved.items():
            if key in BUILT_IN_DEPARTMENTS:
                continue
            if not row.get("band") or row.get("band") not in WORKFORCE_BANDS:
                continue
            catalogue.append(
                {
                    "key": key,
                    "label": row.get("label") or key,
                    "band": row.get("band"),
                    "kind": row.get("kind") if row.get("kind") in WORKFORCE_KINDS else "employee",
                    "saved": row,
                }
            )

        days_in_month = calendar.monthrange(self.today.year, self.today.month)[1]
        bands: Dict[str, Dict[str, Any]] = {
            band: {
                "employees": None,
                "employee_salary_monthly": None,
                "labour": None,
                "labour_salary_monthly": None,
                "departments": [],
            }
            for band in WORKFORCE_BANDS
        }
        unconfigured: List[str] = []
        heads: List[int] = []
        money: List[float] = []

        for entry in catalogue:
            row = entry["saved"] or {}
            people = None if row.get("employees") is None else _int(row.get("employees"))
            salary = formatting.f_or_none(row.get("salary_monthly"))
            if people is None and salary is None:
                unconfigured.append(entry["label"])
            if people is not None:
                heads.append(people)
            if salary is not None:
                money.append(salary)

            slot = bands[entry["band"]]
            slot["departments"].append(
                {
                    "key": entry["key"],
                    "label": entry["label"],
                    "kind": entry["kind"],
                    "employees": people,
                    "salary_monthly": salary,
                }
            )
            # Summed over what is configured only, so one unfilled department
            # cannot turn its whole half into a zero.
            head_key = "employees" if entry["kind"] == "employee" else "labour"
            money_key = "employee_salary_monthly" if entry["kind"] == "employee" else "labour_salary_monthly"
            if people is not None:
                slot[head_key] = (slot[head_key] or 0) + people
            if salary is not None:
                slot[money_key] = round((slot[money_key] or 0.0) + salary, 2)

        for slot in bands.values():
            slot["employee_cost_per_day"] = _per_day(slot["employee_salary_monthly"], days_in_month)
            slot["labour_cost_per_day"] = _per_day(slot["labour_salary_monthly"], days_in_month)

        total_salary = round(sum(money), 2) if money else None
        return {
            "days_in_month": days_in_month,
            "bands": bands,
            "total_people": sum(heads) if heads else None,
            "total_salary_monthly": total_salary,
            "total_cost_per_day": _per_day(total_salary, days_in_month),
            "unconfigured": unconfigured,
            "basis": (
                "Typed on the board's settings page; no system holds it. The monthly wage bill is "
                f"divided by the {days_in_month} calendar days in this month."
            ),
        }
