"""
The Admin board's cost tile: the month's spend as four lines, read off the
factory's own registers. No SAP.

A port of three pieces of the factory: ``admin_board.services._cost`` and its
two helpers (``_labour_departments``, ``_electricity_oil``), and the parts
of ``factory_expense`` (``services.build_board``, ``rates``) whose figures the
tile keeps: the salary accrual and maintenance. Where the tile overrides the
Factory Expense wall board's line (labour, electricity) only the override is
ported; computing the wall's figure only to throw it away would cost two
reads a minute and decide nothing.

WHERE EACH LINE COMES FROM
--------------------------
* **Labour.** ``labour_gate_labourgateentry`` rows booked to five named
  departments (the HOD's split, never the barrier tally that re-describes
  the same people), priced from the Cost Master's ``factory-labour`` rates.
  A subset of the gate by design, and the line says what share.
* **Electricity.** ``maintenance_dailyelectricityreading`` for the meters
  tagged Jivo Oil, mains included. A meter shared with Beverages counts in
  full: the register keeps one reading per meter per day with nothing to
  split it by.
* **Salary.** The Cost Master's ``factory-salary`` PER_MONTH rates, spread
  evenly over the month's days and charged for the days elapsed: an accrual,
  so on the 15th of a 30-day month it is half the bill.
* **Maintenance.** Spares issued or consumed at their unit cost, plus
  material indents once a quotation has been selected.

A line with nothing configured behind it reports zero WITH its warning and
``has_source: False``; a line genuinely nil for the month has no warning.
The donut draws the two differently and only the first raises an alert.

TODAY, PER LINE
---------------
The factory page prints each line's figure for today under the month's, and
the month's average per day under the legend. ``today`` comes off the same
pass as the month, on the same basis, so the two on one line can be compared.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from django.conf import settings

ZERO = Decimal("0.00")

# ---------------------------------------------------------------------------
# The four lines (admin_board/constants.py)
# ---------------------------------------------------------------------------

#: In fixed order, mapped to the Factory Expense wall board's buckets. The
#: fourth was called "Others" until 2026-09-15 and is electricity and nothing
#: else; it carries its own name now.
COST_SLICES = (
    {"key": "labour", "bucket": "LABOUR", "label": "Labour"},
    {"key": "electricity", "bucket": "ELECTRICITY", "label": "Electricity"},
    {"key": "salary", "bucket": "SALARY", "label": "Salary"},
    {"key": "maintenance", "bucket": "MAINTENANCE", "label": "Maintenance"},
)

#: The departments the labour line prices, matched by name without regard to
#: case (the live master spells it "production(oil)"), because department IDs
#: differ between deployments. A name with nothing behind it is a warning.
LABOUR_DEPARTMENTS = (
    "production(oil)",
    "Warehouse Basement",
    "Dock",
    "Scrap",
    "Boiling Floor 1",
)

#: Whose meters the electricity line prices. The register is campus-wide;
#: this board is Jivo Oil's.
ELECTRICITY_COMPANY = "JIVO_OIL"

# ---------------------------------------------------------------------------
# The Factory Expense board's vocabulary (factory_expense/constants.py)
# ---------------------------------------------------------------------------

#: The Cost Master types the lines price from, unless the first company's
#: expense settings name others.
LABOUR_COST_TYPE_CODE = "factory-labour"
SALARY_COST_TYPE_CODE = "factory-salary"

#: Scope precedence when several Cost Master rows could price the same thing.
SCOPE_SPECIFICITY = {"FACTORY": 0, "COMPANY": 1, "DEPARTMENT": 2, "VALUE": 3}

#: Spare movements that are money leaving the store. RECEIPT adds stock,
#: RETURN gives it back, ADJUSTMENT is a stock correction.
MAINTENANCE_SPEND_MOVEMENTS = ("ISSUE", "CONSUME")

#: Indent statuses that are committed money: approved, a quotation chosen or
#: the goods already moving.
MAINTENANCE_COMMITTED_INDENT_STATUSES = ("QUOTATION_SELECTED", "PURCHASED", "GATE_IN", "RECEIVED")

#: ``FactoryExpenseSettings``' defaults, for a company nobody has configured.
#: The factory creates the row on first read; this service writes nothing,
#: so it uses the same defaults without the row.
DEFAULT_EXPENSE_SETTINGS = {
    "labour_cost_type_code": LABOUR_COST_TYPE_CODE,
    "salary_cost_type_code": SALARY_COST_TYPE_CODE,
    "maintenance_include_spares": True,
    "maintenance_include_indents": True,
}

ELECTRICITY_NOTE = (
    "Jivo Oil's meters only, mains included — the sub-meters measure the same "
    "supply again, and the five shared with Beverages count in full, so this "
    "runs well above the metered bill."
)

# ---------------------------------------------------------------------------
# SQL. Tables follow Django's ``<app>_<model>`` naming; ``is_active`` is the
# factory's BaseModel soft-delete flag.
# ---------------------------------------------------------------------------

SQL_EXPENSE_SETTINGS = """-- admin: expense_settings
SELECT labour_cost_type_code, salary_cost_type_code,
       maintenance_include_spares, maintenance_include_indents
FROM factory_expense_factoryexpensesettings
WHERE company_id = %s
"""

#: Every Cost Master row for one type that could apply on the day: the
#: type's newest active definition (the factory's ``.first()`` by name), rows
#: effective on or before the day, company-agnostic or for one of the
#: companies. Ranked in Python, exactly as ``factory_expense.rates`` does.
SQL_RATES = """-- admin: cost_rates
SELECT r.id, r.scope, r.company_id, r.department_id, d.name AS department_name,
       r.basis, r.rate, r.effective_from
FROM cost_master_costrate r
LEFT JOIN accounts_department d ON d.id = r.department_id
WHERE r.cost_type_id = (
        SELECT t.id FROM cost_master_costtype t
        WHERE t.code = %s AND t.is_active
        ORDER BY t.name, t.id
        LIMIT 1
      )
  AND r.is_active
  AND r.effective_from <= %s
  AND (r.company_id IS NULL OR r.company_id = ANY(%s))
"""

SQL_LABOUR_DEPARTMENTS = """-- admin: labour_departments
SELECT id, name
FROM accounts_department
WHERE LOWER(name) = ANY(%s)
ORDER BY id
"""

#: The HOD's split rows for the named departments. Ordered the way the
#: factory's model orders them, because a flat PER_DAY rate is charged to
#: the first entry of its day and the order decides which one that is.
SQL_LABOUR_ENTRIES = """-- admin: labour_entries
SELECT e.company_id, e.department_id, e.work_date, e.count_in
FROM labour_gate_labourgateentry e
WHERE e.department_id = ANY(%s)
  AND e.company_id = ANY(%s)
  AND e.work_date >= %s AND e.work_date <= %s
  AND e.is_active
ORDER BY e.work_date DESC, e.shift, e.department_id, e.contractor_id
"""

#: What walked through the barrier over the same span: for the coverage line
#: only, never added to the figure.
SQL_LABOUR_GATE = """-- admin: labour_gate
SELECT COALESCE(SUM(count_in), 0) AS heads
FROM labour_gate_labourgateentry
WHERE department_id IS NULL
  AND company_id = ANY(%s)
  AND work_date >= %s AND work_date <= %s
  AND is_active
"""

#: Readings on the meters tagged with one company, per meter per day. A
#: meter tagged with several companies is one reading, counted once.
SQL_ELECTRICITY = """-- admin: electricity
SELECT r.date, m.name AS meter,
       COALESCE(SUM(r.units_consumed), 0) AS units,
       COALESCE(SUM(r.total_cost), 0) AS cost
FROM maintenance_dailyelectricityreading r
JOIN maintenance_electricitymeter m ON m.id = r.meter_id
WHERE r.is_active
  AND r.date >= %s AND r.date <= %s
  AND EXISTS (
        SELECT 1 FROM maintenance_electricitymeter_companies mc
        WHERE mc.electricitymeter_id = r.meter_id AND mc.company_id = %s
      )
GROUP BY r.date, m.name
"""

#: Spares out of the store, one row per movement so each is quantised to the
#: paisa exactly as the factory quantises it. The day is the factory's local
#: day the movement was recorded on.
SQL_SPARES = """-- admin: maintenance_spares
SELECT (s.created_at AT TIME ZONE %s)::date AS day, s.quantity, s.unit_cost
FROM maintenance_sparemovement s
WHERE s.company_id = ANY(%s)
  AND s.movement_type = ANY(%s)
  AND (s.created_at AT TIME ZONE %s)::date >= %s
  AND (s.created_at AT TIME ZONE %s)::date <= %s
  AND s.is_active
"""

#: Committed indents at their selected quotation's total: every quoted line's
#: quantity times its price, plus the quotation's other charges.
SQL_INDENTS = """-- admin: maintenance_indents
SELECT i.indent_date AS day,
       q.other_charges,
       COALESCE((
         SELECT SUM(COALESCE(l.quantity, 0) * COALESCE(l.unit_price, 0))
         FROM maintenance_materialindentquotationline l
         WHERE l.quotation_id = q.id
       ), 0) AS lines_total
FROM maintenance_materialindent i
JOIN maintenance_materialindentquotation q ON q.id = i.selected_quotation_id
WHERE i.company_id = ANY(%s)
  AND i.indent_date >= %s AND i.indent_date <= %s
  AND i.status = ANY(%s)
  AND i.is_active
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money(value: Any) -> Decimal:
    """Quantised to the paisa, as the factory's expense board does it."""
    return _dec(value).quantize(Decimal("0.01"))


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct(part: Optional[float], whole: Optional[float]) -> Optional[float]:
    if part is None or not whole:
        return None
    return round(part / whole * 100, 1)


def _lakhs(amount: float) -> str:
    """A rupee figure the way the board prints one: lakhs, then crores."""
    if abs(amount) >= 1_00_00_000:
        return f"₹{amount / 1_00_00_000:.2f} Cr"
    return f"₹{amount / 1_00_000:.2f} L"


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if hasattr(value, "date") and callable(value.date):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The Cost Master (factory_expense/rates.py)
# ---------------------------------------------------------------------------


def _rank(rate: Dict[str, Any], department_id: Optional[int]):
    """How hard a row competes. Higher wins; None means it does not apply."""
    scope = rate["scope"]
    if scope == "FACTORY":
        pass
    elif scope == "COMPANY":
        if rate["company_id"] is None:
            return None
    elif scope == "DEPARTMENT":
        if department_id is None or rate["department_id"] != department_id:
            return None
    else:
        # VALUE rows are keyed to something this board does not identify.
        return None
    return (
        SCOPE_SPECIFICITY[scope],
        1 if rate["company_id"] is not None else 0,
        rate["effective_from"],
        rate["id"],
    )


def resolve(rates: Sequence[Dict[str, Any]], department_id: Optional[int], on_date: date):
    """The one row that prices a thing on ``on_date``, or None."""
    best = None
    best_rank = None
    for rate in rates:
        if rate["effective_from"] > on_date:
            continue
        rank = _rank(rate, department_id)
        if rank is None:
            continue
        if best_rank is None or rank > best_rank:
            best, best_rank = rate, rank
    return best


def price_labour(rate: Dict[str, Any], headcount: int) -> Decimal:
    """What one gate entry costs, by the rate's own basis.

    ``PER_PERSON_DAY`` multiplies by the head count; ``PER_DAY`` is a flat
    daily charge that does not care how many people came; ``PER_MONTH`` is a
    thirtieth of the month. Anything else cannot price a head count.
    """
    amount = _dec(rate["rate"])
    basis = rate["basis"]
    if basis == "PER_PERSON_DAY":
        return amount * headcount
    if basis == "PER_DAY":
        return amount if headcount else ZERO
    if basis == "PER_MONTH":
        return amount / 30 if headcount else ZERO
    return ZERO


def monthly_amounts_by_department(rates: Sequence[Dict[str, Any]], on_date: date) -> List[Tuple]:
    """``[(department_id, department_name, amount, rate_id), ...]`` for a
    PER_MONTH type. Department rows win outright when any is in force: a
    company-wide blanket on top would count the same bill twice."""
    department_rows: Dict[int, Tuple] = {}
    blanket = None
    blanket_rank = None

    for rate in rates:
        if rate["effective_from"] > on_date:
            continue
        if rate["scope"] == "DEPARTMENT" and rate["department_id"]:
            rank = (1 if rate["company_id"] is not None else 0, rate["effective_from"], rate["id"])
            current = department_rows.get(rate["department_id"])
            if current is None or rank > current[0]:
                department_rows[rate["department_id"]] = (rank, rate)
        elif rate["scope"] in ("FACTORY", "COMPANY"):
            rank = (
                SCOPE_SPECIFICITY[rate["scope"]],
                1 if rate["company_id"] is not None else 0,
                rate["effective_from"],
                rate["id"],
            )
            if blanket_rank is None or rank > blanket_rank:
                blanket, blanket_rank = rate, rank

    if department_rows:
        return [
            (rate["department_id"], rate["department_name"] or "", _dec(rate["rate"]), rate["id"])
            for _, rate in sorted(department_rows.values(), key=lambda pair: pair[1]["department_name"] or "")
        ]
    if blanket is not None:
        return [(None, "All departments", _dec(blanket["rate"]), blanket["id"])]
    return []


# ---------------------------------------------------------------------------
# The tile
# ---------------------------------------------------------------------------


class AdminCost:
    """The cost tile for one month to date, over every company.

    ``companies`` is every row of ``company_company`` (id, code, name) in id
    order: the factory reads ``Company.objects.all()``, and its first company
    is the one whose expense settings the lines obey. ``warn`` takes a
    board-level caveat (a department the board names but the site lacks).
    """

    def __init__(
        self,
        postgres,
        companies: Sequence[Dict[str, Any]],
        *,
        month_first: date,
        today: date,
        warn: Callable[[str], None],
    ):
        self.postgres = postgres
        self.companies = list(companies)
        self.company_ids = [company["id"] for company in self.companies]
        self.month_first = month_first
        self.today = today
        self.days_in_month = calendar.monthrange(today.year, today.month)[1]
        self.warn = warn

    # ------------------------------------------------------------- plumbing

    def _settings(self) -> Dict[str, Any]:
        """The first company's expense settings, or the model's defaults."""
        row = self.postgres.one(SQL_EXPENSE_SETTINGS, [self.company_ids[0]]) or {}
        merged = dict(DEFAULT_EXPENSE_SETTINGS)
        for key in merged:
            if row.get(key) is not None:
                merged[key] = row[key]
        return merged

    def _rates_by_company(self, code: str, on_date: date) -> Dict[int, List[Dict[str, Any]]]:
        """``{company_id: [rows]}``. A company-agnostic row belongs to every
        company's list, which is what "applies everywhere" means; a caller
        resolves against the list of the company that owns the thing priced,
        so one company's rate cannot leak onto another's labourers."""
        rows = [
            {
                "id": row.get("id"),
                "scope": row.get("scope"),
                "company_id": row.get("company_id"),
                "department_id": row.get("department_id"),
                "department_name": row.get("department_name"),
                "basis": row.get("basis"),
                "rate": _dec(row.get("rate")),
                "effective_from": _as_date(row.get("effective_from")),
            }
            for row in self.postgres.rows(SQL_RATES, [code, on_date, self.company_ids])
        ]
        rows = [row for row in rows if row["effective_from"] is not None]
        shared = [row for row in rows if row["company_id"] is None]
        by_company = {company_id: list(shared) for company_id in self.company_ids}
        for row in rows:
            if row["company_id"] is not None and row["company_id"] in by_company:
                by_company[row["company_id"]].append(row)
        return by_company

    # ---------------------------------------------------------------- build

    def build(self) -> Dict[str, Any]:
        if not self.companies:
            raise LookupError("No company is configured in the factory database.")

        settings_row = self._settings()
        labour = self._labour_departments(settings_row)
        power = self._electricity_oil()
        salary = self._salary(settings_row)
        maintenance = self._maintenance(settings_row)

        # The wall board's own warnings that survive the factory's filter: its
        # labour and electricity warnings are dropped for this tile's, which
        # are stated over the rows THIS tile prices. Salary's is all that is
        # left of them.
        warnings: List[str] = []
        if salary["warning"]:
            warnings.append(salary["warning"])
        if labour["warning"]:
            warnings.append(labour["warning"])
        if power["warning"]:
            warnings.append(power["warning"])

        lines = {
            "labour": {
                "amount": labour["cost"],
                "warning": labour["warning"],
                "detail": labour["detail"],
                "basis": labour["note"],
                "today": labour["today"],
            },
            "electricity": {
                "amount": power["cost"],
                "warning": power["warning"],
                "detail": power["detail"],
                "basis": ELECTRICITY_NOTE,
                "today": power["today"],
            },
            "salary": {
                "amount": salary["mtd"],
                "warning": salary["warning"],
                "detail": salary["detail"],
                "basis": None,
                "today": salary["today"],
            },
            "maintenance": {
                "amount": maintenance["mtd"],
                "warning": None,
                "detail": {},
                "basis": None,
                "today": maintenance["today"],
            },
        }

        slices = []
        total = 0.0
        today_total = 0.0
        for spec in COST_SLICES:
            line = lines[spec["key"]]
            amount = _f(line["amount"])
            today = line["today"]
            slices.append(
                {
                    "key": spec["key"],
                    "label": spec["label"],
                    "bucket": spec["bucket"],
                    "amount": round(amount, 2),
                    # Zero WITH a warning is "nobody configured this"; zero
                    # without one is a real nil.
                    "has_source": amount > 0 or not line["warning"],
                    "warning": line["warning"],
                    "detail": line["detail"].get("text"),
                    "detail_value": line["detail"].get("value"),
                    "basis": line["basis"],
                    "today": round(_f(today["amount"]), 2),
                    "today_detail": today.get("text"),
                    "today_detail_value": today.get("value"),
                    "today_detail_unit": today.get("unit"),
                }
            )
            total += amount
            today_total += _f(today["amount"])

        for entry in slices:
            entry["share_pct"] = _pct(entry["amount"], total)

        return {
            "currency": "INR",
            "total": round(total, 2),
            "today_total": round(today_total, 2),
            # Elapsed days, not producing days: salary accrues on a Sunday and
            # so does a spare consumed on one.
            "avg_per_day": round(total / self.today.day, 2) if self.today.day else None,
            "slices": slices,
            "warnings": warnings,
            "electricity_note": ELECTRICITY_NOTE,
        }

    # --------------------------------------------------------------- labour

    def _labour_departments(self, settings_row: Dict[str, Any]) -> Dict[str, Any]:
        """Month-to-date labour on the five named departments.

        The register keeps two kinds of row under one shape: no department is
        the barrier tally, a department is the HOD splitting those same people
        across the floors afterwards. This prices the SPLIT rows for the five
        departments and nothing else; summing both kinds double-counts every
        allocated labourer. The line is a subset of the gate by design and
        says what share of it.
        """
        names = [name.strip().lower() for name in LABOUR_DEPARTMENTS]
        found: Dict[str, Dict[str, Any]] = {}
        for row in self.postgres.rows(SQL_LABOUR_DEPARTMENTS, [names]):
            found[(row.get("name") or "").strip().lower()] = row
        missing = [name for name in LABOUR_DEPARTMENTS if name.strip().lower() not in found]
        department_ids = [row["id"] for row in found.values()]

        span = [self.company_ids, self.month_first, self.today]
        entries = self.postgres.rows(SQL_LABOUR_ENTRIES, [department_ids, *span]) if department_ids else []
        gate_heads = int(_dec(self.postgres.value(SQL_LABOUR_GATE, span, default=0)))

        code = settings_row.get("labour_cost_type_code") or LABOUR_COST_TYPE_CODE
        rates = self._rates_by_company(code, self.today)

        heads = 0
        cost = Decimal(0)
        unpriced = 0
        days = set()
        seen_departments = set()
        flat_charged = set()
        today_heads = 0
        today_cost = Decimal(0)
        for entry in entries:
            work_date = _as_date(entry.get("work_date"))
            company_id = entry.get("company_id")
            department_id = entry.get("department_id")
            count = int(entry.get("count_in") or 0)
            heads += count
            if work_date == self.today:
                today_heads += count
            if count:
                days.add(work_date)
                seen_departments.add(department_id)
            rate = resolve(rates.get(company_id, []), department_id, work_date)
            if rate is None:
                unpriced += count
                continue
            charge = price_labour(rate, count)
            if rate["basis"] == "PER_DAY":
                # A flat daily charge lands once per company per day, however
                # many departments were staffed.
                key = (work_date, company_id)
                if key in flat_charged:
                    charge = Decimal(0)
                else:
                    flat_charged.add(key)
            cost += charge
            if work_date == self.today:
                today_cost += charge

        named = len(LABOUR_DEPARTMENTS)
        if not heads:
            detail = {"value": 0, "text": f"nobody booked to the {named} departments this month"}
        else:
            day_count = len(days)
            text = (
                f"{heads:,} across {len(seen_departments)} of {named} departments "
                f"over {day_count} day{'s' if day_count != 1 else ''}"
            )
            if unpriced:
                text += f" · {unpriced:,} unpriced"
            detail = {"value": heads, "text": text}

        warning = None
        if unpriced:
            warning = (
                f"{unpriced:,} of the {heads:,} labourers on these departments "
                f"have no rate in this period — set '{code}' in Admin › Cost "
                "Master, effective from the start of the month."
            )
        elif not heads and gate_heads:
            # Not a nil month: people came in and nobody booked them to a floor.
            warning = (
                f"{gate_heads:,} labourers came through the gate this month but "
                f"none is booked to {', '.join(LABOUR_DEPARTMENTS)} — the labour "
                "line reads nil until the departmental split is entered."
            )

        if missing:
            self.warn(
                f"{', '.join(missing)} "
                + ("is not a department" if len(missing) == 1 else "are not departments")
                + " on this deployment, so the labour line prices "
                + f"{len(department_ids)} of the {named} it names."
            )

        if gate_heads:
            note = (
                f"Labour is {', '.join(LABOUR_DEPARTMENTS)} only — {heads:,} of the "
                f"{gate_heads:,} people the gate counted this month. The Factory "
                "Expense board prices every department and the gate tally as well, "
                "so it reads higher."
            )
        else:
            note = (
                f"Labour is {', '.join(LABOUR_DEPARTMENTS)} only. The Factory Expense "
                "board prices every department and the gate tally as well, so it "
                "reads higher."
            )

        return {
            "cost": round(float(cost), 2),
            "heads": heads,
            "gate_heads": gate_heads,
            "unpriced": unpriced,
            "missing_departments": missing,
            "detail": detail,
            "warning": warning,
            "note": note,
            "today": {
                "amount": float(today_cost),
                "text": (
                    f"{today_heads:,} on the floors today"
                    if today_heads
                    else "nobody booked to a floor yet today"
                ),
                "value": today_heads or None,
                "unit": "in" if today_heads else None,
            },
        }

    # ----------------------------------------------------------- electricity

    def _electricity_oil(self) -> Dict[str, Any]:
        """Month-to-date electricity on Jivo Oil's meters, and only those.

        Forced to the company's own meters whatever the wall board's switch
        says: an Oil-only figure must not quietly widen into a campus one.
        Counts the meters that were READ, not the meter master: a meter
        nobody has read contributes no rupees and is not claimed as measured.
        """
        company = next((row for row in self.companies if row.get("code") == ELECTRICITY_COMPANY), None)
        nothing_today = {"amount": 0.0, "text": None, "value": None, "unit": None}
        if company is None:
            return {
                "cost": 0.0,
                "detail": {},
                "warning": (
                    f"No '{ELECTRICITY_COMPANY}' company on this deployment, so "
                    "the electricity line has no meters to read."
                ),
                "today": nothing_today,
            }

        name = company.get("name") or ELECTRICITY_COMPANY
        cost = Decimal(0)
        units = Decimal(0)
        meters = set()
        today_cost = Decimal(0)
        today_units = Decimal(0)
        read_today = False
        for row in self.postgres.rows(SQL_ELECTRICITY, [self.month_first, self.today, company["id"]]):
            cost += _dec(row.get("cost"))
            units += _dec(row.get("units"))
            meters.add(row.get("meter"))
            if _as_date(row.get("date")) == self.today:
                read_today = True
                today_cost += _dec(row.get("cost"))
                today_units += _dec(row.get("units"))

        if not meters:
            return {
                "cost": 0.0,
                "detail": {"value": 0, "text": f"no {name} meter read this month"},
                "warning": f"No reading on a {name} meter this month — Maintenance › Daily Electricity.",
                "today": nothing_today,
            }

        if read_today:
            today = {
                "amount": float(today_cost),
                "text": f"{float(today_units):,.0f} units today",
                "value": float(today_units),
                "unit": "units",
            }
        else:
            # A meter is read once a day: nil today almost always means the
            # reading has not been entered, not that the plant drew no power.
            today = {"amount": 0.0, "text": "no reading entered today", "value": None, "unit": None}

        return {
            "cost": round(float(cost), 2),
            "detail": {
                "value": len(meters),
                "text": (
                    f"{len(meters)} {name} meters · {float(units):,.0f} units"
                    if units
                    else f"{len(meters)} {name} meters"
                ),
            },
            "warning": None,
            "today": today,
        }

    # --------------------------------------------------------------- salary

    def _salary(self, settings_row: Dict[str, Any]) -> Dict[str, Any]:
        """The month's department-wise salary bill and its accrual to date.

        Every Cost Master row is counted once, whatever it is scoped to: a
        company-agnostic rate is handed to every company's list, so the merge
        keys on the ROW, not on the company that resolved it. Without that a
        ₹50.9 L bill read ₹1.53 Cr across three companies.
        """
        code = settings_row.get("salary_cost_type_code") or SALARY_COST_TYPE_CODE
        rates = self._rates_by_company(code, self.today)

        by_rate: Dict[Any, Dict[str, Any]] = {}
        for company in self.companies:
            for department_id, department_name, amount, rate_id in monthly_amounts_by_department(
                rates.get(company["id"], []), self.today
            ):
                if rate_id not in by_rate:
                    by_rate[rate_id] = {"department_id": department_id, "name": department_name, "amount": amount}

        merged: Dict[Any, Decimal] = {}
        for rate_id, entry in by_rate.items():
            key = entry["department_id"] if entry["department_id"] is not None else f"blanket:{rate_id}"
            merged[key] = merged.get(key, Decimal(0)) + entry["amount"]

        monthly_total = sum(merged.values(), Decimal(0))
        daily_total = _money(monthly_total / self.days_in_month)
        mtd = _money(daily_total * self.today.day)

        warning = None
        if not merged:
            warning = (
                f"No '{code}' rate in force for {self.today:%B %Y} — set one in "
                "Admin › Cost Master, or point this tile at a different cost type "
                "in Configuration."
            )

        # The bill behind the accrual. Without it a reader who knows the
        # payroll sees half of the right number and cannot tell.
        monthly = round(sum(float(_money(amount)) for amount in merged.values()), 2)
        detail = (
            {"value": monthly, "text": f"{_lakhs(monthly)}/month · {self.today.day} of {self.days_in_month} days"}
            if monthly
            else {}
        )

        return {
            "mtd": float(mtd),
            "monthly": monthly,
            "warning": warning,
            "detail": detail,
            # One day's accrual: a fraction of a monthly bill, not a tally of
            # anything, so it carries no count.
            "today": {"amount": float(daily_total), "text": None, "value": None, "unit": None},
        }

    # ----------------------------------------------------------- maintenance

    def _maintenance(self, settings_row: Dict[str, Any]) -> Dict[str, Any]:
        """Spares consumed and indents committed, month to date and today."""
        by_day: Dict[date, Decimal] = defaultdict(Decimal)

        if settings_row.get("maintenance_include_spares"):
            zone = settings.BOARDS_TIME_ZONE
            for row in self.postgres.rows(
                SQL_SPARES,
                [
                    zone,
                    self.company_ids,
                    list(MAINTENANCE_SPEND_MOVEMENTS),
                    zone,
                    self.month_first,
                    zone,
                    self.today,
                ],
            ):
                day = _as_date(row.get("day"))
                if day is not None:
                    by_day[day] += _money(_dec(row.get("quantity")) * _dec(row.get("unit_cost")))

        if settings_row.get("maintenance_include_indents"):
            for row in self.postgres.rows(
                SQL_INDENTS,
                [self.company_ids, self.month_first, self.today, list(MAINTENANCE_COMMITTED_INDENT_STATUSES)],
            ):
                day = _as_date(row.get("day"))
                if day is not None:
                    by_day[day] += _money(_dec(row.get("lines_total")) + _dec(row.get("other_charges")))

        mtd = sum((amount for day, amount in by_day.items() if self.month_first <= day <= self.today), Decimal(0))
        return {
            "mtd": float(_money(mtd)),
            "today": {"amount": float(by_day.get(self.today, Decimal(0))), "text": None, "value": None, "unit": None},
        }
