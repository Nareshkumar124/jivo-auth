"""
Tests for the Logistics Control board: the service run against fakes, and
the fragment that draws it.

Nothing here opens a connection. The factory database and both SAP schemas
are fakes from ``boards.testing``, made parameter-aware (``Routed``) because
the service reads one statement per company and per booking state: a
substring alone cannot tell Oil's plans from Mart's.

The fixture is one October day at BH-BT, built so every figure on the wall
can be worked out by hand (the arithmetic is beside each table). The
degraded tests take sources away one at a time -- SAP, the factory
database, Mart's SAP alone, one statement -- and check the board says "not
read" where it must and still draws everything else.
"""

import copy
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.staticfiles import finders
from django.template.loader import get_template, render_to_string
from django.test import SimpleTestCase, override_settings

from . import formatting, registry
from .compose import build_board, render_fragment
from .services.logistics import LogisticsBoardService, dec, page_money, whole
from .sources import FactoryQueryError, FactoryUnavailable, Unavailable
from .testing import FakeHana, FakePostgres, hana_factory

TODAY = date(2026, 10, 14)  # a Wednesday
NOW = datetime(2026, 10, 14, 14, 5, 9, tzinfo=ZoneInfo("Asia/Kolkata"))
OIL_ID, MART_ID, BEV_ID = 1, 2, 3
COMPANY_IDS = {"JIVO_OIL": OIL_ID, "JIVO_MART": MART_ID, "JIVO_BEVERAGES": BEV_ID}

FACTORY_OFF = {
    "NAME": "", "USER": "", "PASSWORD": "", "HOST": "", "PORT": 5432,
    "CONNECT_TIMEOUT": 1, "STATEMENT_TIMEOUT": 1,
}
HANA_OFF = {
    "HOST": "", "PORT": 30015, "USER": "", "PASSWORD": "", "SCHEMAS": {},
    "CONNECT_TIMEOUT": 1, "COMMUNICATION_TIMEOUT": 1,
}


# ---------------------------------------------------------------- the fakes


class Routed:
    """A fake whose answer may be a function of the bound parameters."""

    def route(self, needle, answer):
        self.answers.append((needle, answer))
        return self

    def rows(self, sql, params=None):
        self.calls.append((sql, params))
        for needle, answer in self.answers:
            hit = needle.search(sql) if isinstance(needle, re.Pattern) else needle in sql
            if hit:
                rows = answer(sql, list(params or [])) if callable(answer) else answer
                return copy.deepcopy(list(rows))
        return []


class RoutedPostgres(Routed, FakePostgres):
    pass


class RoutedHana(Routed, FakeHana):
    pass


def failing(message="column does not exist"):
    def answer(sql, params):
        raise FactoryQueryError(message)

    return answer


# ------------------------------------------------------- the factory database

#: Each statement the service sends, by a substring only it contains.
PG = {
    "companies": "FROM company_company WHERE code = ANY",
    "active_companies": "FROM company_company WHERE is_active",
    "warehouse_settings": "stock_dashboard_warehouseboardsettings",
    "board_settings": "stock_dashboard_logisticsboardsettings",
    "plans": "dispatch_plans_selecteddispatchbill",
    "allocated": "warehouse_pf_movement",
    "gate_out": "g.status = 'DISPATCHED'",
    "partial_scans": "docking_admin_dockingpartialscanrequest",
    "bst": "warehouse_bsttransfer",
    "fleet_plans": "p.booking_status <> 'DISPATCHED'",
    "gate_out_today": "ON a.id = g.arrival_id",
    "arrivals": "WHERE a.gate_in_date",
    "grpo_queue": "grpo_servicegrpoposting",
    "rates": "cost_master_costrate",
    "labour": "labour_gate_labourgateentry",
    "roll_count": "SELECT COUNT(*) FROM employee_hierarchy_employee",
    "roll_departments": "FROM employee_hierarchy_department d",
}


def plan(doc_entry, status, dispatch_date, vehicle=None):
    return {
        "doc_entry": doc_entry,
        "doc_num": str(50000 + doc_entry),
        "dispatch_date": dispatch_date,
        "booking_status": status,
        "vehicle_id": vehicle,
    }


#: Dispatch plans on the Plan page. 103 is PENDING in the app but SAP has
#: stamped it dispatched, so the pending tile drops it; 107 left yesterday.
PLANS = {
    OIL_ID: [
        plan(101, "PENDING", TODAY),
        plan(102, "PENDING", TODAY + timedelta(days=1)),
        plan(103, "PENDING", TODAY - timedelta(days=2)),
        plan(104, "BOOKED", TODAY, vehicle=7),
        plan(105, "BOOKED", TODAY, vehicle=8),
        plan(107, "DISPATCHED", TODAY - timedelta(days=1), vehicle=7),
    ],
    MART_ID: [
        plan(201, "PENDING", TODAY + timedelta(days=2)),
        plan(202, "BOOKED", TODAY, vehicle=9),
    ],
}


def selected_plans(sql, params):
    company_id, date_from, date_to = params[0], params[1], params[2]
    statuses = params[3] if len(params) == 5 else None
    return [
        p for p in PLANS.get(company_id, [])
        if date_from <= p["dispatch_date"] <= date_to
        and (statuses is None or p["booking_status"] in statuses)
    ]


#: Gate-outs by day of October: (day, trucks, bills, kg, litres, boxes).
#: Month: 411.8 T over 11 dispatching days (avg 37.4); 4,34,900 L.
#: Today 21.4 T against yesterday's 36.8 T: down 42%.
GATE_OUT = [
    (1, 9, 14, 41800, 44100, 3480),
    (3, 8, 12, 37200, 39300, 3100),
    (4, 10, 15, 46900, 49600, 3900),
    (6, 7, 11, 33400, 35200, 2780),
    (7, 9, 13, 39800, 42000, 3310),
    (8, 8, 12, 36100, 38100, 3010),
    (9, 10, 16, 47600, 50300, 3960),
    (10, 9, 14, 42300, 44700, 3520),
    (11, 6, 9, 28500, 30100, 2370),
    (13, 8, 12, 36800, 38900, 3060),
    (14, 5, 7, 21400, 22600, 1780),
]


def gate_out_rows(table=GATE_OUT):
    return [
        {
            "day": date(2026, 10, day),
            "trucks": trucks,
            "bills": bills,
            "weight": Decimal(kg),
            "litres": Decimal(litres),
            "boxes": Decimal(boxes),
        }
        for day, trucks, bills, kg, litres, boxes in table
    ]


def grpo_row(pk, age, bilty, freight, *, entry=None, posted=False, status="DISPATCHED"):
    return {
        "id": pk,
        "booking_status": status,
        "dispatch_date": TODAY - timedelta(days=age),
        "bilty_no": bilty,
        "bilty_date": TODAY - timedelta(days=age),
        "linked_vehicle_entry_id": entry or pk,
        "vehicle_id": 7,
        "transporter_id": 3,
        "vehicle_number": "HR69E4548",
        "freight": freight,
        "total_freight": None,
        "bilty_attachment": "bilty.pdf" if bilty else "",
        "posted": posted,
    }


#: The service-GRPO queue. Oil: 2, 4, 6, 9 (304 and 311 are one bilty, so
#: one GRPO), 12 days -> 0-14; 17, 22 -> 15-29; 33 -> 30-44; 51 -> 45+;
#: 310 is posted and off the queue. Mart: 3, 8 -> 0-14; 19 -> 15-29.
#: So the GRPO column reads 7 / 3 / 1 / 1.
GRPO_PLANS = {
    OIL_ID: [
        grpo_row(301, 2, "B-7781", Decimal("18500")),
        grpo_row(302, 4, "B-7779", Decimal("21000")),
        grpo_row(303, 6, "", None, status="BOOKED"),
        grpo_row(304, 9, "B-7762", Decimal("16400")),
        grpo_row(311, 9, "B-7762", Decimal("16400"), entry=304),
        grpo_row(305, 12, "B-7750", None),
        grpo_row(306, 17, "B-7731", Decimal("22800")),
        grpo_row(307, 22, "B-7702", None),
        grpo_row(308, 33, "B-7655", Decimal("19900")),
        grpo_row(309, 51, "B-7590", Decimal("24100")),
        grpo_row(310, 8, "B-7770", Decimal("17000"), posted=True),
    ],
    MART_ID: [
        grpo_row(401, 3, "M-1102", Decimal("9800")),
        grpo_row(402, 8, "M-1097", None),
        grpo_row(403, 19, "M-1088", Decimal("11200")),
    ],
}

#: Typed on the settings screen. Salaries are monthly: 8,400 / 5,700 / 3,200
#: a day. The labour rate (450) wins over Cost Master's 420.
BOARD_SETTINGS = {
    "owned_vehicles": 6,
    "owned_vehicle_numbers": "HR69E4548\nhr69 f 1021\nHR55AB7781\nPB10GH3344\nHR69C9090\nHR38Z1200",
    "vehicles_out_of_service": "HR38Z1200",
    "labour_rate_per_day": Decimal("450"),
    "warehouse_employees": 14,
    "warehouse_salary_monthly": Decimal("252000"),
    "dispatch_employees": 9,
    "dispatch_salary_monthly": Decimal("171000"),
    "transport_employees": 4,
    "transport_salary_monthly": Decimal("96000"),
}

#: The gate's head count today: 46 in the basement, 118 in all.
#: Labour 118 x 450 = 53,100; salaries 17,300; workforce 70,400 a day.
LABOUR = [
    {"company_id": OIL_ID, "department_id": 11, "department": "Warehouse Basement", "count_in": 46},
    {"company_id": OIL_ID, "department_id": 12, "department": "Production", "count_in": 58},
    {"company_id": OIL_ID, "department_id": None, "department": None, "count_in": 14},
]

ROLL = {OIL_ID: 212, MART_ID: 38, BEV_ID: 61}
ROLL_DEPARTMENTS = {
    OIL_ID: [
        {"code": "WAREHOUSE_FG_WAREHOUSE_BASEMEN", "name": "FG Warehouse Basement", "employee_count": 15},
        {"code": "DOCK", "name": "Dock", "employee_count": 10},
        {"code": "TRANSPORT_TRANSPORT", "name": "Transport", "employee_count": 5},
    ],
}


def factory_db(**overrides):
    """The factory's PostgreSQL for one October day. ``overrides`` replace
    a statement's answer by its key in ``PG`` (rows or a callable)."""
    answers = {
        "companies": lambda sql, params: [
            {"id": COMPANY_IDS[code], "code": code} for code in params[0] if code in COMPANY_IDS
        ],
        "active_companies": [{"id": i, "code": c} for c, i in COMPANY_IDS.items()],
        "warehouse_settings": [{"capacity_tonnes": Decimal("1200.00"), "last_audit_date": date(2026, 9, 28)}],
        "board_settings": [BOARD_SETTINGS],
        "plans": selected_plans,
        "allocated": [{"movements": 6, "pieces": 14400, "litres": Decimal("9600.0")}],
        "gate_out": gate_out_rows(),
        "partial_scans": [{"approvals": 3, "expected_boxes": 1840, "scanned_boxes": 1712, "shortfall_boxes": 128}],
        "bst": [{
            "vehicle_number": "HR 69 E 4548", "entry_no": "BST-0912", "sap_doc_num": 88812,
            "sap_from_warehouse": "BH-BT", "sap_to_warehouse": "BH-PF",
        }],
        "fleet_plans": [{"vehicle_number": "HR69F1021", "sap_invoice_doc_num": "50105", "customer_name": "A customer"}],
        "gate_out_today": [{"vehicle_number": "HR55AB7781", "sap_doc_num": 50104}],
        "arrivals": [{"vehicle_number": "PB10GH3344", "status": "INSIDE"}],
        "grpo_queue": lambda sql, params: GRPO_PLANS.get(params[0], []),
        "rates": [{
            "id": 1, "scope": "FACTORY", "company_id": None, "department_id": None,
            "basis": "PER_PERSON_DAY", "rate": Decimal("420"), "effective_from": date(2026, 4, 1),
        }],
        "labour": LABOUR,
        "roll_count": lambda sql, params: [{"count": ROLL.get(params[0], 0)}],
        "roll_departments": lambda sql, params: ROLL_DEPARTMENTS.get(params[1], []),
    }
    answers.update(overrides)
    db = RoutedPostgres()
    for key, needle in PG.items():
        db.route(needle, answers[key])
    return db


# ---------------------------------------------------------------- SAP

HANA = {
    "transit": "WITH credited AS",
    "stock": 'AS "GrossWeightPerCase"',
    "non_moving": "WITH Stock AS",
    "bills": "WITH line_agg AS",
    "awaiting": "L.\"LineStatus\" = 'O'",
    "outstanding": 'H."PaidToDate"',
    "payments": '"OVPM"',
    "freight": "AS freight",
}


def stock_item(code, on_hand, per_box, gross, name="Finished good"):
    return {
        "ItemCode": code, "ItemName": name, "OnHand": Decimal(on_hand), "PiecesPerBox": per_box,
        "LitresPerPiece": 1, "StockValue": Decimal(on_hand) * 120, "Uom": "PCS",
        "GrossWeightPerCase": None if gross is None else Decimal(str(gross)),
    }


#: BH-BT's finished goods: 112.0 + 142.5 + 262.8 + 46.4 + 57.0 = 620.7 T,
#: against 1,200 T of capacity: 52% full, 579 T free. FG-NEW-2L has no
#: case weight.
STOCK = [
    stock_item("FG-OIL-1L", 120000, 12, 11.2),
    stock_item("FG-OIL-5L", 30000, 4, 19.0),
    stock_item("FG-OIL-15L", 18000, 1, 14.6),
    stock_item("FG-OIL-500ML", 96000, 24, 11.6),
    stock_item("FG-MUST-1L", 60000, 12, 11.4),
    stock_item("FG-NEW-2L", 2400, 6, None),
]

#: Idle 40+ days: 15 L tins 2,400 x 14.6 = 35.04 T past 90 days, mustard
#: 6,000 x 0.95 = 5.7 T inside 40-90, one item unweighable; the BH-PF row is
#: another floor's. 40.74 T, 3.4% of the 1,200 T.
NON_MOVING = [
    {"ItemCode": "FG-OIL-15L", "Quantity": Decimal(2400), "Value": Decimal(0), "DaysSinceLastMovement": 118, "WhsCode": "BH-BT"},
    {"ItemCode": "FG-MUST-1L", "Quantity": Decimal(6000), "Value": Decimal(0), "DaysSinceLastMovement": 52, "WhsCode": "BH-BT"},
    {"ItemCode": "FG-NEW-2L", "Quantity": Decimal(500), "Value": Decimal(0), "DaysSinceLastMovement": 64, "WhsCode": "BH-BT"},
    {"ItemCode": "FG-OIL-1L", "Quantity": Decimal(9000), "Value": Decimal(0), "DaysSinceLastMovement": 71, "WhsCode": "BH-PF"},
]

#: Kilograms per SAP invoice. 103 carries SAP's own dispatch stamp.
#: Pending: Oil 101+102+104+105 = 45.6 T, Mart 201+202 = 11.9 T; 6 bills,
#: 3 booked (104, 105, 202) = 32.5 T. Today's plan: 101 (no truck), 104,
#: 105, 202 = 32.5 T booked on 3 bills, 1 awaiting a truck.
BILL_KG = {101: 8200, 102: 12500, 103: 6400, 104: 15800, 105: 9100, 107: 11000, 201: 4300, 202: 7600}
SAP_DISPATCHED = {103}


def bill_weights(sql, params):
    entries = params[: len(params) // 2]
    pending = "U_Dipatch_Date" in sql
    return [
        {"doc_entry": d, "doc_num": str(50000 + d), "total_weight": Decimal(BILL_KG[d])}
        for d in entries
        if d in BILL_KG and not (pending and d in SAP_DISPATCHED)
    ]


#: Oil to Mart, unreceived: 18.4 T a day out, 12.3 T five, 6.1 T nine
#: (one line of it unweighable). 36.8 T on 3 loads, 1 over seven days.
TRANSIT = [
    {"DocNum": 50211, "DocDate": date(2026, 10, 13), "DaysOut": 1, "Kilograms": Decimal(18400), "Unweighed": 0},
    {"DocNum": 50198, "DocDate": date(2026, 10, 9), "DaysOut": 5, "Kilograms": Decimal(12300), "Unweighed": 0},
    {"DocNum": 50177, "DocDate": date(2026, 10, 5), "DaysOut": 9, "Kilograms": Decimal(6100), "Unweighed": 1},
]


def band_rows(*rows, amount="open_sum"):
    """(band, docs, amount, oldest) as the freight readers' grouped rows."""
    out = []
    for band, docs, value, oldest in rows:
        row = {"band": band, "docs": docs, amount: Decimal(value), "oldest_days": oldest}
        if amount == "outstanding":
            row.update(billed=Decimal(value), paid=Decimal(0))
        out.append(row)
    return out


def oil_sap(**overrides):
    """JIVO_OIL's schema. AP awaiting invoice and open payables by age;
    oldest open invoice 74 days. Freight this month 3,12,000 + 1,48,000."""
    answers = {
        "transit": TRANSIT,
        "stock": STOCK,
        "non_moving": NON_MOVING,
        "bills": bill_weights,
        "awaiting": band_rows((0, 12, 284000, 13), (15, 5, 146500, 27), (30, 2, 61200, 41), (45, 1, 38900, 63)),
        "outstanding": band_rows(
            (0, 9, 312400, 14), (15, 6, 205800, 29), (30, 3, 118300, 44), (45, 2, 96700, 74), amount="outstanding"
        ),
        "payments": [{"payments": 14, "paid": Decimal(642000), "latest": date(2026, 10, 10)}],
        "freight": [
            {"CardCode": "V0012", "card_name": "SHREE BALAJI ROADLINES", "docs": 18, "freight": Decimal(312000)},
            {"CardCode": "V0031", "card_name": "PICK & SHIP LOGISTICS PRIVATE LIMITED", "docs": 9, "freight": Decimal(148000)},
        ],
    }
    answers.update(overrides)
    sap = RoutedHana(company_code="JIVO_OIL", schema="OIL_SCHEMA")
    for key, needle in HANA.items():
        sap.route(needle, answers[key])
    return sap


def mart_sap(**overrides):
    """JIVO_MART's schema: no warehouse read, its own bills and freight.
    AP column 0-14: 12 + 3 = 15 GRPOs, 2,84,000 + 52,000 = ₹3.36 L.
    Freight 58,000 on 4 GRPOs: 5,18,000 on 31 in all, ₹1.19 a litre."""
    answers = {
        "transit": [],
        "stock": [],
        "non_moving": [],
        "bills": bill_weights,
        "awaiting": band_rows((0, 3, 52000, 9), (15, 1, 18000, 19)),
        "outstanding": band_rows((0, 2, 41000, 11), amount="outstanding"),
        "payments": [{"payments": 3, "paid": Decimal(88000), "latest": date(2026, 10, 8)}],
        "freight": [{"CardCode": "V0012", "card_name": "SHREE BALAJI ROADLINES", "docs": 4, "freight": Decimal(58000)}],
    }
    answers.update(overrides)
    sap = RoutedHana(company_code="JIVO_MART", schema="MART_SCHEMA")
    for key, needle in HANA.items():
        sap.route(needle, answers[key])
    return sap


def both_sap():
    return hana_factory(oil_sap(), mart_sap())


def factory_down():
    return Unavailable(FactoryUnavailable("The factory database could not be reached: connection refused"))


# --------------------------------------------------------------- building


def build(postgres=None, hana_for=None, company="JIVO_OIL"):
    """The service's payload, stamped the way ``compose.build_board`` stamps it."""
    service = LogisticsBoardService(
        company,
        postgres=factory_db() if postgres is None else postgres,
        hana_for=both_sap() if hana_for is None else hana_for,
        today=TODAY,
    )
    payload = service.build()
    payload["meta"].update(
        slide="logistics", label="Logistics Control", company=company, generated_at=NOW.isoformat()
    )
    return formatting.jsonable(payload)


def full_board():
    return build()


def all_unread_board():
    """SAP and the factory database both down: every section is None."""
    return build(postgres=factory_down(), hana_for=hana_factory())


def render(payload):
    return render_to_string(
        "boards/fragments/logistics.html",
        {"board": payload, "meta": payload["meta"], "slide": registry.get_slide("logistics")},
    )


def tile(payload, key):
    for band in payload["bands"]:
        for item in band["tiles"]:
            if item["key"] == key:
                return item
    raise KeyError(key)


def tile_html(html, key):
    """One tile's markup, up to the next tile or the people strip."""
    match = re.search(
        r'<div class="ops-grp[^"]*" data-tile="%s">(.*?)(?=<div class="ops-grp|<div class="ops-people")' % key,
        html,
        re.S,
    )
    if not match:
        raise AssertionError(f"no tile {key!r} in the fragment")
    return match.group(1)


def squash(html):
    """Whitespace between tags removed, so assertions read as the screen does."""
    return re.sub(r">\s+<", "><", re.sub(r"\s+", " ", html))


# ------------------------------------------------------- the service changes


class FormattingTests(SimpleTestCase):
    """The page's own formatters, which the shared filters get wrong here."""

    def test_dec_carries_into_the_whole_part(self):
        # The shared filter prints 2.96 as "2.0" and 612.97 as "612.0".
        self.assertEqual(dec(2.96), "3.0")
        self.assertEqual(dec(612.97), "613.0")
        self.assertEqual(dec(-2.96), "-3.0")
        self.assertEqual(dec(1234567.96), "12,34,568.0")
        self.assertEqual(dec(40.74), "40.7")
        self.assertEqual(dec(None), "—")

    def test_ties_round_up_as_the_browser_does(self):
        self.assertEqual(dec(32.5, 0), "33")
        self.assertEqual(dec(28.5, 0), "29")
        self.assertEqual(dec(2.25), "2.3")
        # The float's exact value is just under the tie, as in the browser.
        self.assertEqual(dec(2.675, 2), "2.67")
        self.assertEqual(whole(32.5), "33")
        self.assertEqual(whole(620.7), "621")
        self.assertEqual(whole(None), "—")

    def test_page_money_gives_lakhs_two_places(self):
        self.assertEqual(page_money(336000), "₹3.36 L")
        self.assertEqual(page_money(12345678), "₹1.23 Cr")
        self.assertEqual(page_money(70400), "₹70.4k")
        self.assertEqual(page_money(950), "₹950")
        self.assertEqual(page_money(None), "—")

    def test_a_tonnage_that_rounds_up_reads_rounded_up(self):
        table = [row if row[0] != 14 else (14, 1, 1, 2960, 3100, 240) for row in GATE_OUT]
        payload = build(postgres=factory_db(gate_out=gate_out_rows(table)))
        self.assertEqual(tile(payload, "today")["value"], "3.0")

    def test_the_matrix_and_the_workforce_total_use_the_pages_money(self):
        payload = full_board()
        matrix = tile(payload, "account")["viz"]
        self.assertEqual(matrix["rows"][0]["cells"][1]["cash"], "₹3.36 L")  # not "₹3.4 L"
        self.assertEqual(matrix["rows"][0]["cells"][2]["cash"], "₹3.53 L")
        self.assertEqual(payload["topbar"]["totals"][1]["value"], "₹70.4k")


class TransitLatchTests(SimpleTestCase):

    def test_marts_sap_missing_does_not_latch_sap_off_for_oil(self):
        payload = build(hana_for=hana_factory(oil_sap()))

        self.assertIsNone(payload["transit"])
        self.assertIn("MART's SAP could not be read", payload["meta"]["reasons"]["transit"])
        # Oil is still read where Mart is a half of the figure, and says so.
        self.assertIsNotNone(payload["transporter_account"])
        self.assertEqual(payload["transporter_account"]["unread"], ["JIVO_MART"])
        self.assertIsNotNone(payload["freight_rate"])
        self.assertEqual(payload["pending_dispatch"]["unread"], ["JIVO_MART"])
        self.assertNotIn("SAP did not answer. The tiles that read it show nothing rather than a stale figure.",
                         payload["meta"]["warnings"])
        self.assertEqual(payload["meta"]["degraded"], ["transit"])

    def test_oils_own_sap_down_still_latches(self):
        payload = build(hana_for=hana_factory(mart_sap()))
        for name in ("stock", "non_moving", "pending_dispatch", "day_plan", "transit", "transporter_account", "freight_rate"):
            self.assertIsNone(payload[name], name)
        self.assertIn("No SAP schema is configured for JIVO_OIL.", payload["meta"]["reasons"]["transit"])


# ------------------------------------------------------------- the payload


class PayloadTests(SimpleTestCase):

    def test_the_fixture_reads_completely(self):
        payload = full_board()
        self.assertEqual(payload["meta"]["degraded"], [])
        self.assertEqual(payload["meta"]["warnings"], [])

    def test_the_figures_the_tiles_are_drawn_from(self):
        p = full_board()

        self.assertAlmostEqual(p["stock"]["tonnes"], 620.7)
        self.assertAlmostEqual(tile(p, "stock")["figures"]["fill_pct"], 51.725)
        self.assertAlmostEqual(p["non_moving"]["tonnes"], 40.74)
        self.assertEqual(p["non_moving"]["unweighed"], 1)
        self.assertAlmostEqual(p["pending_dispatch"]["tonnes"], 57.5)
        self.assertEqual(p["pending_dispatch"]["invoices"], 6)  # 103 is SAP-dispatched
        self.assertEqual(p["pending_dispatch"]["booked_bills"], 3)
        self.assertAlmostEqual(p["day_plan"]["tonnes"], 32.5)
        self.assertEqual(p["day_plan"]["unbooked_bills"], 1)
        self.assertAlmostEqual(p["dispatch"]["month"]["tonnes"], 411.8)
        self.assertEqual(p["dispatch"]["month"]["active_days"], 11)
        self.assertEqual(
            p["fleet"]["summary"],
            {"owned": 6, "on_bst": 1, "on_dispatch": 2, "at_plant": 1, "out": 0, "free": 1, "out_of_service": 1},
        )
        self.assertEqual(p["transit"]["totals"], {"loads": 3, "tonnes": 36.8})
        self.assertEqual(p["transporter_account"]["oldest_days"], 74)
        self.assertEqual([b["documents"] for b in p["grpo_queue"]["buckets"]], [7, 3, 1, 1])
        self.assertEqual((p["freight_rate"]["amount"], p["freight_rate"]["documents"]), (518000.0, 31))
        self.assertAlmostEqual(p["workforce"]["total_cost_per_day"], 70400.0)

    def test_three_bands_in_the_order_the_plant_works(self):
        p = full_board()
        self.assertEqual([b["key"] for b in p["bands"]], ["warehouse", "dispatch", "transport"])
        self.assertEqual(
            [[t["key"] for t in b["tiles"]] for b in p["bands"]],
            [
                ["stock", "non_moving", "pending", "allocated"],
                ["today", "month", "planned", "unscanned"],
                ["fleet", "account", "cost", "transit"],
            ],
        )

    def test_the_registry_points_at_this_service_and_its_files_exist(self):
        slide = registry.get_slide("logistics")
        self.assertIs(slide.service_class(), LogisticsBoardService)
        for path in (*slide.css, *slide.js):
            self.assertIsNotNone(finders.find(path), path)
        for name in ("band", "tile", "viz", "people"):
            get_template(f"boards/fragments/logistics/{name}.html")

    @override_settings(FACTORY_DB=FACTORY_OFF, FACTORY_HANA=HANA_OFF, FACTORY_COMPANY_CODE="JIVO_OIL")
    def test_compose_builds_and_renders_it_like_any_other_board(self):
        with mock.patch("boards.compose.connect") as connect, \
                mock.patch("boards.compose.hana_for", both_sap()), \
                mock.patch("boards.formatting.now", return_value=NOW):
            connect.return_value.__enter__.return_value = factory_db()
            slide = registry.get_slide("logistics")
            payload = build_board(slide)
            html = render_fragment(slide, payload)

        self.assertEqual(payload["meta"]["slide"], "logistics")
        self.assertEqual(payload["meta"]["company"], "JIVO_OIL")
        self.assertEqual(payload["meta"]["refresh_seconds"], 60)
        self.assertEqual(payload["meta"]["clock_date"], "Wednesday, 14 Oct 2026")
        self.assertEqual(payload["today"], "2026-10-14")
        self.assertIn('<div class="t" data-clock>14:05</div>', html)
        self.assertIn("Wednesday, 14 Oct 2026", html)
        self.assertNotIn("ops-unread", html)


# ------------------------------------------------------------ the template


class TemplateTests(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.payload = full_board()
        cls.html = render(cls.payload)
        cls.flat = squash(cls.html)

    def test_the_frame_is_the_factory_boards(self):
        html = self.flat
        self.assertTrue(html.strip().startswith('<div class="ops-board lc-board" data-board="logistics">'))
        self.assertIn('<div class="ops-board__inner"><header class="ops-topbar">', html)
        self.assertIn('<div class="ops-pill-live" data-state="live"><i></i>LIVE</div>', html)
        self.assertIn('<main class="ops-stack">', html)
        self.assertEqual(html.count('<section class="ops-band '), 3)
        for domain in ("warehouse", "dispatch", "transport"):
            self.assertIn(f'<section class="ops-band ops-b-{domain}" data-band="{domain}">', html)
        self.assertIn("<p>Warehouse <em>BH-BT</em></p>", html)
        self.assertIn("<p>Dispatch <em>JIVO_OIL | JIVO_MART</em></p>", html)
        self.assertIn("<p>Transportation</p>", html)
        self.assertEqual(html.count('style="grid-template-columns: repeat(4, minmax(0, 1fr))"'), 3)
        self.assertEqual(html.count('<div class="ops-people">'), 3)
        self.assertNotIn("ops-unread", html)
        self.assertNotIn("bcx-meta", html)  # nothing degraded, no caveats
        self.assertNotIn("None", html)
        self.assertNotIn("nan", html)

    def test_the_topbar(self):
        html = self.flat
        self.assertIn('<b title="Operations board">Operations board</b>', html)
        self.assertIn('<span title="BH-BT · JIVO_OIL | JIVO_MART">BH-BT · JIVO_OIL | JIVO_MART</span>', html)
        self.assertIn("<em>Month</em> October 2026</div>", html)
        self.assertIn("<em>Dispatching days</em> 11 so far</div>", html)
        self.assertIn(
            '<div class="cap">Employees on roll</div><div class="n">250</div><div class="s">2 companies</div>', html
        )
        self.assertIn(
            '<div class="cap">Workforce cost today</div><div class="n">₹70.4k</div><div class="s">118 labour on shift</div>',
            html,
        )
        self.assertIn('<div class="ops-clock"><div class="t" data-clock>14:05</div><div class="d">Wednesday, 14 Oct 2026</div></div>', html)

    def test_every_tile_keeps_the_four_row_grid(self):
        html = self.flat
        self.assertEqual(html.count('<div class="ops-grp" data-tile='), 12)
        self.assertEqual(html.count('<div class="ops-grow"><b class="ops-nm">'), 12)
        self.assertEqual(html.count('<div class="ops-sub">'), 12)
        # Planned against booked and the transport account are tall.
        self.assertEqual(html.count('<div class="ops-val">'), 10)
        self.assertEqual(html.count('<div class="ops-viz ops-tall">'), 2)

    def test_the_warehouse_band(self):
        stock = squash(tile_html(self.html, "stock"))
        self.assertIn('<span class="ops-tag ops-t-neut">52% full</span>', stock)
        self.assertIn('<div class="ops-sub">1,200 T capacity · last audit 28 Sep</div>', stock)
        self.assertIn("<b>621</b><u>tonnes</u>", stock)
        self.assertIn('<span class="ops-k-main">Stored</span>', stock)
        self.assertIn('<span class="ops-k-mute">Free<b>579 T</b></span>', stock)
        self.assertIn('<i class="ops-f-main" style="width: 51.73%; min-width: var(--meter-h);"></i>', stock)

        idle = squash(tile_html(self.html, "non_moving"))
        self.assertIn('<span class="ops-tag ops-t-neut">3.4% of space</span>', idle)
        self.assertIn('<div class="ops-sub">40+ days</div>', idle)
        self.assertIn("<b>40.7</b><u>tonnes</u>", idle)
        self.assertIn("40–90 days<b>5.7 T</b>", idle)
        self.assertIn("90+ days<b>35.0 T</b>", idle)
        self.assertIn("1 of 3 items have no case weight in SAP, so this is a floor.", idle)

        pending = squash(tile_html(self.html, "pending"))
        self.assertIn('<span class="ops-tag ops-t-neut">6 invoices</span>', pending)
        self.assertIn("<b>57.5</b><u>tonnes</u>", pending)
        self.assertIn(
            '<div class="ops-duo"><div><span class="k">OIL</span><span class="v">45.6 T</span></div>'
            '<div><span class="k">MART</span><span class="v">11.9 T</span></div></div>',
            pending,
        )

        allocated = squash(tile_html(self.html, "allocated"))
        self.assertIn('<span class="ops-tag ops-t-neut">6 consignments</span>', allocated)
        self.assertIn("<b>14,400</b><u>pieces</u>", allocated)
        self.assertIn('<span class="k">Litres</span><span class="v">9,600</span>', allocated)

    def test_the_dispatch_band(self):
        today = squash(tile_html(self.html, "today"))
        self.assertIn('<span class="ops-tag ops-t-warn">↓ 42% vs yesterday</span>', today)
        self.assertIn('<div class="ops-sub"></div>', today)  # held empty, on purpose
        self.assertIn("<b>21.4</b><u>tonnes</u>", today)
        self.assertIn('<span class="ops-k-main">Dispatched</span><span class="ops-k-mute">Day plan<b>33 T</b></span>', today)
        self.assertIn('<p class="ops-note">Booked bills 3 · Awaiting a truck 1</p>', today)

        month = squash(tile_html(self.html, "month"))
        self.assertIn('<span class="ops-tag ops-t-nil">no target set</span>', month)
        self.assertIn('<div class="ops-sub">1 Oct – 14 Oct · avg 37.4 T day</div>', month)
        self.assertIn("<b>412</b><u>tonnes</u>", month)
        labels = re.findall(r"<span(?: class=\"ops-now\")?>([^<]+)</span>", month.split('class="ops-blab"')[1])
        self.assertEqual(labels, ["08", "09", "10", "11", "12", "13", "Today"])
        self.assertEqual(re.findall(r'<em class="ops-bval">([^<]+)</em>', month), ["36", "48", "42", "29", "37", "21"])
        # The 12th moved nothing: an empty column, no figure over it.
        self.assertIn('<div><i style="height: 0.00%; animation-delay: 0.26s;"></i></div>', month)
        self.assertIn('<div class="ops-now"><em class="ops-bval">21</em>', month)

        planned = squash(tile_html(self.html, "planned"))
        self.assertIn('<span class="ops-tag ops-t-warn">3 awaiting a truck</span>', planned)
        self.assertIn('<em title="Planned">Planned</em>', planned)
        self.assertIn("<strong>6 bills</strong>", planned)
        self.assertIn('<i class="ops-f-main" style="width: 50.00%"></i></div><strong>3 bills</strong>', planned)
        self.assertIn("Booked<b>32.5 T of 57.5 T</b>", planned)

        unscanned = squash(tile_html(self.html, "unscanned"))
        self.assertIn('<span class="ops-tag ops-t-bad">3 approvals</span>', unscanned)
        self.assertIn("<b>128</b><u>boxes</u>", unscanned)
        self.assertIn("Scanned<b>1,712</b>", unscanned)

    def test_the_transport_band(self):
        fleet = squash(tile_html(self.html, "fleet"))
        self.assertIn('<span class="ops-tag ops-t-bad">1 out of service</span>', fleet)
        self.assertIn("<b>4/6</b><u>in use</u>", fleet)
        self.assertIn("On transfer<b>1</b>", fleet)
        self.assertIn("On dispatch<b>3</b>", fleet)
        self.assertIn("Idle<b>2</b>", fleet)
        self.assertIn("1 of the idle trucks are out of service.", fleet)

        account = squash(tile_html(self.html, "account"))
        self.assertIn('<span class="ops-tag ops-t-warn">oldest 74 days</span>', account)
        self.assertIn("<thead><tr><th>Days old</th><th>GRPO</th><th>AP</th><th>Payment</th></tr></thead>", account)
        self.assertIn(
            '<tr class="ops-a-new"><th>0–14</th><td>7</td>'
            '<td>15<em class="ops-cash">₹3.36 L</em></td><td>11<em class="ops-cash">₹3.53 L</em></td></tr>',
            account,
        )
        self.assertIn('<tr class="ops-a-bad"><th>45+</th><td>1</td>', account)
        self.assertIn("<th>15–29</th>", account)
        self.assertIn("<th>30–44</th>", account)
        # The GRPO stage is counts only: documents that are not money yet.
        self.assertEqual(account.count("ops-cash"), 8)

        cost = squash(tile_html(self.html, "cost"))
        self.assertIn('<span class="ops-tag ops-t-neut">31 freight GRPOs</span>', cost)
        self.assertIn('<div class="ops-sub">1 Oct – 14 Oct · 4,34,900 L dispatched</div>', cost)
        self.assertIn("<b>₹1.99</b><u>per litre</u>", cost)
        self.assertIn('<span class="k">Freight</span><span class="v">₹1.19</span>', cost)
        self.assertIn('<span class="k">Loading</span><span class="v">₹0.80</span>', cost)

        transit = squash(tile_html(self.html, "transit"))
        self.assertIn('<span class="ops-tag ops-t-bad">1 over 7 days</span>', transit)
        self.assertIn('<div class="ops-sub">Dispatched, no SAP receipt</div>', transit)
        self.assertIn("<b>36.8</b><u>tonnes</u>", transit)
        self.assertIn("Up to 3 days<b>18.4 T</b>", transit)
        self.assertIn("4 - 7 days<b>12.3 T</b>", transit)
        self.assertIn("Over 7 days<b>6.1 T</b>", transit)
        self.assertIn("1 lines have no case weight, so this is a floor.", transit)

    def test_who_is_on_shift(self):
        html = self.flat
        self.assertIn("On shift<b>60</b>", html)
        self.assertIn('<span class="k">Employees</span><span class="n">14</span></div><div class="s">₹8.4k a day</div>', html)
        self.assertIn('<span class="k">Labour</span><span class="n">46</span></div><div class="s">₹20.7k a day</div>', html)
        # Dispatch has no gate department: nobody assigned reads 0 labour,
        # priced at the configured rate, as the factory's strip does.
        self.assertIn("On shift<b>9</b>", html)
        self.assertIn('<span class="k">Employees</span><span class="n">9</span></div><div class="s">₹5.7k a day</div>', html)
        self.assertEqual(html.count('<div class="ops-psplit">'), 3)
        self.assertIn('<i class="ops-f-main" style="width: 23.33%"></i><i class="ops-f-light" style="width: 76.67%"></i>', html)

    def test_widths_never_take_a_locale_separator(self):
        widths = re.findall(r"(?:width|height): ([^;\"%]+)%", self.html)
        self.assertTrue(widths)
        for value in widths:
            self.assertRegex(value, r"^\d+\.\d{2}$")

    def test_a_missing_figure_is_a_rule_and_a_reason_not_a_zero(self):
        # No litres left the gate this month: nothing to divide the freight by.
        payload = build(postgres=factory_db(gate_out=[]))
        cost = squash(tile_html(render(payload), "cost"))
        self.assertIn('<span class="ops-tag ops-t-nil">no source</span>', cost)
        self.assertIn('<div class="ops-sub">Freight and litres from the same bilties</div>', cost)
        self.assertIn('<div class="ops-val"><b class="ops-nil">—</b></div>', cost)
        self.assertIn(
            '<p class="ops-note">No bilty carries a freight figure this month, so there is nothing to divide.</p>', cost
        )
        self.assertNotIn("₹0", cost)

    def test_no_capacity_says_so_rather_than_drawing_a_meter(self):
        payload = build(postgres=factory_db(warehouse_settings=[]))
        stock = squash(tile_html(render(payload), "stock"))
        self.assertIn('<span class="ops-tag ops-t-nil">capacity not set</span>', stock)
        self.assertIn("<b>621</b><u>tonnes</u>", stock)
        self.assertNotIn("ops-meter", stock)
        self.assertIn("No rated capacity is set", stock)


# ------------------------------------------------------- the degraded cases


def unread_tiles(html):
    return re.findall(r'<div class="ops-grp lc-unread" data-tile="([a-z_]+)">', html)


class DegradedTemplateTests(SimpleTestCase):

    def test_everything_unread_still_renders_a_whole_board(self):
        payload = all_unread_board()
        html = render(payload)
        flat = squash(html)

        sections = [
            "stock", "non_moving", "warehouse_settings", "board_settings", "pending_dispatch", "day_plan",
            "allocated", "dispatch", "unscanned", "fleet", "transit", "transporter_account", "grpo_queue",
            "freight_rate", "expense", "employee_roll",
        ]
        for name in sections:
            self.assertIsNone(payload[name], name)
        # Every tile with a feed of its own is unread; the account's matrix
        # is a rule in every cell.
        self.assertEqual(
            unread_tiles(html),
            ["stock", "non_moving", "pending", "allocated", "today", "month", "planned", "unscanned",
             "fleet", "cost", "transit"],
        )
        self.assertEqual(flat.count('<div class="ops-unread"'), 11)
        account = squash(tile_html(html, "account"))
        self.assertIn('<span class="ops-tag ops-t-nil">SAP not read</span>', account)
        self.assertEqual(account.count('<td><span class="ops-nil">—</span></td>'), 12)
        # Not one figure on the wall: every value is a rule.
        values = re.findall(r'<div class="ops-val">(.*?)</div>', flat)
        self.assertEqual(len(values), 10)
        self.assertEqual(set(values), {'<b class="ops-nil">—</b>'})
        self.assertNotIn("₹0", flat)
        self.assertNotIn("<b>0</b>", flat)
        # The topbar and the people strips are rules too.
        self.assertIn('<div class="n ops-nil">—</div><div class="s">head count unavailable</div>', flat)
        self.assertIn("<em>Dispatching days</em> not read", flat)
        self.assertEqual(flat.count('<span class="n"><span class="ops-nil">—</span></span>'), 6)
        self.assertEqual(flat.count('On shift<b><span class="ops-nil">—</span></b>'), 3)
        self.assertNotIn("ops-psplit", flat)
        # Why, on the tile and in the chips.
        self.assertIn("No SAP schema is configured for JIVO_OIL.", flat)
        self.assertIn("The factory database could not be reached: connection refused", flat)
        # Past four unread sections the shell shows one chip that counts them.
        self.assertEqual(flat.count("bcx-meta__chip bcx-meta__chip--degraded"), 1)
        self.assertIn(f"{len(sections)} sections not read", flat)
        self.assertIn("data-clock", flat)

    def test_sap_down_leaves_the_factory_figures_standing(self):
        payload = build(hana_for=hana_factory())
        html = render(payload)

        self.assertEqual(unread_tiles(html), ["stock", "non_moving", "pending", "planned", "cost", "transit"])
        today = squash(tile_html(html, "today"))
        self.assertIn("<b>21.4</b><u>tonnes</u>", today)
        self.assertIn('<p class="ops-note">Could not read today&#x27;s dispatch plan.</p>', today)
        self.assertNotIn("ops-meter", today)
        self.assertIn("<b>4/6</b>", tile_html(html, "fleet"))
        # The GRPO queue is the app's own: its column still counts.
        account = squash(tile_html(html, "account"))
        self.assertIn('<span class="ops-tag ops-t-nil">SAP not read</span>', account)
        self.assertIn('<th>0–14</th><td>7</td><td><span class="ops-nil">—</span></td><td><span class="ops-nil">—</span></td>', account)
        self.assertIn("No SAP schema is configured for JIVO_OIL.", squash(tile_html(html, "stock")))
        self.assertIn("SAP did not answer", html)

    def test_the_factory_database_down_leaves_the_sap_figures_standing(self):
        payload = build(postgres=factory_down())
        html = render(payload)

        self.assertEqual(
            unread_tiles(html), ["pending", "allocated", "today", "month", "planned", "unscanned", "fleet"]
        )
        stock = squash(tile_html(html, "stock"))
        self.assertIn("<b>621</b><u>tonnes</u>", stock)
        self.assertIn('<span class="ops-tag ops-t-nil">capacity not read</span>', stock)
        # Without a capacity the idle stock is a share of what is on hand.
        self.assertIn("6.6% of space", tile_html(html, "non_moving"))
        self.assertIn("<b>36.8</b>", tile_html(html, "transit"))
        cost = squash(tile_html(html, "cost"))
        self.assertIn("Dispatched litres were not read, so there is nothing to divide by.", cost)
        self.assertIn('<b class="ops-nil">—</b>', cost)
        account = squash(tile_html(html, "account"))
        self.assertIn('<span class="ops-tag ops-t-warn">oldest 74 days</span>', account)
        self.assertIn('<th>0–14</th><td><span class="ops-nil">—</span></td><td>15<em class="ops-cash">', account)
        self.assertIn('<div class="n ops-nil">—</div>', squash(html))

    def test_marts_sap_missing_is_named_where_it_is_half_a_figure(self):
        payload = build(hana_for=hana_factory(oil_sap()))
        html = render(payload)
        flat = squash(html)

        self.assertEqual(unread_tiles(html), ["transit"])
        self.assertIn("MART&#x27;s SAP could not be read", squash(tile_html(html, "transit")))
        pending = squash(tile_html(html, "pending"))
        self.assertIn("<b>45.6</b><u>tonnes</u>", pending)
        self.assertIn('<div class="ops-duo"><div><span class="k">OIL</span><span class="v">45.6 T</span></div></div>', pending)
        self.assertIn('<p class="ops-note">Bills missing MART — could not be read.</p>', pending)
        self.assertIn("Plan missing MART — could not read it.", tile_html(html, "today"))
        self.assertIn("Freight account missing MART — could not be read.", tile_html(html, "account"))
        self.assertIn("MART&#x27;s SAP could not be read: No SAP schema is configured for JIVO_MART.", flat)

    def test_one_failing_statement_costs_one_tile(self):
        payload = build(postgres=factory_db(bst=failing()))
        html = render(payload)

        self.assertEqual(payload["meta"]["degraded"], ["fleet"])
        self.assertEqual(unread_tiles(html), ["fleet"])
        fleet = squash(tile_html(html, "fleet"))
        self.assertIn('<span class="ops-tag ops-t-nil">not read</span>', fleet)
        self.assertIn('<div class="ops-val"><b class="ops-nil">—</b></div>', fleet)
        self.assertIn("FactoryQueryError: column does not exist", fleet)
        self.assertIn("<b>36.8</b>", tile_html(html, "transit"))

    def test_a_hand_built_payload_without_reasons_still_says_not_read(self):
        payload = all_unread_board()
        payload["meta"]["reasons"] = {}
        html = render(payload)
        self.assertEqual(len(unread_tiles(html)), 11)
        self.assertIn("This feed could not be read.", html)

    def test_a_payload_with_no_bands_is_one_unread_board(self):
        payload = {"meta": {"degraded": [], "reasons": {}, "warnings": [], "generated_at": NOW.isoformat()}}
        flat = squash(render(payload))
        self.assertIn('<div class="ops-board lc-board" data-board="logistics">', flat)
        self.assertIn("Logistics Control could not be composed", flat)
        self.assertIn('<div class="t" data-clock>14:05</div>', flat)
        self.assertNotIn("ops-stack", flat)


class BeveragesTests(SimpleTestCase):

    def beverages(self):
        sap = RoutedHana(company_code="JIVO_BEVERAGES", schema="BEV_SCHEMA")
        sap.route('AS "GrossWeightPerCase"', [stock_item("FG-BEV-250", 240000, 24, 7.2)])
        sap.route("WITH Stock AS", [])
        sap.route("WITH line_agg AS", [])
        return build(hana_for=hana_factory(sap), company="JIVO_BEVERAGES")

    def test_the_beverages_wall_has_its_own_floor_and_drops_two_tiles(self):
        payload = self.beverages()
        flat = squash(render(payload))

        self.assertIn('<b title="Beverages operations">Beverages operations</b>', flat)
        self.assertIn("BH-FG · JIVO_BEVERAGES", flat)
        self.assertIn("<p>Warehouse <em>BH-FG</em></p>", flat)
        self.assertEqual(flat.count('style="grid-template-columns: repeat(3, minmax(0, 1fr))"'), 2)
        self.assertEqual(flat.count('style="grid-template-columns: repeat(4, minmax(0, 1fr))"'), 1)
        self.assertNotIn("Allocated stock", flat)
        self.assertNotIn("Sent without barcodes", flat)
        transit = squash(tile_html(render(payload), "transit"))
        self.assertIn('<span class="ops-tag ops-t-nil">no route</span>', transit)
        self.assertIn("No intercompany route is configured for Beverages", transit)
        self.assertIn('<b class="ops-nil">—</b>', transit)
