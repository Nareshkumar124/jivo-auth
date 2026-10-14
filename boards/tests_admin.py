"""
Tests for the Admin Control board, ported from the factory's ``admin_board``.

Nothing here opens a connection: the service takes a ``FakePostgres`` and
``FakeHana`` readers that answer each statement by the ``-- admin: <name>``
line it opens with, and ``SimpleTestCase`` refuses any query that would
reach a real database. Two reads need their parameters to answer (the
occupancy read is one statement for every warehouse; the Cost Master read is
one statement for both cost types), so the fakes below route those two.

The fixture is one September plant, hand-built so every figure can be
checked: 1,612 T made against a 4,000 T plan, 1,181.7 T out of the gate,
BH-BT 88% full, GP-FG holding stock nobody rated, and a ₹53.89 L month.
"""

import copy
import json
import re
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings

from . import registry
from .compose import build_board
from .formatting import jsonable
from .services import admin_alerts, admin_tonnage
from .services.admin import (
    DEFAULT_SQFT_PER_PALLET,
    PACKAGING_FLOOR_SQFT,
    TANK_FARM_REASON,
    AdminBoardService,
)
from .services.admin_alerts import build_alerts
from .sources import FactoryQueryError, FactoryUnavailable, SAPConnectionError, SAPDataError, Unavailable
from .templatetags import boards_admin
from .testing import FakeHana, FakePostgres, HanaDown, hana_factory

TODAY = date(2026, 9, 16)  # a Wednesday; 16 of 30 days gone
OIL_ID, MART_ID, BEV_ID = 1, 2, 3

FACTORY_OFF = {
    "NAME": "",
    "USER": "",
    "PASSWORD": "",
    "HOST": "",
    "PORT": 5432,
    "CONNECT_TIMEOUT": 1,
    "STATEMENT_TIMEOUT": 1,
}
HANA_OFF = {
    "HOST": "",
    "PORT": 30015,
    "USER": "",
    "PASSWORD": "",
    "SCHEMAS": {},
    "CONNECT_TIMEOUT": 1,
    "COMMUNICATION_TIMEOUT": 1,
}


def marker(name):
    """The line every statement of the service opens with."""
    return f"-- admin: {name}\n"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class StockHana(FakeHana):
    """A SAP schema whose occupancy read answers per warehouse.

    ``stock`` is ``{warehouse: [row, ...]}`` in the statement's column names
    plus ``Group`` (the item group). The group-102 clause in the statement
    is honoured, so a test can prove the FG tile never counts group 105.
    """

    def __init__(self, stock=None, answers=None, company_code="JIVO_OIL", schema="OIL_SCHEMA"):
        super().__init__(answers, company_code=company_code, schema=schema)
        self.stock = stock or {}

    def rows(self, sql, params=None):
        if marker("occupancy") in sql:
            self.calls.append((sql, params))
            finished_only = '"ItmsGrpCod" IN (102)' in sql
            out = []
            for warehouse in params:
                for row in self.stock.get(warehouse, []):
                    if finished_only and row.get("Group", 102) != 102:
                        continue
                    out.append({"Warehouse": warehouse, **{k: v for k, v in row.items() if k != "Group"}})
            return copy.deepcopy(out)
        return super().rows(sql, params)


class RefusingHana(StockHana):
    """A schema that rejects one named statement."""

    def __init__(self, failing, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failing = marker(failing)

    def rows(self, sql, params=None):
        if self.failing in sql:
            self.calls.append((sql, params))
            raise SAPDataError("SAP rejected this query: invalid column name")
        return super().rows(sql, params)


class Factory(FakePostgres):
    """The factory database. The Cost Master read is routed by its type code
    (the first parameter), because labour and salary share the statement."""

    def __init__(self, answers=None, rates=None, failing=None):
        super().__init__(answers)
        self.rates = rates or {}
        self.failing = marker(failing) if failing else None

    def rows(self, sql, params=None):
        if self.failing and self.failing in sql:
            self.calls.append((sql, params))
            raise FactoryQueryError("column does not exist")
        if marker("cost_rates") in sql:
            self.calls.append((sql, params))
            return copy.deepcopy(self.rates.get(params[0], []))
        return super().rows(sql, params)


def calls_for(fake, name):
    return [(sql, params) for sql, params in fake.calls if marker(name) in sql]


# ---------------------------------------------------------------------------
# the fixture
# ---------------------------------------------------------------------------

#: Litres received onto BH-PF per day. The 6th and 13th are Sundays and made
#: nothing; today, the 16th, made 118.5 T. 1,612 T in all over 14 days.
PRODUCED_LITRES = {
    1: 112_000, 2: 121_500, 3: 109_000, 4: 117_500, 5: 104_000,
    7: 126_000, 8: 119_500, 9: 113_000, 10: 122_000, 11: 108_500,
    12: 90_000, 14: 127_000, 15: 123_500, 16: 118_500,
}


def oil_stock():
    """Oil's SAP stock: BH-BT and GP-FG (finished goods), the PM stores and
    the tank farm."""
    return {
        "BH-BT": [
            # 1 L pouches: 1,20,000 pieces, 12 to a 11.4 kg case = 114 T.
            {"ItemCode": "FG001", "ItemName": "JIVO SOYABEAN 1L POUCH", "OnHand": 120000, "PiecesPerBox": 12,
             "LitresPerPiece": 1, "StockValue": 15_600_000, "Uom": "PCS", "GrossWeightPerCase": 11.4},
            # 5 L jars, 4 to a 19.2 kg case = 192 T.
            {"ItemCode": "FG002", "ItemName": "JIVO MUSTARD 5L JAR", "OnHand": 40000, "PiecesPerBox": 4,
             "LitresPerPiece": 5, "StockValue": 31_000_000, "Uom": "PCS", "GrossWeightPerCase": 19.2},
            # 15 L tins, sold by the piece: one piece is one 15.2 kg case.
            {"ItemCode": "FG003", "ItemName": "JIVO CANOLA 15L TIN", "OnHand": 9000, "PiecesPerBox": 1,
             "LitresPerPiece": 15, "StockValue": 19_000_000, "Uom": "PCS", "GrossWeightPerCase": 15.2},
            # No case weight recorded: disclosed, never weighed as nothing.
            {"ItemCode": "FG004", "ItemName": "JIVO GHEE 500ML JAR", "OnHand": 4000, "PiecesPerBox": 24,
             "LitresPerPiece": 0.5, "StockValue": 1_100_000, "Uom": "PCS", "GrossWeightPerCase": None},
            # Stocked by mass: not a piece unit, so never divided by a pack factor.
            {"ItemCode": "FG005", "ItemName": "JIVO LOOSE GHEE", "OnHand": 800, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 500_000, "Uom": "KG", "GrossWeightPerCase": None},
            # Group 105, standing in BH-BT: not finished goods, not in the tile.
            {"ItemCode": "SF001", "ItemName": "SEMI FINISHED BLEND", "OnHand": 500000, "PiecesPerBox": 1,
             "LitresPerPiece": 1, "StockValue": 9_000_000, "Uom": "PCS", "GrossWeightPerCase": 1.0, "Group": 105},
        ],
        "GP-FG": [
            # 213 T nobody has rated: named beside the total, never in it.
            {"ItemCode": "FG006", "ItemName": "JIVO RICE BRAN 1L", "OnHand": 30000, "PiecesPerBox": 6,
             "LitresPerPiece": 1, "StockValue": 4_000_000, "Uom": "PCS", "GrossWeightPerCase": 42.6},
        ],
        "BH-PM": [
            # 250 to a pallet: 200 pallets.
            {"ItemCode": "PM0000003", "ItemName": "CARTON 1L X 12", "OnHand": 50000, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 1_250_000, "Uom": "PCS", "GrossWeightPerCase": None},
            # 7,00,000 labels to a pallet: 5 pallets.
            {"ItemCode": "PM0000014", "ItemName": "LABEL SOYA 1L", "OnHand": 3_500_000, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 420_000, "Uom": "PCS", "GrossWeightPerCase": None},
            # Not on the stacking sheet: in the value, out of the floor.
            {"ItemCode": "PM0099999", "ItemName": "TRIAL SHRINK FILM", "OnHand": 1000, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 15_000, "Uom": "PCS", "GrossWeightPerCase": None},
        ],
        "BH-BS": [
            {"ItemCode": "PM0000005", "ItemName": "CARTON 5L X 4", "OnHand": 120000, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 2_400_000, "Uom": "PCS", "GrossWeightPerCase": None},
        ],
        "BH-NM": [
            {"ItemCode": "PM0000029", "ItemName": "LABEL MUSTARD 5L", "OnHand": 288000, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 310_000, "Uom": "PCS", "GrossWeightPerCase": None},
        ],
        "BH-PC": [
            {"ItemCode": "PM0000053", "ItemName": "PREFORM 1L", "OnHand": 60000, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 1_840_000, "Uom": "PCS", "GrossWeightPerCase": None},
        ],
        "BH-LO": [
            {"ItemCode": "RM001", "ItemName": "SOYABEAN REFINED LOOSE OIL", "OnHand": 420000, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 50_000_000, "Uom": "LTR", "GrossWeightPerCase": None},
            {"ItemCode": "RM002", "ItemName": "MUSTARD KACHI GHANI LOOSE OIL", "OnHand": 260000, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 39_000_000, "Uom": "LTR", "GrossWeightPerCase": None},
            {"ItemCode": "RM003", "ItemName": "RICE BRAN LOOSE OIL", "OnHand": 140000, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 16_800_000, "Uom": "LTR", "GrossWeightPerCase": None},
            {"ItemCode": "RM004", "ItemName": "CANOLA LOOSE OIL", "OnHand": 49000, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 7_300_000, "Uom": "LTR", "GrossWeightPerCase": None},
            # Kilograms beside the oil: never added to a litre total.
            {"ItemCode": "RM005", "ItemName": "ROSEMARY EXTRACT", "OnHand": 12, "PiecesPerBox": 0,
             "LitresPerPiece": 0, "StockValue": 90_000, "Uom": "KG", "GrossWeightPerCase": None},
        ],
    }


def mart_stock():
    # The Gupta godown: 5,000 cases at 18.8 kg = 94 T.
    return {
        "GP-FGM": [
            {"ItemCode": "FG001", "ItemName": "JIVO SOYABEAN 1L POUCH", "OnHand": 50000, "PiecesPerBox": 10,
             "LitresPerPiece": 1, "StockValue": 6_500_000, "Uom": "PCS", "GrossWeightPerCase": 18.8},
        ]
    }


def oil_answers():
    return [
        (marker("plans"), [
            {"AbsID": 24, "Code": "SEP26", "Name": "OIL Monthly Production Planning for the Sep Month 2026",
             "StartDate": datetime(2026, 9, 1), "EndDate": datetime(2026, 9, 30)},
            {"AbsID": 23, "Code": "AUG26", "Name": "OIL Monthly Production Planning for Aug 2026",
             "StartDate": datetime(2026, 8, 1), "EndDate": datetime(2026, 8, 31)},
        ]),
        # 20,00,000 one-litre pieces and 4,00,000 five-litre ones: 4,000 T.
        (marker("plan_lines"), [
            {"ItemCode": "FG001", "PlannedQty": Decimal("2000000"), "LitresPerUnit": Decimal("1")},
            {"ItemCode": "FG002", "PlannedQty": Decimal("400000"), "LitresPerUnit": Decimal("5")},
        ]),
        # The plan's own actuals: 1,400 T on 10,00,000 of 24,00,000 pieces.
        (marker("plan_produced"), [
            {"ItemCode": "FG001", "ProducedQty": Decimal("900000")},
            {"ItemCode": "FG002", "ProducedQty": Decimal("100000")},
        ]),
        (marker("production_days"), [
            {"Day": datetime(2026, 9, day), "Pieces": Decimal(litres), "Litres": Decimal(litres)}
            for day, litres in PRODUCED_LITRES.items()
        ]),
        (marker("invoiced"), [
            {"Day": datetime(2026, 9, 2), "Bills": 40, "Litres": Decimal("600000")},
            {"Day": datetime(2026, 9, 10), "Bills": 31, "Litres": Decimal("425100")},
        ]),
        (marker("item_columns"), [{"COLUMN_NAME": "ItemCode"}, {"COLUMN_NAME": "U_Gross_Weight"}]),
    ]


def mart_answers():
    return [
        (marker("invoiced"), [{"Day": datetime(2026, 9, 3), "Bills": 12, "Litres": Decimal("180000")}]),
        (marker("item_columns"), [{"COLUMN_NAME": "U_Gross_Weight"}]),
    ]


def oil_hana(**kwargs):
    return StockHana(oil_stock(), oil_answers(), **kwargs)


def mart_hana():
    return StockHana(mart_stock(), mart_answers(), company_code="JIVO_MART", schema="MART_SCHEMA")


def producing_days():
    return [day for day in range(1, 17) if day not in (6, 13)]


def labour_entries():
    """The HOD's split for every producing day: production(oil) 40,
    Warehouse Basement 15, Dock 12, Boiling Floor 1 20. Scrap never staffed.
    87 a day over 14 days = 1,218, newest first as the model orders them."""
    rows = []
    for day in sorted(producing_days(), reverse=True):
        for department_id, count in ((21, 40), (22, 15), (23, 12), (25, 20)):
            rows.append({"company_id": OIL_ID, "department_id": department_id, "work_date": date(2026, 9, day), "count_in": count})
    return rows


def electricity_rows():
    """Eight Oil meters read every day but today, 1,700 units at ₹8.40."""
    return [
        {"date": date(2026, 9, day), "meter": f"Meter {n}", "units": Decimal("1700"), "cost": Decimal("14280.00")}
        for day in range(1, 16)
        for n in range(1, 9)
    ]


LABOUR_RATE = {
    "id": 1, "scope": "FACTORY", "company_id": None, "department_id": None, "department_name": None,
    "basis": "PER_PERSON_DAY", "rate": Decimal("650.0000"), "effective_from": date(2026, 4, 1),
}


def salary_rates():
    """Three department rows, company-agnostic: every company's list carries
    them, and each must still be billed once. ₹50.7 L a month."""
    return [
        {"id": 11, "scope": "DEPARTMENT", "company_id": None, "department_id": 31, "department_name": "Production",
         "basis": "PER_MONTH", "rate": Decimal("1850000"), "effective_from": date(2026, 4, 1)},
        {"id": 12, "scope": "DEPARTMENT", "company_id": None, "department_id": 32, "department_name": "Warehouse",
         "basis": "PER_MONTH", "rate": Decimal("920000"), "effective_from": date(2026, 4, 1)},
        {"id": 13, "scope": "DEPARTMENT", "company_id": None, "department_id": 33, "department_name": "Admin",
         "basis": "PER_MONTH", "rate": Decimal("2300000"), "effective_from": date(2026, 4, 1)},
        # A blanket the department rows outrank: never added on top.
        {"id": 14, "scope": "FACTORY", "company_id": None, "department_id": None, "department_name": None,
         "basis": "PER_MONTH", "rate": Decimal("9999999"), "effective_from": date(2026, 4, 1)},
    ]


def factory_answers():
    return {
        "companies": [
            {"id": OIL_ID, "code": "JIVO_OIL", "name": "Jivo Oil"},
            {"id": MART_ID, "code": "JIVO_MART", "name": "Jivo Mart"},
            {"id": BEV_ID, "code": "JIVO_BEVERAGES", "name": "Jivo Beverages"},
        ],
        "dispatch_by_company": [
            {"company_id": OIL_ID, "weight": Decimal("980400.000"), "trucks": 61, "bills": 88},
            {"company_id": MART_ID, "weight": Decimal("201300.000"), "trucks": 17, "bills": 21},
        ],
        "dispatch_total": [
            {"weight": Decimal("1181700.000"), "trucks": 78, "bills": 109, "today_weight": Decimal("64200.000"), "days": 14}
        ],
        "warehouse_settings": [
            {"company_code": "JIVO_OIL", "warehouse": "BH-BT", "capacity_tonnes": Decimal("502.00"), "last_audit_date": date(2026, 8, 2)},
            {"company_code": "JIVO_MART", "warehouse": "GP-FGM", "capacity_tonnes": Decimal("437.00"), "last_audit_date": date(2026, 9, 10)},
            {"company_code": "JIVO_OIL", "warehouse": "BH-LO", "capacity_tonnes": Decimal("1296.00"), "last_audit_date": None},
        ],
        "plant_settings": [{"sqft_per_pallet": Decimal("15.00")}],
        "expense_settings": [],
        "labour_departments": [
            {"id": 21, "name": "production(oil)"},
            {"id": 22, "name": "Warehouse Basement"},
            {"id": 23, "name": "Dock"},
            {"id": 24, "name": "Scrap"},
            {"id": 25, "name": "Boiling Floor 1"},
        ],
        "labour_entries": labour_entries(),
        "labour_gate": [{"heads": 2150}],
        "electricity": electricity_rows(),
        "maintenance_spares": [
            {"day": date(2026, 9, 3), "quantity": Decimal("4.000"), "unit_cost": Decimal("2350.50")},
            {"day": date(2026, 9, 9), "quantity": Decimal("2.000"), "unit_cost": Decimal("18900.00")},
            {"day": date(2026, 9, 16), "quantity": Decimal("1.000"), "unit_cost": Decimal("4200.00")},
        ],
        "maintenance_indents": [
            {"day": date(2026, 9, 5), "other_charges": Decimal("1500.00"), "lines_total": Decimal("84500.00")},
            {"day": date(2026, 9, 12), "other_charges": None, "lines_total": Decimal("42750.00")},
        ],
    }


def rates():
    return {"factory-labour": [dict(LABOUR_RATE)], "factory-salary": salary_rates()}


def factory(answers=None, rates_by_code=None, failing=None):
    postgres = Factory(rates=rates() if rates_by_code is None else rates_by_code, failing=failing)
    for name, rows in (factory_answers() if answers is None else answers).items():
        postgres.add(marker(name), rows)
    return postgres


def service(postgres=None, *readers, today=TODAY):
    readers = readers or (oil_hana(), mart_hana())
    return AdminBoardService(
        "JIVO_OIL", postgres=postgres if postgres is not None else factory(), hana_for=hana_factory(*readers), today=today
    )


def build(postgres=None, *readers, today=TODAY):
    return service(postgres, *readers, today=today).build()


def full_board():
    """The whole fixture board as the shell would serve it."""
    payload = jsonable(build())
    payload["meta"].update(slide="admin", label="Admin Control", company="JIVO_OIL", generated_at="2026-09-16T14:05:00+05:30")
    return payload


# ---------------------------------------------------------------------------
# tile 1: output
# ---------------------------------------------------------------------------


class ProductionTests(SimpleTestCase):

    def test_the_headline_is_every_receipt_and_the_plan_is_the_target(self):
        production = build()["output"]["production"]

        self.assertEqual(production["mtd_tons"], 1612.0)
        self.assertEqual(production["today_tons"], 118.5)
        self.assertEqual(production["producing_days"], 14)
        self.assertEqual(production["avg_tons_per_producing_day"], 115.14)
        self.assertEqual(production["plan_tons"], 4000.0)
        # The plan's own actuals speak for 1,400 T of it; 212 T was unplanned.
        self.assertEqual(production["planned_items_tons"], 1400.0)
        self.assertEqual(production["unplanned_tons"], 212.0)
        # SAP's attainment on its own lines is in pieces: 10 L of 24 L.
        self.assertEqual(production["planned_items_pct"], 41.7)
        # The bar follows the headline.
        self.assertEqual(production["plan_pct"], 40.3)
        self.assertEqual(production["plan_to_date_tons"], round(4000 * 16 / 30, 2))
        self.assertEqual(production["remaining_days"], 14)
        self.assertEqual(production["required_tons_per_day"], 170.57)
        self.assertEqual(production["plan_name"], "OIL Monthly Production Planning for the Sep Month 2026")
        self.assertEqual(production["warehouse"], "BH-PF")
        self.assertIn("Every production receipt", production["basis"])

    def test_the_trend_keeps_the_days_that_made_nothing(self):
        trend = build()["output"]["production"]["trend"]

        self.assertEqual([day["date"] for day in trend][0], "2026-09-10")
        self.assertEqual(len(trend), 7)
        self.assertEqual(trend[3], {"date": "2026-09-13", "label": "13", "tons": 0.0})
        self.assertEqual(trend[-1]["tons"], 118.5)

    def test_the_movement_read_is_the_floor_and_receipts_only(self):
        hana = oil_hana()
        build(None, hana, mart_hana())

        sql, params = calls_for(hana, "production_days")[0]
        self.assertEqual(params, [59, "BH-PF", date(2026, 9, 1), TODAY])
        self.assertIn('"OIL_SCHEMA"."OINM"', sql)
        self.assertIn('U_IsLitre', sql)
        # The plan's actuals are read over the plan's own window, per item.
        sql, params = calls_for(hana, "plan_produced")[0]
        self.assertEqual(params[:2], [date(2026, 9, 1), date(2026, 9, 30)])
        self.assertEqual(sorted(params[2:]), ["FG001", "FG002"])
        self.assertIn('"TransType" = 59', sql)

    def test_output_beyond_the_plan_never_reads_as_negative(self):
        answers = [(n, r) for n, r in oil_answers() if n != marker("plan_produced")]
        answers.insert(0, (marker("plan_produced"), [{"ItemCode": "FG001", "ProducedQty": Decimal("1900000")}]))
        production = build(None, StockHana(oil_stock(), answers), mart_hana())["output"]["production"]

        self.assertEqual(production["planned_items_tons"], 1900.0)
        self.assertEqual(production["unplanned_tons"], 0)

    def test_with_no_plan_the_floor_still_reports_its_own_output(self):
        answers = [(n, r) for n, r in oil_answers() if n != marker("plans")]
        board = build(None, StockHana(oil_stock(), answers), mart_hana())
        production = board["output"]["production"]

        self.assertEqual(production["mtd_tons"], 1612.0)
        self.assertIsNone(production["plan_tons"])
        self.assertIsNone(production["plan_pct"])
        self.assertIsNone(production["unplanned_tons"])
        self.assertTrue(any("No production plan is filed" in w for w in board["meta"]["warnings"]))
        self.assertIn("production.no_plan", [alert["key"] for alert in board["alerts"]])

    def test_last_months_plan_is_never_passed_off_as_this_months(self):
        answers = [(n, r) for n, r in oil_answers() if n != marker("plans")]
        answers.insert(0, (marker("plans"), [
            {"AbsID": 23, "Code": "AUG26", "Name": "Aug plan", "StartDate": date(2026, 8, 1), "EndDate": date(2026, 8, 31)},
        ]))
        board = build(None, StockHana(oil_stock(), answers), mart_hana())

        self.assertEqual(board["output"]["production"]["plan_name"], "Aug plan")
        self.assertIn("No SAP plan covers today; showing the most recent plan (Aug plan).", board["meta"]["warnings"])

    def test_actuals_sap_refuses_leave_the_plan_standing(self):
        hana = RefusingHana("plan_produced", oil_stock(), oil_answers())
        board = build(None, hana, mart_hana())
        production = board["output"]["production"]

        self.assertEqual(production["planned_items_tons"], 0.0)
        self.assertEqual(production["planned_items_pct"], 0.0)
        self.assertEqual(production["plan_tons"], 4000.0)
        self.assertNotIn("production", board["meta"]["degraded"])

    def test_a_line_produced_with_no_litre_volume_is_counted_not_hidden(self):
        answers = [(n, r) for n, r in oil_answers() if n not in (marker("plan_lines"), marker("plan_produced"))]
        answers += [
            (marker("plan_lines"), [
                {"ItemCode": "FG001", "PlannedQty": Decimal("100"), "LitresPerUnit": Decimal("1")},
                {"ItemCode": "PCS1", "PlannedQty": Decimal("100"), "LitresPerUnit": Decimal("0")},
            ]),
            (marker("plan_produced"), [{"ItemCode": "PCS1", "ProducedQty": Decimal("40")}]),
        ]
        production = build(None, StockHana(oil_stock(), answers), mart_hana())["output"]["production"]

        self.assertEqual(production["unweighed_lines"], 1)


class DispatchTests(SimpleTestCase):

    def test_the_gate_register_adds_oil_and_mart(self):
        dispatch = build()["output"]["dispatch"]

        self.assertEqual(dispatch["mtd_tons"], 1181.7)
        self.assertEqual(dispatch["today_tons"], 64.2)
        self.assertEqual(dispatch["trucks"], 78)
        self.assertEqual(dispatch["bills"], 109)
        self.assertEqual(dispatch["dispatch_days"], 14)
        self.assertEqual(dispatch["avg_tons_per_dispatch_day"], round(1181.7 / 14, 2))
        self.assertEqual(
            dispatch["companies"],
            [
                {"company_code": "JIVO_OIL", "tons": 980.4, "trucks": 61, "bills": 88},
                {"company_code": "JIVO_MART", "tons": 201.3, "trucks": 17, "bills": 21},
            ],
        )
        self.assertIn("left the gate", dispatch["basis"])

    def test_intercompany_customers_are_excluded_per_company_never_merged(self):
        postgres = factory()
        build(postgres)

        sql, params = calls_for(postgres, "dispatch_by_company")[0]
        self.assertIn("gate_core_salesdispatchgateout", sql)
        self.assertEqual(params[:4], [[OIL_ID, MART_ID], date(2026, 9, 1), TODAY, "DISPATCHED"])
        self.assertEqual(sql.count("NOT (g.company_id = %s AND g.customer_code = ANY(%s))"), 2)
        oil_codes, mart_codes = params[5], params[7]
        self.assertEqual((params[4], params[6]), (OIL_ID, MART_ID))
        # A group company to Oil and a real customer to Mart.
        self.assertIn("CUSTA000906", oil_codes)
        self.assertNotIn("CUSTA000906", mart_codes)
        sql, params = calls_for(postgres, "dispatch_total")[0]
        self.assertEqual(params[0], TODAY)

    def test_what_was_billed_rides_beside_what_left(self):
        oil, mart = oil_hana(), mart_hana()
        dispatch = build(None, oil, mart)["output"]["dispatch"]

        # 600 + 425.1 on Oil, 180 on Mart.
        self.assertEqual(dispatch["invoiced_tons"], 1205.1)
        sql, params = calls_for(mart, "invoiced")[0]
        self.assertIn('"MART_SCHEMA"."OINV"', sql)
        self.assertIn("\"CANCELED\" = 'N'", sql)
        self.assertEqual(params[:2], [date(2026, 9, 1), TODAY])
        self.assertIn("CUSTA000874", params)

    def test_sap_down_costs_the_billed_line_and_nothing_else(self):
        board = build(None, oil_hana(), HanaDown("JIVO_MART"))
        dispatch = board["output"]["dispatch"]

        self.assertIsNone(dispatch["invoiced_tons"])
        self.assertEqual(dispatch["mtd_tons"], 1181.7)
        self.assertNotIn("dispatch", board["meta"]["degraded"])

    def test_a_deployment_without_mart_ships_oil_alone_and_says_so(self):
        answers = factory_answers()
        answers["companies"] = [{"id": OIL_ID, "code": "JIVO_OIL", "name": "Jivo Oil"}]
        board = build(factory(answers))

        self.assertEqual([c["company_code"] for c in board["output"]["dispatch"]["companies"]], ["JIVO_OIL"])
        self.assertIn(
            "JIVO_MART is not configured on this deployment, so dispatch is the remaining companies only.",
            board["meta"]["warnings"],
        )
        # Mart is not billed either: 1,025.1 T is Oil's alone.
        self.assertEqual(board["output"]["dispatch"]["invoiced_tons"], 1025.1)

    def test_no_dispatch_company_at_all_is_a_degraded_tile(self):
        answers = factory_answers()
        answers["companies"] = [{"id": BEV_ID, "code": "JIVO_BEVERAGES", "name": "Jivo Beverages"}]
        board = build(factory(answers))

        self.assertIsNone(board["output"]["dispatch"])
        self.assertIn("No dispatch company", board["meta"]["reasons"]["dispatch"])


# ---------------------------------------------------------------------------
# tile 2: storage
# ---------------------------------------------------------------------------


class FgStorageTests(SimpleTestCase):

    def test_finished_goods_weighed_by_case_and_scoped_to_group_102(self):
        fg = build()["storage"]["fg"]
        bt, gupta = fg["rows"]

        # 114 + 192 + 136.8; group 105 and the unweighable rows are out.
        self.assertEqual(bt["tons"], 442.8)
        self.assertEqual(bt["capacity_tons"], 502.0)
        self.assertEqual(bt["used_pct"], 88.2)
        self.assertEqual(bt["free_tons"], 59.2)
        self.assertEqual(bt["last_audit_date"], "2026-08-02")
        self.assertEqual(bt["unweighed_items"], 1)
        self.assertEqual((gupta["label"], gupta["company_code"], gupta["tons"]), ("Gupta", "JIVO_MART", 94.0))
        self.assertEqual(fg["total_tons"], 536.8)
        self.assertEqual(fg["capacity_tons"], 939.0)
        self.assertEqual(fg["used_pct"], 57.2)
        self.assertEqual(fg["free_tons"], 402.2)
        self.assertEqual((fg["unweighed_items"], fg["non_piece_items"]), (1, 1))

    def test_stock_nobody_rated_is_named_beside_the_total_not_inside_it(self):
        fg = build()["storage"]["fg"]

        self.assertEqual(
            fg["unrated"],
            [{"warehouse": "GP-FG", "label": "GP-FG (Oil, Gupta basement)", "company_code": "JIVO_OIL", "tons": 213.0}],
        )
        self.assertNotIn("GP-FG", [row["warehouse"] for row in fg["rows"]])

    def test_each_company_is_read_once_on_its_own_schema(self):
        oil, mart = oil_hana(), mart_hana()
        build(None, oil, mart)

        fg_reads = [params for sql, params in calls_for(oil, "occupancy") if '"ItmsGrpCod" IN (102)' in sql]
        self.assertEqual(fg_reads, [["BH-BT", "GP-FG"]])
        self.assertEqual([params for _, params in calls_for(mart, "occupancy")], [["GP-FGM"]])
        sql, params = calls_for(mart, "item_columns")[0]
        self.assertEqual(params, ["MART_SCHEMA", "OITM"])

    def test_a_store_with_no_rating_leaves_the_total_unrated(self):
        answers = factory_answers()
        answers["warehouse_settings"] = [r for r in answers["warehouse_settings"] if r["warehouse"] != "GP-FGM"]
        fg = build(factory(answers))["storage"]["fg"]

        self.assertIsNone(fg["capacity_tons"])
        self.assertIsNone(fg["used_pct"])
        self.assertIsNone(fg["free_tons"])
        self.assertIsNone(fg["rows"][1]["used_pct"])
        self.assertEqual(fg["total_tons"], 536.8)

    def test_the_weight_field_is_probed_once_per_company(self):
        answers = [(n, r) for n, r in mart_answers() if n != marker("item_columns")]
        mart = StockHana(mart_stock(), answers, company_code="JIVO_MART", schema="MART_SCHEMA")
        oil = oil_hana()
        build(None, oil, mart)

        # A schema without the user field asks for no weight at all, so every
        # row is disclosed as unweighed rather than weighing nothing.
        self.assertIn("CAST(NULL AS DECIMAL)", calls_for(mart, "occupancy")[0][0])
        self.assertIn('NULLIF(COALESCE(i."U_Gross_Weight", 0), 0)', calls_for(oil, "occupancy")[0][0])
        # Three tiles read Oil's stock; the item master is probed once.
        self.assertEqual(len(calls_for(oil, "item_columns")), 1)

    def test_a_probe_sap_refuses_reads_as_no_weight_field(self):
        mart = RefusingHana("item_columns", mart_stock(), mart_answers(), company_code="JIVO_MART", schema="MART_SCHEMA")
        board = build(None, oil_hana(), mart)

        self.assertIn("CAST(NULL AS DECIMAL)", calls_for(mart, "occupancy")[0][0])
        self.assertNotIn("fg_storage", board["meta"]["degraded"])


class PmStorageTests(SimpleTestCase):

    def test_value_and_the_floor_it_stands_on(self):
        pm = build()["storage"]["pm"]

        self.assertEqual(pm["total_value"], 6_235_000.0)
        self.assertEqual(pm["pallets"], 615.0)
        self.assertEqual(pm["sqft_per_pallet"], 15.0)
        self.assertEqual(pm["occupied_sqft"], 9225.0)
        self.assertEqual(pm["floor_sqft"], PACKAGING_FLOOR_SQFT)
        self.assertEqual(pm["free_sqft"], 29675.0)
        self.assertEqual(pm["used_pct"], 23.7)
        self.assertEqual(pm["unmeasured_items"], 1)
        self.assertEqual(pm["unmeasured_pieces"], 1000)
        self.assertEqual(pm["stacking_measured_on"], "2026-09-11")
        self.assertIsNone(pm["capacity_tons"])
        self.assertEqual([row["warehouse"] for row in pm["rows"]], ["BH-BS", "BH-PC", "BH-PM", "BH-NM"])

    def test_an_item_with_no_pallet_figure_is_disclosed(self):
        warnings = build()["meta"]["warnings"]
        self.assertIn(
            "1 packaging items have no pallet figure on the stacking sheet, so the floor used is understated.",
            warnings,
        )

    def test_the_four_stores_are_one_read_on_every_item_group(self):
        oil = oil_hana()
        build(None, oil, mart_hana())

        pm = [(sql, params) for sql, params in calls_for(oil, "occupancy") if params[0] == "BH-PM"]
        self.assertEqual(pm[0][1], ["BH-PM", "BH-BS", "BH-NM", "BH-PC"])
        self.assertNotIn("ItmsGrpCod", pm[0][0])

    def test_a_cleared_footprint_is_honoured_not_defaulted_over(self):
        answers = factory_answers()
        answers["plant_settings"] = [{"sqft_per_pallet": None}]
        pm = build(factory(answers))["storage"]["pm"]

        self.assertIsNone(pm["sqft_per_pallet"])
        self.assertIsNone(pm["used_pct"])
        self.assertIsNone(pm["occupied_sqft"])
        self.assertEqual(pm["total_value"], 6_235_000.0)

    def test_no_settings_row_or_an_unreadable_one_uses_the_measured_default(self):
        answers = factory_answers()
        answers["plant_settings"] = []
        self.assertEqual(build(factory(answers))["storage"]["pm"]["sqft_per_pallet"], DEFAULT_SQFT_PER_PALLET)

        board = build(factory(failing="plant_settings"))
        self.assertEqual(board["storage"]["pm"]["sqft_per_pallet"], DEFAULT_SQFT_PER_PALLET)
        self.assertNotIn("pm_storage", board["meta"]["degraded"])


class OilStorageTests(SimpleTestCase):

    def test_the_tank_farm_in_litres_against_its_rating(self):
        oil = build()["storage"]["oil"]

        # 4,20,000 + 2,60,000 + 1,40,000 + 49,000 L; the rosemary is kg.
        self.assertEqual(oil["total_litres"], 869000)
        self.assertEqual(oil["total_tons"], 869.0)
        self.assertEqual(oil["sap_tons"], 869.0)
        self.assertEqual(oil["capacity_tons"], 1296.0)
        self.assertEqual(oil["used_pct"], 67.1)
        self.assertEqual(oil["source"], "SAP")
        self.assertEqual(oil["no_capacity_reason"], TANK_FARM_REASON)
        self.assertEqual(oil["tank_rows"], [])
        self.assertEqual([row["label"] for row in oil["rows"]][:3], [
            "SOYABEAN REFINED LOOSE OIL", "MUSTARD KACHI GHANI LOOSE OIL", "RICE BRAN LOOSE OIL",
        ])
        self.assertEqual(len(oil["rows"]), 4)

    def test_an_unrated_farm_reports_no_percentage_rather_than_zero(self):
        answers = factory_answers()
        answers["warehouse_settings"] = [r for r in answers["warehouse_settings"] if r["warehouse"] != "BH-LO"]
        board = build(factory(answers))

        self.assertIsNone(board["storage"]["oil"]["used_pct"])
        alert = next(a for a in board["alerts"] if a["key"] == "storage.unrated")
        self.assertEqual(alert["title"], "Oil tanks show no % used")
        self.assertEqual(alert["detail"], TANK_FARM_REASON)


# ---------------------------------------------------------------------------
# tile 3: cost
# ---------------------------------------------------------------------------


def line(cost, key):
    return next(entry for entry in cost["slices"] if entry["key"] == key)


class CostTests(SimpleTestCase):

    def test_the_four_lines_and_their_total(self):
        cost = build()["cost"]

        self.assertEqual([entry["key"] for entry in cost["slices"]], ["labour", "electricity", "salary", "maintenance"])
        self.assertEqual(line(cost, "labour")["amount"], 791_700.0)  # 1,218 x ₹650
        self.assertEqual(line(cost, "electricity")["amount"], 1_713_600.0)  # 120 readings x ₹14,280
        self.assertEqual(line(cost, "salary")["amount"], 2_704_000.0)  # ₹1,69,000 a day x 16
        self.assertEqual(line(cost, "maintenance")["amount"], 180_152.0)
        self.assertEqual(cost["total"], 5_389_452.0)
        self.assertEqual(cost["avg_per_day"], round(5_389_452 / 16, 2))
        self.assertEqual(cost["warnings"], [])
        self.assertTrue(all(entry["has_source"] for entry in cost["slices"]))
        self.assertAlmostEqual(sum(entry["share_pct"] for entry in cost["slices"]), 100, delta=0.2)
        self.assertIn("sub-meters", cost["electricity_note"])

    def test_labour_is_five_departments_and_says_what_share_of_the_gate(self):
        labour = line(build()["cost"], "labour")

        self.assertEqual(labour["detail"], "1,218 across 4 of 5 departments over 14 days")
        self.assertEqual(labour["detail_value"], 1218)
        self.assertIn("1,218 of the 2,150 people the gate counted", labour["basis"])
        self.assertEqual(labour["today"], 56_550.0)
        self.assertEqual((labour["today_detail_value"], labour["today_detail_unit"]), (87, "in"))

    def test_the_departments_are_matched_by_name_and_only_their_rows_priced(self):
        postgres = factory()
        build(postgres)

        _, params = calls_for(postgres, "labour_departments")[0]
        self.assertEqual(params, [["production(oil)", "warehouse basement", "dock", "scrap", "boiling floor 1"]])
        sql, params = calls_for(postgres, "labour_entries")[0]
        self.assertEqual(params[0], [21, 22, 23, 24, 25])
        self.assertEqual(params[1:], [[OIL_ID, MART_ID, BEV_ID], date(2026, 9, 1), TODAY])
        sql, _ = calls_for(postgres, "labour_gate")[0]
        self.assertIn("department_id IS NULL", sql)

    def test_unpriced_people_are_counted_and_named_not_silently_free(self):
        cost = build(factory(rates_by_code={"factory-salary": salary_rates()}))["cost"]
        labour = line(cost, "labour")

        self.assertEqual(labour["amount"], 0.0)
        self.assertFalse(labour["has_source"])
        self.assertIn("1,218 unpriced", labour["detail"])
        self.assertIn("1,218 of the 1,218 labourers on these departments have no rate", labour["warning"])
        self.assertIn(labour["warning"], cost["warnings"])

    def test_a_month_with_nobody_booked_to_a_floor_is_not_read_as_quiet(self):
        answers = factory_answers()
        answers["labour_entries"] = []
        labour = line(build(factory(answers))["cost"], "labour")

        self.assertEqual(labour["detail"], "nobody booked to the 5 departments this month")
        self.assertIn("2,150 labourers came through the gate this month but none is booked", labour["warning"])
        self.assertFalse(labour["has_source"])

    def test_a_department_the_board_names_but_the_site_lacks_is_flagged(self):
        answers = factory_answers()
        answers["labour_departments"] = [r for r in answers["labour_departments"] if r["name"] != "Dock"]
        board = build(factory(answers))

        self.assertIn(
            "Dock is not a department on this deployment, so the labour line prices 4 of the 5 it names.",
            board["meta"]["warnings"],
        )

    def test_a_flat_daily_rate_is_charged_once_per_company_day(self):
        flat = dict(LABOUR_RATE, basis="PER_DAY", rate=Decimal("12000"))
        labour = line(build(factory(rates_by_code={"factory-labour": [flat], "factory-salary": salary_rates()}))["cost"], "labour")

        # Fourteen days, four departments each: fourteen charges, not 56.
        self.assertEqual(labour["amount"], 14 * 12000.0)
        self.assertEqual(labour["today"], 12000.0)

    def test_electricity_is_oils_meters_read_and_today_says_why_it_is_nil(self):
        postgres = factory()
        electricity = line(build(postgres)["cost"], "electricity")

        sql, params = calls_for(postgres, "electricity")[0]
        self.assertIn("maintenance_electricitymeter_companies", sql)
        self.assertEqual(params, [date(2026, 9, 1), TODAY, OIL_ID])
        self.assertEqual(electricity["detail"], "8 Jivo Oil meters · 204,000 units")
        self.assertEqual(electricity["today"], 0.0)
        self.assertEqual(electricity["today_detail"], "no reading entered today")
        self.assertIn("mains included", electricity["basis"])

    def test_a_month_with_no_oil_reading_says_so_rather_than_reading_nil(self):
        answers = factory_answers()
        answers["electricity"] = []
        cost = build(factory(answers))["cost"]
        electricity = line(cost, "electricity")

        self.assertEqual(electricity["amount"], 0.0)
        self.assertFalse(electricity["has_source"])
        self.assertEqual(electricity["detail"], "no Jivo Oil meter read this month")
        self.assertEqual(electricity["warning"], "No reading on a Jivo Oil meter this month — Maintenance › Daily Electricity.")

    def test_salary_is_an_accrual_and_a_shared_rate_is_billed_once(self):
        salary = line(build()["cost"], "salary")

        # ₹50.7 L a month, read once although three companies resolve it.
        self.assertEqual(salary["detail"], "₹50.70 L/month · 16 of 30 days")
        self.assertEqual(salary["detail_value"], 5_070_000.0)
        self.assertEqual(salary["today"], 169_000.0)
        self.assertIsNone(salary["today_detail_unit"])

    def test_no_salary_rate_is_a_line_with_no_source(self):
        cost = build(factory(rates_by_code={"factory-labour": [dict(LABOUR_RATE)]}))["cost"]
        salary = line(cost, "salary")

        self.assertEqual(salary["amount"], 0.0)
        self.assertFalse(salary["has_source"])
        self.assertEqual(
            salary["warning"],
            "No 'factory-salary' rate in force for September 2026 — set one in Admin › Cost Master, "
            "or point this tile at a different cost type in Configuration.",
        )
        self.assertEqual(cost["warnings"], [salary["warning"]])

    def test_maintenance_follows_the_expense_settings(self):
        answers = factory_answers()
        answers["expense_settings"] = [{"maintenance_include_spares": False, "maintenance_include_indents": True}]
        postgres = factory(answers)
        maintenance = line(build(postgres)["cost"], "maintenance")

        self.assertEqual(maintenance["amount"], 128_750.0)
        self.assertEqual(maintenance["today"], 0.0)
        self.assertEqual(calls_for(postgres, "maintenance_spares"), [])
        # Spares are dated in the factory's own time zone.
        _, params = calls_for(factory_and_build(), "maintenance_spares")[0]
        self.assertEqual(params[0], settings.BOARDS_TIME_ZONE)
        self.assertEqual(params[2], ["ISSUE", "CONSUME"])

    def test_a_genuinely_nil_line_has_a_source(self):
        answers = factory_answers()
        answers["maintenance_spares"] = []
        answers["maintenance_indents"] = []
        maintenance = line(build(factory(answers))["cost"], "maintenance")

        self.assertEqual(maintenance["amount"], 0.0)
        self.assertTrue(maintenance["has_source"])
        self.assertIsNone(maintenance["warning"])


def factory_and_build():
    postgres = factory()
    build(postgres)
    return postgres


# ---------------------------------------------------------------------------
# the action centre
# ---------------------------------------------------------------------------


def alert_board(**overrides):
    """A board with everything healthy. Tests break one thing at a time."""
    base = {
        "output": {
            "production": {
                "mtd_tons": 2200.0, "plan_tons": 4320.0, "plan_pct": 50.9,
                "required_tons_per_day": 141.3, "avg_tons_per_producing_day": 157.1,
            },
            "dispatch": {"mtd_tons": 940.0},
        },
        "storage": {
            "fg": {
                "total_tons": 846.0,
                "rows": [
                    {"warehouse": "BH-BT", "label": "BH-BT", "tons": 300.0, "capacity_tons": 502.0,
                     "used_pct": 59.8, "free_tons": 202.0, "last_audit_date": "2026-09-10"},
                ],
                "unrated": [],
            },
            "pm": {"used_pct": 12.0, "no_capacity_reason": None},
            "oil": {"used_pct": 40.0, "no_capacity_reason": None},
        },
        "cost": {
            "total": 2_658_138.0,
            "slices": [
                {"key": "labour", "label": "Labour", "amount": 671_400.0, "has_source": True, "warning": None},
                {"key": "electricity", "label": "Electricity", "amount": 1_986_738.0, "has_source": True, "warning": None},
            ],
            "warnings": [],
        },
        "meta": {"period": {"elapsed_pct": 50.0}},
    }
    base.update(overrides)
    return base


def keys(alerts):
    return [alert["key"] for alert in alerts]


class AlertTests(SimpleTestCase):

    def test_a_plant_doing_well_raises_nothing(self):
        self.assertEqual(build_alerts(alert_board(), today=date(2026, 9, 15)), [])

    def test_a_tile_that_could_not_be_read_raises_no_alert_at_all(self):
        board = alert_board(output={"production": None, "dispatch": None}, storage={"fg": None, "pm": None, "oil": None}, cost=None)
        self.assertEqual(build_alerts(board, today=date(2026, 9, 15)), [])

    def test_the_plan_thresholds(self):
        def production(plan_pct):
            board = alert_board()
            board["output"]["production"]["plan_pct"] = plan_pct
            return build_alerts(board, today=date(2026, 9, 15))

        self.assertEqual(production(46.0), [])  # 4 points behind: a batch not posted yet
        self.assertEqual(production(45.0)[0]["severity"], "warning")
        critical = production(30.0)[0]
        self.assertEqual(critical["severity"], "critical")
        self.assertEqual(critical["title"], "Production is at 30% of plan")
        self.assertIn("needs 141.3 T/day against 157.1 T actual", critical["detail"])
        self.assertEqual(production(60.0), [])

    def test_a_full_store_names_where_the_room_is(self):
        board = alert_board()
        board["storage"]["fg"]["rows"] = [
            {"warehouse": "BH-BT", "label": "BH-BT", "tons": 470.0, "capacity_tons": 502.0, "used_pct": 93.6, "free_tons": 32.0},
            {"warehouse": "GP-FGM", "label": "Gupta", "tons": 100.0, "capacity_tons": 437.0, "used_pct": 22.9, "free_tons": 337.0},
        ]
        alerts = build_alerts(board, today=date(2026, 9, 15))

        self.assertEqual(keys(alerts), ["storage.full.BH-BT"])
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertIn("Gupta is at 23% with 337.0 T free", alerts[0]["detail"])

    def test_unrated_stock_and_a_stale_count_are_chased(self):
        board = alert_board()
        board["storage"]["fg"]["unrated"] = [{"warehouse": "GP-FG", "label": "GP-FG (Oil, Gupta basement)", "tons": 213.0}]
        board["storage"]["fg"]["rows"][0]["last_audit_date"] = "2026-08-02"
        alerts = build_alerts(board, today=date(2026, 9, 15))

        self.assertEqual(keys(alerts), ["storage.unrated_stock.GP-FG", "storage.audit.BH-BT"])
        self.assertEqual(alerts[0]["title"], "213.0 T sits in GP-FG, which is on no storage sheet")
        self.assertEqual(alerts[1]["title"], "BH-BT was last counted 44 days ago")

    def test_unrated_capacity_is_one_alert_naming_both(self):
        board = alert_board()
        board["storage"]["pm"] = {"used_pct": None, "no_capacity_reason": "Rated in square feet."}
        board["storage"]["oil"] = {"used_pct": None, "no_capacity_reason": "No tank rating."}
        alerts = build_alerts(board, today=date(2026, 9, 15))

        self.assertEqual(alerts[0]["title"], "PM stores and oil tanks show no % used")
        self.assertEqual(alerts[0]["detail"], "Rated in square feet. No tank rating.")

    def test_one_unsourced_line_warns_and_two_are_critical(self):
        board = alert_board()
        board["cost"]["slices"][0].update(amount=0.0, has_source=False, warning="no labour rate")
        one = build_alerts(board, today=date(2026, 9, 15))
        self.assertEqual((one[0]["key"], one[0]["severity"]), ("cost.unsourced", "warning"))

        board["cost"]["slices"][1].update(amount=0.0, has_source=False, warning="no reading")
        two = build_alerts(board, today=date(2026, 9, 15))
        self.assertEqual(two[0]["severity"], "critical")
        self.assertEqual(two[0]["title"], "2 of 2 cost lines read nil")

    def test_a_cost_warning_comes_through_once_with_a_stable_key(self):
        board = alert_board()
        board["cost"]["warnings"] = ["No 'factory-salary' rate in force"]
        first = build_alerts(board, today=date(2026, 9, 15))
        second = build_alerts(copy.deepcopy(board), today=date(2026, 9, 15))

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["title"], "A cost rate is missing")
        self.assertEqual(first[0]["key"], second[0]["key"])

    def test_critical_alerts_come_first(self):
        alerts = build()["alerts"]

        self.assertEqual(
            keys(alerts),
            ["production.behind_plan", "storage.full.BH-BT", "storage.unrated_stock.GP-FG", "storage.audit.BH-BT"],
        )
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertEqual(alerts[0]["title"], "Production is at 40% of plan")
        self.assertIn("needs 170.6 T/day against 115.1 T actual", alerts[0]["detail"])
        self.assertEqual(alerts[1]["title"], "BH-BT is 88.2% full")
        self.assertIn("Gupta is at 22% with 343.0 T free", alerts[1]["detail"])

    def test_the_thresholds_the_screen_mirrors(self):
        self.assertEqual(admin_alerts.WAREHOUSE_CRITICAL_PCT, 90.0)
        self.assertEqual(admin_alerts.WAREHOUSE_WARNING_PCT, 80.0)
        self.assertEqual(boards_admin.fill_condition(90), "bad")
        self.assertEqual(boards_admin.fill_condition(80), "warn")
        self.assertEqual(boards_admin.fill_condition(None), "ok")


class TonnageTests(SimpleTestCase):

    def test_weight_is_per_case_over_pieces_per_case(self):
        row = {"uom": "PCS", "on_hand": 120, "gross_weight_per_case": 11.4, "pieces_per_box": 12}
        self.assertAlmostEqual(admin_tonnage.row_kilograms(row), 114.0)

    def test_a_mass_or_blank_unit_is_never_weighed_by_case(self):
        self.assertIsNone(admin_tonnage.row_kilograms({"uom": "KG", "on_hand": 5, "gross_weight_per_case": 1, "pieces_per_box": 1}))
        self.assertIsNone(admin_tonnage.row_kilograms({"uom": "", "on_hand": 5, "gross_weight_per_case": 1, "pieces_per_box": 1}))

    def test_negative_stock_is_carried_not_clamped(self):
        rolled = admin_tonnage.roll_up([{"uom": "PCS", "on_hand": -1000, "gross_weight_per_case": 10, "pieces_per_box": 1}])
        self.assertEqual(rolled["tonnes"], -10.0)

    def test_litres_skips_a_mass_but_keeps_a_volume_and_a_drum(self):
        rows = [
            {"uom": "LTR", "on_hand": 1000},
            {"uom": "KG", "on_hand": 50},
            {"uom": "DRM", "on_hand": 2, "litres_per_piece": 200},
            {"uom": "PCS", "on_hand": 9},
        ]
        self.assertEqual(admin_tonnage.litres(rows), 1400.0)


# ---------------------------------------------------------------------------
# degradation
# ---------------------------------------------------------------------------


class DegradationTests(SimpleTestCase):

    def test_sap_down_keeps_the_gate_and_the_cost_registers(self):
        board = build(factory(), HanaDown("JIVO_OIL"), HanaDown("JIVO_MART"))

        for path in (("output", "production"), ("storage", "pm"), ("storage", "oil"), ("storage", "fg")):
            self.assertIsNone(board[path[0]][path[1]], path)
        self.assertEqual(board["meta"]["degraded"], ["production", "pm_storage", "oil_storage", "fg_storage"])
        self.assertEqual(board["meta"]["reasons"]["fg_storage"], "SAP HANA could not be reached.")
        self.assertEqual(board["output"]["dispatch"]["mtd_tons"], 1181.7)
        self.assertIsNone(board["output"]["dispatch"]["invoiced_tons"])
        self.assertEqual(board["cost"]["total"], 5_389_452.0)
        self.assertTrue(any("SAP did not answer" in w for w in board["meta"]["warnings"]))
        # No tile that was not read produced an alert, and nothing pretended
        # to be clear.
        self.assertEqual(board["alerts"], [])

    def test_one_sap_outage_costs_one_timeout(self):
        class CountingDown(HanaDown):
            attempts = 0

            def table(self, name):
                CountingDown.attempts += 1
                return super().table(name)

        build(factory(), CountingDown("JIVO_OIL"), CountingDown("JIVO_MART"))
        self.assertEqual(CountingDown.attempts, 1)

    def test_the_factory_database_down_keeps_the_sap_tiles(self):
        stand_in = Unavailable(FactoryUnavailable("The factory database could not be reached: refused"))
        board = build(stand_in)

        self.assertIsNone(board["output"]["dispatch"])
        self.assertIsNone(board["cost"])
        self.assertIsNone(board["storage"]["oil"])
        self.assertIsNone(board["storage"]["fg"])
        self.assertEqual(board["output"]["production"]["mtd_tons"], 1612.0)
        # The space settings are tolerated missing: the measured default.
        self.assertEqual(board["storage"]["pm"]["used_pct"], 23.7)
        self.assertEqual(board["meta"]["degraded"], ["dispatch", "oil_storage", "fg_storage", "cost"])
        self.assertIn("could not be reached", board["meta"]["reasons"]["cost"])
        self.assertTrue(any("factory database" in w for w in board["meta"]["warnings"]))

    def test_a_missing_mart_schema_costs_the_tiles_that_read_mart_only(self):
        board = build(factory(), oil_hana(), HanaDown("JIVO_MART", "No SAP schema is configured for JIVO_MART (FACTORY_HANA_SCHEMAS)."))

        self.assertIsNone(board["storage"]["fg"])
        self.assertIn("JIVO_MART", board["meta"]["reasons"]["fg_storage"])
        self.assertIsNotNone(board["storage"]["pm"])
        self.assertIsNotNone(board["storage"]["oil"])
        self.assertIsNotNone(board["output"]["production"])
        self.assertEqual(board["meta"]["degraded"], ["fg_storage"])

    def test_one_failing_statement_costs_the_tiles_that_read_it(self):
        board = build(factory(failing="warehouse_settings"))

        self.assertEqual(board["meta"]["degraded"], ["oil_storage", "fg_storage"])
        self.assertEqual(board["meta"]["reasons"]["oil_storage"], "FactoryQueryError: column does not exist")
        self.assertIsNotNone(board["cost"])
        self.assertIsNotNone(board["storage"]["pm"])

    def test_a_section_never_raises_out_of_build(self):
        answers = factory_answers()
        answers["dispatch_total"] = [{"weight": "not a number", "trucks": "x", "days": None}]
        board = build(factory(answers))

        self.assertIsNone(board["output"]["dispatch"])
        self.assertIn("dispatch", board["meta"]["degraded"])
        self.assertIsNotNone(board["cost"])


# ---------------------------------------------------------------------------
# the payload
# ---------------------------------------------------------------------------


class PayloadTests(SimpleTestCase):

    def test_the_payload_is_plain_json_with_the_factorys_keys(self):
        board = build()
        json.dumps(jsonable(board))

        self.assertEqual(set(board), {"output", "storage", "cost", "alerts", "meta"})
        self.assertEqual(set(board["output"]), {"production", "dispatch"})
        self.assertEqual(set(board["storage"]), {"fg", "pm", "oil"})
        meta = board["meta"]
        self.assertEqual(meta["company_code"], "JIVO_OIL")
        self.assertEqual(meta["scope"], "Jivo Oil · Bhakharpur plant")
        self.assertEqual(
            meta["period"],
            {"from": "2026-09-01", "to": "2026-09-16", "day_of_month": 16, "days_in_month": 30, "elapsed_pct": 53.3},
        )
        self.assertEqual(meta["refresh_seconds"], 60)
        self.assertEqual(meta["withheld"], [])
        self.assertEqual(meta["degraded"], [])
        self.assertIn("1000 litres = 1 ton", meta["tonnage_basis"])

    def test_every_statement_is_named(self):
        postgres, oil, mart = factory(), oil_hana(), mart_hana()
        build(postgres, oil, mart)

        for fake in (postgres, oil, mart):
            for sql, _ in fake.calls:
                self.assertRegex(sql, r"^-- admin: [a-z_]+\n")
        for sql, _ in oil.calls + mart.calls:
            self.assertNotIn("%s", sql)
        for sql, _ in postgres.calls:
            self.assertNotIn("?", sql)

    def test_the_registry_points_at_this_service(self):
        slide = registry.get_slide("admin")
        self.assertIs(slide.service_class(), AdminBoardService)
        for path in (*slide.css, *slide.js):
            self.assertIsNotNone(finders.find(path), path)

    @override_settings(FACTORY_DB=FACTORY_OFF, FACTORY_HANA=HANA_OFF, FACTORY_COMPANY_CODE="JIVO_OIL")
    def test_compose_builds_it_like_any_other_board(self):
        postgres = factory()
        readers = hana_factory(oil_hana(), mart_hana())
        with mock.patch("boards.compose.connect") as connect_mock, mock.patch("boards.compose.hana_for", readers):
            connect_mock.return_value.__enter__.return_value = postgres
            payload = build_board(registry.get_slide("admin"))

        self.assertEqual(payload["meta"]["slide"], "admin")
        self.assertEqual(payload["meta"]["company"], "JIVO_OIL")
        self.assertEqual(payload["meta"]["refresh_seconds"], 60)
        self.assertEqual(payload["output"]["dispatch"]["mtd_tons"], 1181.7)

    @override_settings(FACTORY_DB=FACTORY_OFF, FACTORY_HANA=HANA_OFF, FACTORY_COMPANY_CODE="JIVO_OIL")
    def test_with_both_sources_unconfigured_every_tile_says_why(self):
        payload = build_board(registry.get_slide("admin"))

        self.assertEqual(
            payload["meta"]["degraded"],
            ["production", "dispatch", "pm_storage", "oil_storage", "fg_storage", "cost"],
        )
        self.assertIn("FACTORY_HANA_HOST", payload["meta"]["reasons"]["production"])
        self.assertIn("FACTORY_DB_NAME", payload["meta"]["reasons"]["cost"])
        self.assertEqual(payload["alerts"], [])


# ---------------------------------------------------------------------------
# the screen
# ---------------------------------------------------------------------------


def render(payload):
    return render_to_string(
        "boards/fragments/admin.html",
        {"board": payload, "meta": payload["meta"], "slide": registry.get_slide("admin")},
    )


def all_none_board():
    return {
        "output": {"production": None, "dispatch": None},
        "storage": {"fg": None, "pm": None, "oil": None},
        "cost": None,
        "alerts": None,
        "meta": {
            "company_code": "JIVO_OIL",
            "scope": "Jivo Oil · Bhakharpur plant",
            "date": "2026-09-16",
            "period": {"from": "2026-09-01", "to": "2026-09-16", "day_of_month": 16, "days_in_month": 30, "elapsed_pct": 53.3},
            "generated_at": "2026-09-16T14:05:00+05:30",
            "degraded": ["production", "dispatch", "pm_storage", "oil_storage", "fg_storage", "cost", "alerts"],
            "reasons": {
                "production": "SAP HANA could not be reached.",
                "dispatch": "The factory database could not be reached: refused",
                "pm_storage": "SAP HANA could not be reached.",
                "oil_storage": "SAP HANA could not be reached.",
                "fg_storage": "SAP HANA could not be reached.",
                "cost": "The factory database could not be reached: refused",
                "alerts": "TypeError: boom",
            },
            "warnings": ["SAP did not answer. The tiles that read it show nothing rather than a stale figure."],
            "withheld": [],
        },
    }


class TemplateTests(SimpleTestCase):

    def test_the_whole_board(self):
        html = render(full_board())

        for fragment in (
            'class="ops-board admin-board"', 'class="ops-board__inner"', 'class="ops-topbar"', "ops-pill-live",
            'class="t" data-clock>14:05<', "Wednesday, 16 Sep 2026", "Jivo Oil · Bhakharpur plant",
            "<em>Window</em> 2026-09-01 to 2026-09-16", "<em>Month</em> day 16 of 30",
            "ops-b-dispatch", "ops-b-warehouse", "ops-b-transport",
            "Output <em>month to date</em>", "Storage <em>on hand now</em>", "Cost <em>&amp; action centre</em>",
        ):
            self.assertIn(fragment, html)
        # Topbar totals.
        self.assertIn("1,612.0 T", html)
        self.assertIn("40% of plan", html)
        self.assertIn("1,181.7 T", html)
        self.assertIn("1 need a decision", html)
        self.assertNotIn("ops-unread", html)

    def test_the_output_band(self):
        html = render(full_board())

        # Production: critical gap, today's figure against the average.
        self.assertIn('<span class="ops-tag ops-t-bad">40% of plan</span>', html)
        self.assertIn("<em>Today</em><b>118.5<u>T</u></b><s>against a 115.1 T average</s>", html)
        self.assertIn('<span class="ops-k-mute">Plan<b>4,000.0 T</b></span>', html)
        self.assertIn('<span class="ops-k-mute">Month gone<b>53.3%</b></span>', html)
        self.assertIn('<span class="ops-k-mute">Unplanned<b>212.0 T</b></span>', html)
        self.assertIn('style="width: 40.3%; min-width: var(--meter-h);"', html)
        # Dispatch: Oil and Mart on one bar, the billed line beside it.
        self.assertIn('<span class="ops-tag ops-t-neut">84.4 T a day</span>', html)
        self.assertIn("78 trucks · 109 bills left the gate over 14 days", html)
        self.assertIn("Oil<b>980.4 T</b>", html)
        self.assertIn("Mart<b>201.3 T</b>", html)
        self.assertIn("1,205.1 T billed this month · 23.4 T of it still to ship", html)
        self.assertIn("<em>Today</em><b>64.2<u>T</u></b></div>", html)

    def test_the_storage_band(self):
        html = render(full_board())

        self.assertIn('<span class="ops-tag ops-t-warn">BH-BT 88%</span>', html)
        self.assertIn("Finished goods · 939.0 T rated · 1 SKUs have no case weight", html)
        self.assertIn("<b>536.8</b><u>tonnes</u>", html)
        self.assertIn("<strong>442.8 / 502.0 T</strong>", html)
        self.assertIn("<strong>94.0 / 437.0 T</strong>", html)
        self.assertIn("Outside the rating: GP-FG<b>213.0 T</b>", html)
        self.assertIn("<em>Space used</em><b>57.2%</b><s>402.2 T still free</s>", html)
        # PM at value, its floor in square feet.
        self.assertIn("<b>₹62.35</b><u>L</u>", html)
        self.assertIn("Filled<b>9,225 sq ft</b>", html)
        self.assertIn("Free<b>29,675 sq ft</b>", html)
        self.assertIn("<s>29,675 sq ft free</s>", html)
        # Oil: empty, not used; three varieties by their first word.
        self.assertIn('<span class="ops-tag ops-t-neut">loose oil, BH-LO</span>', html)
        self.assertIn("<em>Empty</em><b>32.9%</b>", html)
        self.assertIn("Free <b>427.0 T</b>", html)
        self.assertIn("Rated <b>1,296.0 T</b>", html)
        self.assertIn("Soyabean <b>420.0 T</b>", html)
        self.assertIn("Mustard <b>260.0 T</b>", html)
        self.assertNotIn("Canola", html)

    def test_the_cost_band(self):
        html = render(full_board())

        self.assertIn('<span class="ops-tag ops-t-ok">all four sourced</span>', html)
        self.assertEqual(html.count('class="adm-arc"'), 4)
        self.assertIn('<div class="adm-hole"><b>₹53.89 L</b><em>1–16 Sep</em></div>', html)
        self.assertIn("<strong>₹7.92 L</strong>", html)
        self.assertIn("<u>1,218 across 4 of 5 departments over 14 days</u><b>₹56,550 · 87 in today</b>", html)
        self.assertIn("<b>no reading entered today</b>", html)
        self.assertIn("<b>₹1.69 L today</b>", html)
        self.assertIn("₹3.37 L a day on average this month", html)
        self.assertIn("--lc: #2563EB", html)

    def test_the_action_centre(self):
        html = render(full_board())

        self.assertIn('<i class="adm-c-critical">1 critical</i>', html)
        self.assertIn('<i class="adm-c-warning">3 warning</i>', html)
        self.assertIn("Production is at 40% of plan", html)
        self.assertIn('data-alert="storage.full.BH-BT"', html)
        self.assertIn("Shift stock", html)
        self.assertNotIn("Nothing needs a decision", html)
        self.assertNotIn("Part of this board could not be read", html)

    def test_a_quiet_plant_says_so_and_a_half_read_one_does_not(self):
        payload = full_board()
        payload["alerts"] = []
        self.assertIn("Nothing needs a decision", render(payload))

        payload["meta"]["degraded"] = ["fg_storage"]
        html = render(payload)
        self.assertNotIn("Nothing needs a decision", html)
        self.assertIn("FG storage — the rules that watch it have raised nothing because they saw nothing.", html)

    def test_a_long_alert_list_is_cut_and_says_so(self):
        payload = full_board()
        payload["alerts"] = [
            {"key": f"k{n}", "severity": "warning", "title": f"Alert {n}", "detail": "d", "action": None} for n in range(9)
        ]
        html = render(payload)
        self.assertIn("Alert 6", html)
        self.assertNotIn("Alert 7", html)
        self.assertIn("2 more alerts below the fold.", html)

    def test_an_unrated_track_is_hatched_never_empty(self):
        payload = full_board()
        payload["storage"]["pm"].update(used_pct=None, occupied_sqft=None, free_sqft=None, sqft_per_pallet=None)
        payload["storage"]["oil"].update(used_pct=None, capacity_tons=None)
        html = render(payload)

        self.assertEqual(html.count('class="ops-meter adm-unknown"'), 2)
        self.assertIn("pallet footprint not set", html)
        self.assertIn("no tank rating yet", html)
        self.assertIn("8,69,000 litres in tank · 1,000 L = 1 T", html)

    def test_an_unsourced_cost_line_keeps_its_row_and_loses_its_arc(self):
        payload = full_board()
        salary = next(entry for entry in payload["cost"]["slices"] if entry["key"] == "salary")
        salary.update(amount=0.0, has_source=False, warning="No 'factory-salary' rate", today=0.0, share_pct=None)
        html = render(payload)

        self.assertEqual(html.count('class="adm-arc"'), 3)
        self.assertIn('<span class="ops-tag ops-t-bad">1 of 4 unsourced</span>', html)
        self.assertIn('<div class="adm-lrow adm-zero" style="--lc: #7C3AED" title="No &#x27;factory-salary&#x27; rate">', html)
        self.assertIn("<span>no rate</span>", html)

    def test_everything_unread_still_renders_a_whole_board(self):
        html = render(all_none_board())

        # Six tiles and the action centre, each saying why.
        self.assertEqual(html.count('class="ops-unread adm-unread"'), 7)
        self.assertIn("SAP HANA could not be reached.", html)
        self.assertIn("The factory database could not be reached: refused", html)
        self.assertEqual(html.count('<b class="ops-nil">—</b>'), 5)
        self.assertEqual(html.count('class="n ops-nil"'), 3)
        # Not "no plan filed", not "none critical": nothing was read.
        self.assertEqual(html.count('<div class="s">not read</div>'), 2)
        self.assertIn("data-clock", html)
        # The shell's chip strip names them too.
        self.assertIn("bcx-meta__chip--degraded", html)
        self.assertNotIn("₹0", html)
        self.assertNotIn("0.0 T", html)
        self.assertNotIn("adm-arc", html)

    def test_the_composed_payload_with_everything_down_renders(self):
        payload = jsonable(build(Unavailable(FactoryUnavailable("refused")), HanaDown("JIVO_OIL"), HanaDown("JIVO_MART")))
        html = render(payload)

        self.assertEqual(html.count('class="ops-unread adm-unread"'), 6)
        # The alerts ran over nothing and say the board is half-read.
        self.assertIn("Part of this board could not be read", html)
        self.assertNotIn("Nothing needs a decision", html)


class FormatTests(SimpleTestCase):
    """The page's own formatters, utils/format.ts."""

    def test_tons_rounds_half_up_and_drops_the_decimal_past_ten_thousand(self):
        self.assertEqual(boards_admin.tons(12.96), "13.0")
        self.assertEqual(boards_admin.tons(1620), "1,620.0")
        self.assertEqual(boards_admin.tons(12345.6), "12,346")
        self.assertEqual(boards_admin.tons(None), "—")
        self.assertEqual(boards_admin.tons("n/a"), "—")
        self.assertEqual(boards_admin.tons(-4.25), "-4.3")

    def test_money_at_the_scale_a_factory_reads_it(self):
        self.assertEqual(boards_admin.money(5_389_452), "₹53.89 L")
        self.assertEqual(boards_admin.money(33_900_000), "₹3.39 Cr")
        self.assertEqual(boards_admin.money(56_550), "₹56,550")
        self.assertEqual(boards_admin.money_parts(6_235_000), {"value": "₹62.35", "unit": "L"})
        self.assertEqual(boards_admin.money_parts(None), {"value": "—", "unit": ""})

    def test_percentages_and_words(self):
        self.assertEqual(boards_admin.pct(57.15), "57.2%")
        self.assertEqual(boards_admin.pct_rough(40.5), "41%")
        self.assertEqual(boards_admin.short_oil("SOYABEAN REFINED LOOSE OIL"), "Soyabean")
        self.assertEqual(boards_admin.short_window("2026-09-01", "2026-09-21"), "1–21 Sep")
        self.assertEqual(boards_admin.long_date("2026-09-16"), "Wednesday, 16 Sep 2026")

    def test_today_on_a_cost_line(self):
        label = boards_admin._today_label
        self.assertEqual(label({"today": 34450, "today_detail_value": 53, "today_detail_unit": "in", "amount": 1}), "₹34,450 · 53 in today")
        self.assertEqual(label({"today": 99766, "today_detail_value": None, "today_detail_unit": None, "amount": 1}), "₹99,766 today")
        self.assertEqual(label({"today": 0, "today_detail": "no reading entered today", "amount": 5}), "no reading entered today")
        self.assertEqual(label({"today": 0, "today_detail": None, "amount": 5}), "nothing today")
        self.assertIsNone(label({"today": 0, "today_detail": None, "amount": 0}))

    def test_the_donut_takes_its_gaps_off_the_arcs(self):
        view = boards_admin.present(full_board())["cost"]
        lengths = [float(arc["dash"].split()[0]) for arc in view["arcs"]]
        # Four arcs and four gaps make the whole ring.
        self.assertAlmostEqual(sum(lengths) + 4 * boards_admin.GAP, boards_admin.CIRCUMFERENCE, places=2)
        self.assertEqual(view["arcs"][0]["offset"], "0.000")
        self.assertTrue(re.match(r"^-\d+\.\d{3}$", view["arcs"][1]["offset"]))
