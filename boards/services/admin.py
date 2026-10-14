"""
The Admin Control board: the factory's summary wall, composed here. A port of
the factory's ``admin_board`` (``services.py``, ``hana_reader.py``,
``constants.py``, ``tonnage.py``, ``alerts.py``), section for section.

Three bands, in the order an owner asks about them: what the plant MADE and
SHIPPED, what is STANDING in it, and what it COST; then an action centre that
turns the first three into things to do.

WHERE EACH TILE READS
---------------------
* **Total production** (SAP, the board's company). Every production receipt
  (``OINM`` TransType 59) onto the finished floor ``BH-PF``, litres at
  1,000 L = 1 T, against the month's plan (``OFCT``/``FCT1``) and the plan's
  own actuals. The headline is the whole floor, planned or not: a fifth of
  September's output was items the plan never listed.
* **Total dispatch** (factory database, SAP for one line). Trucks that left
  the gate (``gate_core_salesdispatchgateout``, DISPATCHED), Oil and Mart
  ADDED together, the same register the Logistics board reads. Beside it,
  what was BILLED (``OINV``/``INV1`` on both companies' schemas,
  intercompany customers excluded per company): the gap is the useful number.
* **FG storage** (SAP on Oil and Mart, factory database for the ratings).
  Item group 102 in BH-BT (Oil) and the Gupta godown GP-FGM (Mart), weighed
  by gross case weight; GP-FG is real stock nobody has rated and is named
  beside the total, never inside it.
* **PM storage** (SAP). Packaging at value, and the floor it stands on
  bridged pieces -> pallets -> square feet by the factory's stacking sheet.
* **Oil storage** (SAP, factory database for the rating). The loose-oil tank
  farm BH-LO, in litres, because the tanks are stocked by volume.
* **Factory cost** (factory database only): ``admin_cost.AdminCost``.

Every section is read on its own and degrades on its own (``sections``).
The alerts are computed here, not on the screen: the thresholds are what a
business argues about, and they belong where they can be tested.

NOT PORTED
----------
The EXIM tank-farm read (the factory board works without it: SAP's litres
are the oil tile's figure and the farm's own rating is not shown), and the
two drill-down panels (tank farm, cost rows): a wall has nobody to click.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .. import formatting
from ..sections import SectionBuilder
from ..sources import SAPConnectionError, SAPDataError
from ..sources.hana import LITRES_PER_UNIT
from .admin_alerts import build_alerts
from .admin_cost import AdminCost
from .stacking import MEASURED_ON, PIECES_PER_PALLET
from .admin_tonnage import FINISHED_ITEM_GROUP, litres as sum_litres, roll_up

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (admin_board/constants.py, with the factory's reasoning)
# ---------------------------------------------------------------------------

#: Seconds between reads.
REFRESH_SECONDS = 60

#: Where the plant is, for the topbar.
PLANT_NAME = "Bhakharpur plant"

#: The production-finished floor. Output is counted as it is received here.
PRODUCTION_FLOOR = "BH-PF"

#: A production receipt: the only inbound that counts as "produced". A goods
#: receipt (16) or a transfer in (67) is stock arriving, not stock made.
TRANS_TYPE_PRODUCTION_RECEIPT = 59

#: The finished-goods stores the FG tile reports, and whose company owns each.
#: BH-BT is Oil's; the Gupta godown is Mart's (its stock is Mart's even though
#: the labour working it is Oil's). A reader asking "how much finished goods
#: is on site" means both, so the tile sums across the company boundary.
FG_STORES = (
    {"warehouse": "BH-BT", "company": "JIVO_OIL", "label": "BH-BT"},
    {"warehouse": "GP-FGM", "company": "JIVO_MART", "label": "Gupta"},
)

#: Finished goods standing in a warehouse nobody has rated. The Gupta godown
#: is GP-FGM and only GP-FGM (confirmed by the warehouse on 15 Sep 2026);
#: GP-FG is a different warehouse on no storage sheet, reported as a figure
#: and an alert and never folded into the rated total.
UNRATED_FG_WAREHOUSES = (
    {"warehouse": "GP-FG", "company": "JIVO_OIL", "label": "GP-FG (Oil, Gupta basement)"},
)

#: The packaging stores the PM tile values: the four the Plant board's floor
#: table covers, so the two boards cannot disagree about "PM storage".
PM_STORES = ("BH-PM", "BH-BS", "BH-NM", "BH-PC")

#: The loose-oil tank farm.
OIL_TANK = "BH-LO"

#: The packaging floor, as the factory measured it (plant_board/constants.py).
PACKAGING_FLOOR_BLOCKS = (
    {"label": "BH-PM, BH-NM, BH-BS", "sqft": 16900},
    {"label": "BH-PM", "sqft": 15000},
    {"label": "BH-PC", "sqft": 7000},
)
PACKAGING_FLOOR_SQFT = sum(block["sqft"] for block in PACKAGING_FLOOR_BLOCKS)

#: The floor one pallet stands on: a measurement, used until the Plant
#: board's settings say otherwise.
DEFAULT_SQFT_PER_PALLET = 15.0

#: The companies whose dispatch the tile ADDS together. A sum, not a
#: comparison.
DISPATCH_COMPANIES = ("JIVO_OIL", "JIVO_MART")

#: CardCodes that are the group selling to itself, per company and never
#: merged: CUSTA000906 is a group company to Oil and a real customer to Mart,
#: and a combined list strips 64 t of genuine Mart sales.
INTERCOMPANY_CARD_CODES = {
    "JIVO_OIL": (
        "CUSTA000001", "CUSTA000002", "CUSTA000003", "CUSTA000004", "CUSTA000606",
        "CUSTA000827", "CUSTA000906", "CUSTA001099", "CUSTA001113",
    ),
    "JIVO_MART": (
        "CUSTA000001", "CUSTA000827", "CUSTA000874", "CUSTA000875", "CUSTA000876",
        "CUSTA000877", "CUSTA000878", "CUSTA000926",
    ),
    "JIVO_BEVERAGES": (
        "CUSTA000001", "CUSTA000002", "CUSTA000003", "CUSTA000004", "CUSTA000606",
        "CUSTA000827",
    ),
}

#: The gate-out status that means the truck has gone.
DISPATCHED = "DISPATCHED"

#: How many plans are looked at to find this month's.
PLAN_LIST_LIMIT = 24

#: How many days of production the trend carries.
TREND_DAYS = 7

#: Item codes per IN list, as the plan reader chunks them.
CHUNK = 400

#: Why the oil tile has no tank-farm rating: EXIM is not read from here.
TANK_FARM_REASON = "The tank farm register is not connected to this server."

TONNAGE_BASIS = (
    "1000 litres = 1 ton, applied to oil and finished goods only. Packing "
    "material carries no litre volume in SAP and is reported at value."
)

# ---------------------------------------------------------------------------
# SQL. Every statement opens with ``-- admin: <name>``: it names the read in
# the database's activity view, and it is what the tests' fakes match on.
# HANA tables are qualified through ``hana.table()`` and bound with ``?``;
# the factory database's are Django's ``<app>_<model>`` and bound with %s.
# ---------------------------------------------------------------------------

SQL_COMPANIES = """-- admin: companies
SELECT id, code, name FROM company_company ORDER BY id
"""

#: Plan headers, newest first. The plan is the sales forecast (OFCT/FCT1,
#: keyed AbsID), not OWOR: there is no DocEntry and no PlannedQty.
SQL_PLANS = """-- admin: plans
SELECT H."AbsID", H."Code", H."Name", H."StartDate", H."EndDate"
FROM {ofct} H
ORDER BY H."AbsID" DESC
LIMIT {limit}
"""

SQL_PLAN_LINES = """-- admin: plan_lines
SELECT L."ItemCode", L."Quantity" AS "PlannedQty", {litres} AS "LitresPerUnit"
FROM {fct1} L
LEFT JOIN {oitm} M ON M."ItemCode" = L."ItemCode"
WHERE L."AbsID" = ?
"""

#: What was produced against the plan, per item, over the plan's own window,
#: in the item's inventory unit (the plan's unit).
SQL_PLAN_PRODUCED = """-- admin: plan_produced
SELECT N."ItemCode", SUM(N."InQty") AS "ProducedQty"
FROM {oinm} N
WHERE N."TransType" = {trans_type}
  AND N."InQty" > 0
  AND N."DocDate" >= ?
  AND N."DocDate" <= ?
  AND N."ItemCode" IN ({codes})
GROUP BY N."ItemCode"
"""

#: Output onto the finished floor, by day. Days that made nothing are absent,
#: which is what makes "average per producing day" computable.
SQL_PRODUCTION_DAYS = """-- admin: production_days
SELECT N."DocDate" AS "Day",
       SUM(N."InQty") AS "Pieces",
       SUM(N."InQty" * {litres}) AS "Litres"
FROM {oinm} N
JOIN {oitm} M ON M."ItemCode" = N."ItemCode"
WHERE N."TransType" = ?
  AND N."Warehouse" = ?
  AND N."DocDate" >= ?
  AND N."DocDate" <= ?
GROUP BY N."DocDate"
ORDER BY N."DocDate"
"""

#: Billed volume by day for one company. ``CANCELED`` is upper-case on OINV.
SQL_INVOICED = """-- admin: invoiced
SELECT H."DocDate" AS "Day",
       COUNT(DISTINCT H."DocEntry") AS "Bills",
       SUM(L."Quantity" * {litres}) AS "Litres"
FROM {oinv} H
JOIN {inv1} L ON L."DocEntry" = H."DocEntry"
LEFT JOIN {oitm} M ON M."ItemCode" = L."ItemCode"
WHERE H."CANCELED" = 'N'
  AND H."DocDate" >= ?
  AND H."DocDate" <= ?
  AND H."CardCode" NOT IN ({excluded})
GROUP BY H."DocDate"
ORDER BY H."DocDate"
"""

#: Whether this company's item master carries the gross-weight user field.
#: Naming a column a schema lacks fails the whole statement, so it is probed.
SQL_ITEM_COLUMNS = """-- admin: item_columns
SELECT "COLUMN_NAME" FROM "SYS"."TABLE_COLUMNS"
WHERE "SCHEMA_NAME" = ? AND "TABLE_NAME" = ?
"""

#: One row per SKU holding stock in the warehouses, with the pack fields the
#: weighing needs. Rows with no stock are dropped; a negative is kept.
SQL_OCCUPANCY = """-- admin: occupancy
SELECT w."WhsCode" AS "Warehouse",
       w."ItemCode",
       i."ItemName",
       COALESCE(w."OnHand", 0) AS "OnHand",
       COALESCE(i."SalFactor2", 0) AS "PiecesPerBox",
       COALESCE(i."SalPackUn", 0) AS "LitresPerPiece",
       COALESCE(w."StockValue", 0) AS "StockValue",
       COALESCE(i."InvntryUom", '') AS "Uom",
       {gross} AS "GrossWeightPerCase"
FROM {oitw} w
JOIN {oitm} i ON i."ItemCode" = w."ItemCode"
WHERE w."WhsCode" IN ({warehouses})
  AND COALESCE(w."OnHand", 0) <> 0
  {groups}
ORDER BY COALESCE(w."OnHand", 0) DESC
"""

#: The trucks that left the gate this month, per company.
SQL_DISPATCH_BY_COMPANY = """-- admin: dispatch_by_company
SELECT g.company_id,
       COALESCE(SUM(g.total_weight), 0) AS weight,
       COUNT(g.id) AS trucks,
       COUNT(DISTINCT g.dispatch_plan_id) AS bills
FROM gate_core_salesdispatchgateout g
WHERE {where}
GROUP BY g.company_id
"""

#: The same, whole. Bills are distinct on the plan: a truck carrying four
#: invoices is four bills but one truck. Days are the days a truck left.
SQL_DISPATCH_TOTAL = """-- admin: dispatch_total
SELECT COALESCE(SUM(g.total_weight), 0) AS weight,
       COUNT(g.id) AS trucks,
       COUNT(DISTINCT g.dispatch_plan_id) AS bills,
       COALESCE(SUM(CASE WHEN g.gate_out_date = %s THEN g.total_weight END), 0) AS today_weight,
       COUNT(DISTINCT g.gate_out_date) AS days
FROM gate_core_salesdispatchgateout g
WHERE {where}
"""

#: The rated tonnage somebody typed in on the Logistics or Plant board's
#: settings page, and the day stock was last physically counted. SAP records
#: no capacity anywhere.
SQL_WAREHOUSE_SETTINGS = """-- admin: warehouse_settings
SELECT company_code, warehouse, capacity_tonnes, last_audit_date
FROM stock_dashboard_warehouseboardsettings
WHERE company_code = ANY(%s) AND warehouse = ANY(%s)
"""

#: Square feet under one pallet, shared with the Plant board's settings so a
#: re-measured figure moves both screens.
SQL_PLANT_SETTINGS = """-- admin: plant_settings
SELECT sqft_per_pallet FROM stock_dashboard_plantboardsettings WHERE company_code = %s
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_f = formatting.f
_f_or_none = formatting.f_or_none
_tons = formatting.tons


def _pct(part: Optional[float], whole: Optional[float]) -> Optional[float]:
    """A percentage, or None where there is no denominator. Zero capacity is
    no capacity: an unrated warehouse and an empty one must not read alike."""
    if part is None or not whole:
        return None
    return round(part / whole * 100, 1)


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _litres(qty: Decimal, litres_per_unit: Decimal) -> Decimal:
    """Pieces x litres per piece, quantised to the millilitre as the plan
    service does: rounding per line drifts a 98-SKU plan by thousands."""
    if litres_per_unit <= 0:
        return Decimal(0)
    return (qty * litres_per_unit).quantize(Decimal("0.001"))


def _attainment(actual: Decimal, planned: Decimal) -> Decimal:
    if not planned:
        return Decimal(0)
    return ((actual / planned) * 100).quantize(Decimal("0.1"))


def _company_label(code: str) -> str:
    """"JIVO_OIL" -> "Jivo Oil", for the topbar. The factory page writes
    ``replace('JIVO_', 'Jivo ')``, which prints "Jivo OIL"; its own fallback
    is "Jivo Oil", which is what it meant."""
    if code.startswith("JIVO_"):
        return "Jivo " + code[len("JIVO_"):].replace("_", " ").title()
    return code


def _chunks(values: Sequence[Any], size: int = CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _placeholders(count: int) -> str:
    return ", ".join(["?"] * count)


class AdminBoardService(SectionBuilder):
    """The whole Admin Control board for one company, in one read."""

    REFRESH_SECONDS = REFRESH_SECONDS

    def __init__(
        self,
        company_code: str,
        *,
        postgres,
        hana_for: Callable[[str], Any],
        today: Optional[date] = None,
    ):
        """Nothing is read here: a source that is down degrades the sections
        that need it, never the constructor ``compose`` retries."""
        self.company_code = company_code
        self.postgres = postgres
        self.hana_for = hana_for
        self.hana = hana_for(company_code)
        self.today = today or formatting.today()
        self.month_first = self.today.replace(day=1)
        self.days_in_month = calendar.monthrange(self.today.year, self.today.month)[1]
        self._readers: Dict[str, Any] = {company_code: self.hana}
        self._item_columns: Dict[str, set] = {}
        self._companies: Optional[List[Dict[str, Any]]] = None
        self._init_sections()

    # ------------------------------------------------------------------ build

    def build(self) -> Dict[str, Any]:
        """Every tile, each absent-able on its own; then the alerts over them.

        Storage is read single-company first (PM, oil) and the two-company FG
        tile last: a SAP failure on Mart's schema latches SAP off for the rest
        of the build, and must not take the Oil-only tiles down with it.
        """
        production = self.section("production", self._production, needs_factory=False)
        # The gate register, so it survives a HANA outage; the billed line
        # inside it guards itself.
        dispatch = self.section("dispatch", self._dispatch, needs_sap=False)
        # The space settings it reads are tolerated missing, so it needs SAP
        # and nothing else.
        pm = self.section("pm_storage", self._pm_storage, needs_factory=False)
        oil = self.section("oil_storage", self._oil_storage)
        fg = self.section("fg_storage", self._fg_storage)
        cost = self.section("cost", self._cost, needs_sap=False)

        period = self._period()
        board: Dict[str, Any] = {
            "output": {"production": production, "dispatch": dispatch},
            "storage": {"fg": fg, "pm": pm, "oil": oil},
            "cost": cost,
        }
        # Last, because every rule reads the tiles above. A tile that failed
        # produces no alert rather than a false all-clear.
        board["alerts"] = self.section(
            "alerts",
            lambda: build_alerts({**board, "meta": {"period": period}}, today=self.today),
            needs_sap=False,
            needs_factory=False,
        )
        board["meta"] = {
            "company_code": self.company_code,
            "scope": f"{_company_label(self.company_code)} · {PLANT_NAME}",
            "date": self.today.isoformat(),
            "period": period,
            "refresh_seconds": self.REFRESH_SECONDS,
            "generated_at": formatting.now().isoformat(),
            "tonnage_basis": TONNAGE_BASIS,
            **self.section_meta(),
        }
        return board

    def _period(self) -> Dict[str, Any]:
        return {
            "from": self.month_first.isoformat(),
            "to": self.today.isoformat(),
            "day_of_month": self.today.day,
            "days_in_month": self.days_in_month,
            "elapsed_pct": round(self.today.day / self.days_in_month * 100, 1),
        }

    # -------------------------------------------------------------- plumbing

    def _reader(self, company_code: str):
        """One SAP reader per company: dispatch and FG storage span two
        schemas, and a schema is reached only as its company."""
        if company_code not in self._readers:
            self._readers[company_code] = self.hana_for(company_code)
        return self._readers[company_code]

    def _company_rows(self) -> List[Dict[str, Any]]:
        if self._companies is None:
            self._companies = [
                {"id": row.get("id"), "code": row.get("code"), "name": row.get("name") or ""}
                for row in self.postgres.rows(SQL_COMPANIES)
            ]
        return self._companies

    def _companies_by_code(self) -> Dict[str, Dict[str, Any]]:
        return {row["code"]: row for row in self._company_rows()}

    # ------------------------------------------------------------- tile 1: output

    def _production(self) -> Dict[str, Any]:
        """What the plant made this month, against what it said it would.

        The plan's totals and its own actuals are read against the SAME plan
        lines, so attainment is apples to apples. The headline is every
        receipt onto the finished floor, planned or not, so it reads above
        the plan's own actuals: unplanned output is output. The average
        divides by days that actually produced.
        """
        plan = self._resolve_plan()
        if not plan or not plan.get("abs_id"):
            self.warn(
                "No production plan is filed in SAP for this month, so output is "
                "reported without a target."
            )
            return self._production_without_plan()

        totals, lines = self._plan_detail(plan)
        planned_items_tons = _tons(totals["produced_litres"])
        plan_tons = _tons(totals["planned_litres"]) or None
        planned_items_pct = _f_or_none(totals["attainment_pct"])

        # Lines the tonnage cannot speak for: produced in pieces with no
        # litre volume. Counted rather than hidden.
        unweighed_lines = sum(
            1 for line in lines if line["produced_qty"] > 0 and not line["produced_litres"]
        )

        by_day = self._daily_production()
        producing_days = len([day for day, entry in by_day.items() if entry["tons"] > 0])
        today_tons = by_day.get(self.today, {}).get("tons", 0.0)
        mtd_tons = round(sum(entry["tons"] for entry in by_day.values()), 2)
        mtd_pieces = sum(entry["pieces"] for entry in by_day.values())
        unplanned_tons = round(max(mtd_tons - planned_items_tons, 0), 2)

        plan_to_date = round(plan_tons * self.today.day / self.days_in_month, 2) if plan_tons else None
        remaining_days = max(self.days_in_month - self.today.day, 0)
        required_rate = (
            round(max(plan_tons - mtd_tons, 0) / remaining_days, 2)
            if plan_tons and remaining_days > 0
            else None
        )

        return {
            "mtd_tons": mtd_tons,
            "mtd_pieces": round(mtd_pieces),
            "planned_items_tons": planned_items_tons,
            "planned_items_pct": planned_items_pct,
            "unplanned_tons": unplanned_tons,
            "today_tons": today_tons,
            "producing_days": producing_days,
            "avg_tons_per_producing_day": round(mtd_tons / producing_days, 2) if producing_days else None,
            "plan_tons": plan_tons,
            "plan_to_date_tons": plan_to_date,
            # The bar follows the headline: everything made over the target.
            "plan_pct": _pct(mtd_tons, plan_tons),
            "required_tons_per_day": required_rate,
            "remaining_days": remaining_days,
            "plan_name": plan.get("name"),
            "warehouse": PRODUCTION_FLOOR,
            "unweighed_lines": unweighed_lines,
            "basis": (
                "Every production receipt onto the finished floor, litres at "
                "1,000 L = 1 T — the same source and basis as the Plant board. "
                "Includes output the plan never listed, so it reads above the "
                "plan's own actuals; the target beside it is the plan's."
            ),
            "trend": self._trend(by_day),
        }

    def _resolve_plan(self) -> Optional[Dict[str, Any]]:
        """The plan whose dates contain today; failing that the newest, with
        a warning. Silently reporting last month's plan as this month's is
        the worst of the available outcomes."""
        sql = SQL_PLANS.format(ofct=self.hana.table("OFCT"), limit=int(PLAN_LIST_LIMIT))
        plans = []
        for row in self.hana.rows(sql):
            start = formatting.as_date(row.get("StartDate"))
            end = formatting.as_date(row.get("EndDate"))
            plans.append(
                {
                    "abs_id": row.get("AbsID"),
                    "code": row.get("Code") or "",
                    "name": row.get("Name") or "",
                    "start_date": start,
                    "end_date": end,
                    "is_current": bool(start and end and start <= self.today <= end),
                }
            )
        if not plans:
            return None
        current = next((plan for plan in plans if plan["is_current"]), None)
        if current is None:
            current = plans[0]
            self.warn(
                "No SAP plan covers today; showing the most recent plan "
                f"({current.get('name') or current.get('code')})."
            )
        return current

    def _plan_detail(self, plan: Dict[str, Any]):
        """The plan's litre totals and SAP's attainment on its own lines: the
        plan service's ``get_plan(include_actuals=True)``, cut to what this
        tile reads. Every line of a plan is priced at its item's litres."""
        sql = SQL_PLAN_LINES.format(
            litres=LITRES_PER_UNIT, fct1=self.hana.table("FCT1"), oitm=self.hana.table("OITM")
        )
        lines = []
        for row in self.hana.rows(sql, [int(plan["abs_id"])]):
            qty = _dec(row.get("PlannedQty"))
            per_unit = _dec(row.get("LitresPerUnit"))
            lines.append(
                {
                    "item_code": row.get("ItemCode") or "",
                    "planned_qty": qty,
                    "litres_per_unit": per_unit,
                    "planned_litres": _litres(qty, per_unit),
                }
            )

        produced: Dict[str, Decimal] = {}
        codes = [line["item_code"] for line in lines if line["item_code"]]
        if codes and plan.get("start_date") and plan.get("end_date"):
            try:
                produced = self._produced_quantities(codes, plan["start_date"], plan["end_date"])
            except SAPDataError as exc:
                # As the plan service does: a plan whose actuals SAP refused
                # is still a plan. A connection failure is not swallowed: it
                # latches SAP for the rest of the build.
                logger.warning("Admin board: plan actuals unreadable: %s", exc)
                produced = {}

        for line in lines:
            actual = produced.get(line["item_code"], Decimal(0))
            line["produced_qty"] = actual
            line["produced_litres"] = _litres(actual, line["litres_per_unit"])

        planned_total = sum((line["planned_qty"] for line in lines), Decimal(0))
        produced_total = sum((line["produced_qty"] for line in lines), Decimal(0))
        totals = {
            "planned_litres": sum((line["planned_litres"] for line in lines), Decimal(0)),
            "produced_litres": sum((line["produced_litres"] for line in lines), Decimal(0)),
            "attainment_pct": _attainment(produced_total, planned_total),
        }
        return totals, lines

    def _produced_quantities(self, codes: List[str], date_from: date, date_to: date) -> Dict[str, Decimal]:
        produced: Dict[str, Decimal] = {}
        for chunk in _chunks(codes):
            sql = SQL_PLAN_PRODUCED.format(
                oinm=self.hana.table("OINM"),
                trans_type=int(TRANS_TYPE_PRODUCTION_RECEIPT),
                codes=_placeholders(len(chunk)),
            )
            for row in self.hana.rows(sql, [date_from, date_to, *chunk]):
                produced[row.get("ItemCode")] = _dec(row.get("ProducedQty"))
        return produced

    def _daily_production(self) -> Dict[date, Dict[str, float]]:
        """Output per day from the movement ledger, keyed by real dates."""
        sql = SQL_PRODUCTION_DAYS.format(
            litres=LITRES_PER_UNIT, oinm=self.hana.table("OINM"), oitm=self.hana.table("OITM")
        )
        by_day: Dict[date, Dict[str, float]] = {}
        params = [TRANS_TYPE_PRODUCTION_RECEIPT, PRODUCTION_FLOOR, self.month_first, self.today]
        for row in self.hana.rows(sql, params):
            day = formatting.as_date(row.get("Day"))
            if day is not None:
                by_day[day] = {"tons": _tons(row.get("Litres")), "pieces": _f(row.get("Pieces"))}
        return by_day

    def _production_without_plan(self) -> Dict[str, Any]:
        """Output alone, when SAP holds no plan to measure it against."""
        by_day = self._daily_production()
        mtd_tons = round(sum(entry["tons"] for entry in by_day.values()), 2)
        producing_days = len([day for day, entry in by_day.items() if entry["tons"] > 0])
        return {
            "mtd_tons": mtd_tons,
            "mtd_pieces": round(sum(entry["pieces"] for entry in by_day.values())),
            "today_tons": by_day.get(self.today, {}).get("tons", 0.0),
            "producing_days": producing_days,
            "avg_tons_per_producing_day": round(mtd_tons / producing_days, 2) if producing_days else None,
            "planned_items_tons": None,
            "planned_items_pct": None,
            "unplanned_tons": None,
            "plan_tons": None,
            "plan_to_date_tons": None,
            "plan_pct": None,
            "required_tons_per_day": None,
            "remaining_days": max(self.days_in_month - self.today.day, 0),
            "plan_name": None,
            "warehouse": PRODUCTION_FLOOR,
            "unweighed_lines": 0,
            "basis": "Production receipts onto the finished floor, litres at 1,000 L = 1 T.",
            "trend": self._trend(by_day),
        }

    def _trend(self, by_day: Dict[date, Dict[str, float]]) -> List[Dict[str, Any]]:
        """The last few days, oldest first, INCLUDING the ones that made
        nothing: a shut Sunday is a gap a reader uses the strip to spot."""
        start = self.today - timedelta(days=TREND_DAYS - 1)
        days = []
        for offset in range(TREND_DAYS):
            day = start + timedelta(days=offset)
            days.append(
                {"date": day.isoformat(), "label": day.strftime("%d"), "tons": by_day.get(day, {}).get("tons", 0.0)}
            )
        return days

    def _dispatch(self) -> Dict[str, Any]:
        """What physically left the gate, month to date: the gate-out
        register, not SAP invoices, the same basis as the Logistics board.

        Beside a tile that counts a physical event, "what we billed" would
        compare a physical fact with a financial one. Both are kept: what left
        and what was billed, and the gap between them is the useful number.
        The Oil-to-Mart leg lives in the BST shifting register, which this
        never touches; the intercompany exclusion removes nothing today and
        guards against a future change routing transfers through this one.
        """
        companies = self._companies_by_code()
        company_ids: Dict[int, str] = {}
        for code in DISPATCH_COMPANIES:
            row = companies.get(code)
            if row is None:
                self.warn(
                    f"{code} is not configured on this deployment, so dispatch is "
                    "the remaining companies only."
                )
                continue
            company_ids[row["id"]] = code

        if not company_ids:
            raise LookupError("No dispatch company is configured in the factory database.")

        # Applied per company: a code that is a group entity to one and a
        # customer to the other is judged correctly.
        where = (
            "g.company_id = ANY(%s) AND g.gate_out_date >= %s AND g.gate_out_date <= %s "
            "AND g.status = %s"
        )
        params: List[Any] = [list(company_ids), self.month_first, self.today, DISPATCHED]
        for company_id, code in company_ids.items():
            excluded = list(INTERCOMPANY_CARD_CODES.get(code, ()))
            if excluded:
                where += " AND NOT (g.company_id = %s AND g.customer_code = ANY(%s))"
                params.extend([company_id, excluded])

        by_company = {
            row.get("company_id"): row
            for row in self.postgres.rows(SQL_DISPATCH_BY_COMPANY.format(where=where), params)
        }
        companies_out = []
        for company_id, code in company_ids.items():
            row = by_company.get(company_id) or {}
            companies_out.append(
                {
                    "company_code": code,
                    "tons": round(_f(row.get("weight")) / 1000, 2),
                    "trucks": int(row.get("trucks") or 0),
                    "bills": int(row.get("bills") or 0),
                }
            )

        total = self.postgres.one(SQL_DISPATCH_TOTAL.format(where=where), [self.today, *params]) or {}
        mtd_tons = round(_f(total.get("weight")) / 1000, 2)
        dispatch_days = int(total.get("days") or 0)

        return {
            "mtd_tons": mtd_tons,
            "today_tons": round(_f(total.get("today_weight")) / 1000, 2),
            "trucks": int(total.get("trucks") or 0),
            "bills": int(total.get("bills") or 0),
            "dispatch_days": dispatch_days,
            "avg_tons_per_dispatch_day": round(mtd_tons / dispatch_days, 2) if dispatch_days else None,
            "companies": companies_out,
            "invoiced_tons": self._invoiced_tons(set(company_ids.values())),
            "basis": (
                "Trucks that left the gate this month, weighed in kg — the same "
                "register and basis as the Logistics board. Includes bills raised "
                "in an earlier month, and excludes this month's bills that have "
                "not shipped yet."
            ),
        }

    def _invoiced_tons(self, configured: set) -> Optional[float]:
        """What was BILLED this month across both companies, intercompany
        excluded, for the comparison line. None rather than zero when SAP
        cannot answer: the headline does not depend on it, so a failure here
        costs the comparison line and nothing else."""
        if self._sap_down:
            return None
        try:
            total = 0.0
            for code in DISPATCH_COMPANIES:
                if code not in configured:
                    continue
                hana = self._reader(code)
                excluded = list(INTERCOMPANY_CARD_CODES.get(code, ()))
                sql = SQL_INVOICED.format(
                    litres=LITRES_PER_UNIT,
                    oinv=hana.table("OINV"),
                    inv1=hana.table("INV1"),
                    oitm=hana.table("OITM"),
                    excluded=_placeholders(len(excluded)) or "''",
                )
                rows = hana.rows(sql, [self.month_first, self.today, *excluded])
                total += sum(_tons(row.get("Litres")) for row in rows)
            return round(total, 2)
        except (SAPConnectionError, SAPDataError) as exc:
            logger.warning("Admin board: billed tonnage unreadable: %s", exc)
            return None

    # ------------------------------------------------------------ tile 2: storage

    def _gross_weight_expr(self, company_code: str) -> str:
        """Gross weight of one sales case in kg, or NULL where the company's
        item master has no ``U_Gross_Weight`` field: then every row is
        disclosed as unweighed rather than weighing nothing."""
        if company_code not in self._item_columns:
            hana = self._reader(company_code)
            try:
                rows = hana.rows(SQL_ITEM_COLUMNS, [hana.schema, "OITM"])
                self._item_columns[company_code] = {row.get("COLUMN_NAME") for row in rows}
            except SAPDataError as exc:
                # Unknown reads as "no user-defined columns"; the tile already
                # discloses how many rows it could not weigh.
                logger.warning("Admin board: could not probe OITM columns: %s", exc)
                self._item_columns[company_code] = set()
        if "U_Gross_Weight" not in self._item_columns[company_code]:
            return "CAST(NULL AS DECIMAL)"
        return 'NULLIF(COALESCE(i."U_Gross_Weight", 0), 0)'

    def _occupancy(self, company_code: str, warehouses: Sequence[str], finished_only: bool) -> Dict[str, List[Dict[str, Any]]]:
        """Stock rows per warehouse, scoped the way the Logistics board scopes
        them. ``finished_only`` is group 102; off only for packaging and the
        tank farm, which hold no finished goods at all."""
        hana = self._reader(company_code)
        oitw = hana.table("OITW")
        oitm = hana.table("OITM")
        sql = SQL_OCCUPANCY.format(
            gross=self._gross_weight_expr(company_code),
            oitw=oitw,
            oitm=oitm,
            warehouses=_placeholders(len(warehouses)),
            groups=f'AND i."ItmsGrpCod" IN ({int(FINISHED_ITEM_GROUP)})' if finished_only else "",
        )
        by_warehouse: Dict[str, List[Dict[str, Any]]] = {warehouse: [] for warehouse in warehouses}
        for row in hana.rows(sql, list(warehouses)):
            gross = row.get("GrossWeightPerCase")
            by_warehouse.setdefault(row.get("Warehouse"), []).append(
                {
                    "item_code": row.get("ItemCode") or "",
                    "item_name": row.get("ItemName") or "",
                    "on_hand": _f(row.get("OnHand")),
                    # None rather than 0 where SAP holds nothing, so the
                    # caller never divides by a zero that looks deliberate.
                    "pieces_per_box": _f(row.get("PiecesPerBox")) or None,
                    "litres_per_piece": _f(row.get("LitresPerPiece")) or None,
                    "stock_value": _f(row.get("StockValue")),
                    "uom": row.get("Uom") or "",
                    "gross_weight_per_case": _f(gross) if gross is not None else None,
                }
            )
        return by_warehouse

    def _warehouse_settings(self, pairs: Sequence[tuple]) -> Dict[tuple, Dict[str, Any]]:
        """``{(company, warehouse): {"capacity": float|None, "audit": iso|None}}``."""
        rows = self.postgres.rows(
            SQL_WAREHOUSE_SETTINGS,
            [sorted({company for company, _ in pairs}), sorted({warehouse for _, warehouse in pairs})],
        )
        found = {}
        for row in rows:
            key = (row.get("company_code"), row.get("warehouse"))
            if key in pairs:
                found[key] = {
                    "capacity": _f_or_none(row.get("capacity_tonnes")),
                    "audit": formatting.iso(formatting.as_date(row.get("last_audit_date"))),
                }
        return found

    def _fg_storage(self) -> Dict[str, Any]:
        """Finished goods on site: BH-BT (Oil) and the Gupta godown (Mart),
        weighed by gross case weight, group 102 only. Unrated stock is
        reported ALONGSIDE the total and never inside it."""
        stores = list(FG_STORES) + list(UNRATED_FG_WAREHOUSES)
        by_company: Dict[str, List[str]] = {}
        for store in stores:
            by_company.setdefault(store["company"], []).append(store["warehouse"])
        stock: Dict[tuple, List[Dict[str, Any]]] = {}
        for company, warehouses in by_company.items():
            for warehouse, rows in self._occupancy(company, warehouses, finished_only=True).items():
                stock[(company, warehouse)] = rows

        ratings = self._warehouse_settings([(store["company"], store["warehouse"]) for store in FG_STORES])

        rows = []
        total_tons = 0.0
        total_capacity = 0.0
        capacity_known = True
        unweighed = 0
        non_piece = 0
        for store in FG_STORES:
            weighed = roll_up(stock.get((store["company"], store["warehouse"]), []))
            tons = weighed["tonnes"]
            unweighed += weighed["unweighed_items"]
            non_piece += weighed["non_piece_items"]

            rating = ratings.get((store["company"], store["warehouse"])) or {}
            capacity = rating.get("capacity")
            if capacity is None:
                capacity_known = False
            else:
                total_capacity += capacity
            total_tons += tons
            rows.append(
                {
                    "warehouse": store["warehouse"],
                    "label": store["label"],
                    "company_code": store["company"],
                    "tons": tons,
                    "capacity_tons": capacity,
                    "used_pct": _pct(tons, capacity),
                    "free_tons": round(capacity - tons, 2) if capacity else None,
                    "last_audit_date": rating.get("audit"),
                    "unweighed_items": weighed["unweighed_items"],
                }
            )

        unrated = []
        for store in UNRATED_FG_WAREHOUSES:
            weighed = roll_up(stock.get((store["company"], store["warehouse"]), []))
            if weighed["tonnes"] > 0:
                unrated.append(
                    {
                        "warehouse": store["warehouse"],
                        "label": store["label"],
                        "company_code": store["company"],
                        "tons": weighed["tonnes"],
                    }
                )

        return {
            "unit": "tonnes",
            "total_tons": round(total_tons, 2),
            "capacity_tons": round(total_capacity, 2) if capacity_known else None,
            "used_pct": _pct(total_tons, total_capacity) if capacity_known else None,
            "free_tons": round(total_capacity - total_tons, 2) if capacity_known else None,
            "rows": rows,
            "unrated": unrated,
            "unweighed_items": unweighed,
            "non_piece_items": non_piece,
            "basis": (
                "Gross case weight (OITM.U_Gross_Weight / SalFactor2), finished "
                "goods only — the same basis and scope as the Logistics board."
            ),
        }

    def _pm_storage(self) -> Dict[str, Any]:
        """Packaging: what it is worth, and how much floor it stands on.

        VALUE, NOT TONNES: packaging has no litre volume and no case weight.
        Floor is bridged pieces -> pallets per ITEM by the stacking sheet (300
        five-litre bottles fill a pallet, 60,000 caps fill one), then pallets
        -> square feet by the Plant board's footprint. An item with no pallet
        figure is counted in the value and left out of the floor, and the
        count is disclosed: every one makes the stores read EMPTIER.
        """
        stock = self._occupancy(self.company_code, PM_STORES, finished_only=False)

        rows = []
        total_value = 0.0
        total_pieces = 0.0
        total_pallets = 0.0
        unmeasured_codes = set()
        unmeasured_pieces = 0.0
        for warehouse in PM_STORES:
            value = 0.0
            pieces = 0.0
            pallets = 0.0
            for row in stock.get(warehouse, []):
                on_hand = float(row.get("on_hand") or 0)
                value += float(row.get("stock_value") or 0)
                pieces += on_hand
                factor = PIECES_PER_PALLET.get((row.get("item_code") or "").strip())
                if factor:
                    pallets += on_hand / factor
                else:
                    unmeasured_codes.add(row.get("item_code"))
                    unmeasured_pieces += on_hand
            total_value += value
            total_pieces += pieces
            total_pallets += pallets
            rows.append(
                {
                    "warehouse": warehouse,
                    "label": warehouse,
                    "value": round(value, 2),
                    "pieces": round(pieces),
                    "pallets": round(pallets, 1),
                }
            )
        rows.sort(key=lambda entry: entry["value"], reverse=True)

        sqft_per_pallet = self._sqft_per_pallet()
        occupied_sqft = free_sqft = used_pct = None
        if sqft_per_pallet:
            occupied_sqft = round(total_pallets * sqft_per_pallet, 1)
            # Free floored at zero; the percentage is left uncapped so an
            # overfill still shows.
            free_sqft = round(max(0.0, PACKAGING_FLOOR_SQFT - occupied_sqft), 1)
            used_pct = round(occupied_sqft / PACKAGING_FLOOR_SQFT * 100, 1)

        if unmeasured_codes:
            self.warn(
                f"{len(unmeasured_codes)} packaging items have no pallet figure on the "
                "stacking sheet, so the floor used is understated."
            )

        return {
            "unit": "value",
            "total_value": round(total_value, 2),
            "total_pieces": round(total_pieces),
            "pallets": round(total_pallets, 1),
            "floor_sqft": PACKAGING_FLOOR_SQFT,
            "occupied_sqft": occupied_sqft,
            "free_sqft": free_sqft,
            "used_pct": used_pct,
            "sqft_per_pallet": sqft_per_pallet,
            "blocks": [dict(block) for block in PACKAGING_FLOOR_BLOCKS],
            "unmeasured_items": len(unmeasured_codes),
            "unmeasured_pieces": round(unmeasured_pieces),
            "stacking_measured_on": MEASURED_ON,
            "capacity_tons": None,
            "no_capacity_reason": (
                "Rated in square feet, not tonnes — packaging has no litre volume "
                "and no case weight."
            ),
            "basis": (
                "Stock value, with floor use bridged pieces → pallets → square feet "
                "by the factory's own stacking sheet — the Plant board's method."
            ),
            "rows": rows,
        }

    def _sqft_per_pallet(self) -> Optional[float]:
        """Square feet under one pallet, from the Plant board's settings.

        An explicit null means somebody CLEARED it, and is honoured. A missing
        row is the measured default; so is a settings read that fails, which
        must not take out a tile that has nothing to do with the setting.
        """
        if self._factory_down:
            return DEFAULT_SQFT_PER_PALLET
        try:
            row = self.postgres.one(SQL_PLANT_SETTINGS, [self.company_code])
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("Admin board: space settings unreadable: %s", exc)
            return DEFAULT_SQFT_PER_PALLET
        if row is None:
            return DEFAULT_SQFT_PER_PALLET
        value = row.get("sqft_per_pallet")
        return float(value) if value is not None else None

    def _oil_storage(self) -> Dict[str, Any]:
        """Loose oil in the tank farm, the one tile still in litres.

        The tanks are stocked in LTR, so the on-hand figure IS the volume and
        1,000 L = 1 T applies with no case weight. Every item group, because
        the farm holds raw oil, not group 102. The farm's own register (EXIM)
        is not read from this service: SAP is the source, and the reason the
        tank farm has no rating of its own travels with the tile.
        """
        occupancy = self._occupancy(self.company_code, [OIL_TANK], finished_only=False).get(OIL_TANK, [])
        volume = sum_litres(occupancy)
        sap_tons = _tons(volume)
        rating = self._warehouse_settings([(self.company_code, OIL_TANK)]).get((self.company_code, OIL_TANK)) or {}
        capacity = rating.get("capacity")

        by_item = sorted(
            (
                {
                    "label": row.get("item_name") or row.get("item_code") or "",
                    "tons": _tons(
                        float(row.get("on_hand") or 0) * float(row.get("litres_per_piece") or 1)
                        if row.get("uom") and row.get("litres_per_piece")
                        else float(row.get("on_hand") or 0)
                    ),
                }
                for row in occupancy
            ),
            key=lambda entry: entry["tons"],
            reverse=True,
        )

        return {
            "unit": "tonnes",
            "warehouse": OIL_TANK,
            "total_tons": sap_tons,
            "total_litres": round(volume),
            "capacity_tons": capacity,
            "used_pct": _pct(sap_tons, capacity),
            "no_capacity_reason": TANK_FARM_REASON,
            "source": "SAP",
            "sap_tons": sap_tons,
            "tank_rows": [],
            "by_type": {},
            "excluded": {},
            "basis": (
                "Litres at 1,000 L = 1 T. The tanks are stocked by volume, so no "
                "case weight applies — unlike the finished-goods tiles."
            ),
            "rows": by_item[:4],
        }

    # --------------------------------------------------------------- tile 3: cost

    def _cost(self) -> Dict[str, Any]:
        """The month's spend as four lines, off the factory's registers."""
        return AdminCost(
            self.postgres,
            self._company_rows(),
            month_first=self.month_first,
            today=self.today,
            warn=self.warn,
        ).build()
