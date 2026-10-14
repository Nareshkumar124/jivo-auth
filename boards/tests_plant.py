"""
Tests for the plant board, ported from the factory's ``plant_board``.

Nothing here opens a connection. The service takes a ``FakePostgres`` and a
``FakeHana`` that answer each statement by the ``-- plant: <name>`` line it
opens with, and ``SimpleTestCase`` refuses any query that would reach a real
database.

The fixture is one hand-built mid-September at the Oil plant (today is
16 September 2026, day 16 of a 30-day plan), sized so every figure on the
wall can be checked by hand. The tests guard the decisions a later reader
would otherwise "tidy up": output is the whole floor and not the plan's
share of it, stock is aged from when it last LEFT, waste is dated by its run
and valued in money, the two shifting registers are never netted, losing SAP
costs the shipped tile its unit and not its figures, and a tile that cannot
be read is a reason on the wall, never a zero.
"""

import json
import re
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings

from . import registry
from .compose import build_board
from .services.plant import PlantBoardService, classify_material, movement_status
from .services.stacking import PIECES_PER_PALLET, pieces_per_pallet
from .services.plant_view import present
from .sources import FactoryQueryError, FactoryUnavailable, SAPDataError, Unavailable
from .testing import FakeHana, FakePostgres, HanaDown, hana_factory

TODAY = date(2026, 9, 16)
COMPANY_ID = 3

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

#: Every tile, in board order.
TILES = (
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
)
SAP_TILES = (
    "purchase_plan",
    "purchased",
    "benchmark",
    "open_pos",
    "stock_space",
    "non_moving",
    "monthly_planning",
    "floor_stock",
    "wastage",
)
FACTORY_TILES = ("stock_space", "pm_in", "blowing", "today_lines", "wastage", "declared", "shipped", "workforce")


def marker(name):
    return f"-- plant: {name}\n"


def d(day):
    return date(2026, 9, day)


# ---------------------------------------------------------------- the fixture


def sap_answers():
    """SAP for one mid-September: the plan, its packing requirement, the
    stores, the floor's output and stock, and the item master's litres."""
    plan_days = [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 14, 15, 16]
    return dict(
        plans=[
            {
                "AbsID": 24,
                "Code": "OIL-SEP-26",
                "Name": "OIL Monthly Production Planning for the September 2026",
                "StartDate": datetime(2026, 9, 1),
                "EndDate": datetime(2026, 9, 30),
            },
            {
                "AbsID": 23,
                "Code": "OIL-AUG-26",
                "Name": "OIL Monthly Production Planning for the August 2026",
                "StartDate": datetime(2026, 8, 1),
                "EndDate": datetime(2026, 8, 31),
            },
        ],
        # Planning value: 7,44,000 + 11,10,000 + 2,79,000 + 16,80,000 = 38,13,000.
        requirement=[
            {"ItemCode": "PM0000059", "PlanningQty": Decimal("1200000"), "SkuCount": 9, "UnitPrice": Decimal("0.62")},
            {"ItemCode": "PM0000003", "PlanningQty": Decimal("60000"), "SkuCount": 3, "UnitPrice": Decimal("18.5")},
            {"ItemCode": "PM0000014", "PlanningQty": Decimal("900000"), "SkuCount": 7, "UnitPrice": Decimal("0.31")},
            {"ItemCode": "PM0000057", "PlanningQty": Decimal("400000"), "SkuCount": 4, "UnitPrice": Decimal("4.2")},
            {"ItemCode": "PM0000999", "PlanningQty": Decimal("5000"), "SkuCount": 1, "UnitPrice": Decimal("0")},
        ],
        # Reached BH-PC plan-to-date: 3,10,000 + 4,62,500 + 1,30,200 + 6,30,000 = 15,32,700.
        pm_received=[
            {"ItemCode": "PM0000059", "ReceivedQty": Decimal("500000")},
            {"ItemCode": "PM0000003", "ReceivedQty": Decimal("25000")},
            {"ItemCode": "PM0000014", "ReceivedQty": Decimal("420000")},
            {"ItemCode": "PM0000057", "ReceivedQty": Decimal("150000")},
            {"ItemCode": "PM0000777", "ReceivedQty": Decimal("8000")},  # not the plan's
        ],
        # Held in the feeding stores: 1,86,000 + 1,85,000 + 62,000 + 2,10,000 = 6,43,000.
        pm_on_hand=[
            {"ItemCode": "PM0000059", "OnHand": Decimal("300000")},
            {"ItemCode": "PM0000003", "OnHand": Decimal("10000")},
            {"ItemCode": "PM0000014", "OnHand": Decimal("200000")},
            {"ItemCode": "PM0000057", "OnHand": Decimal("50000")},
        ],
        # Open book: 1,55,000 + 5,55,000 + 4,20,000 = 11,30,000. Caps and PET
        # are past due (and short); the cartons are due after today.
        pm_open_po=[
            {"ItemCode": "PM0000059", "OpenQty": Decimal("250000"), "EarliestDue": datetime(2026, 9, 5), "OpenLines": 2},
            {"ItemCode": "PM0000003", "OpenQty": Decimal("30000"), "EarliestDue": datetime(2026, 9, 25), "OpenLines": 1},
            {"ItemCode": "PM0000057", "OpenQty": Decimal("100000"), "EarliestDue": datetime(2026, 8, 20), "OpenLines": 1},
        ],
        purchase_orders=[
            {"DocEntry": 501, "ItemCode": "PM0000059", "Lines": 1, "OrderedQty": 200000, "OpenQty": 150000,
             "OrderedValue": Decimal("124000"), "OpenValue": Decimal("93000"), "ClosedLines": 0},
            {"DocEntry": 502, "ItemCode": "PM0000003", "Lines": 2, "OrderedQty": 30000, "OpenQty": 30000,
             "OrderedValue": Decimal("555000"), "OpenValue": Decimal("555000"), "ClosedLines": 0},
            {"DocEntry": 503, "ItemCode": "PM0000014", "Lines": 1, "OrderedQty": 300000, "OpenQty": 0,
             "OrderedValue": Decimal("93000"), "OpenValue": Decimal("0"), "ClosedLines": 1},
            {"DocEntry": 503, "ItemCode": "PM0000057", "Lines": 1, "OrderedQty": 120000, "OpenQty": 40000,
             "OrderedValue": Decimal("504000"), "OpenValue": Decimal("168000"), "ClosedLines": 0},
        ],
        open_po_age=[
            {"ItemCode": "PM0000059", "DocDate": datetime(2026, 9, 4), "Lines": 1, "OpenQty": Decimal("150000")},
            {"ItemCode": "PM0000059", "DocDate": datetime(2026, 7, 10), "Lines": 1, "OpenQty": Decimal("100000")},
            {"ItemCode": "PM0000003", "DocDate": datetime(2026, 9, 8), "Lines": 1, "OpenQty": Decimal("30000")},
            {"ItemCode": "PM0000057", "DocDate": datetime(2026, 9, 10), "Lines": 1, "OpenQty": Decimal("40000")},
            {"ItemCode": "PM0000057", "DocDate": datetime(2026, 6, 1), "Lines": 1, "OpenQty": Decimal("60000")},
        ],
        benchmark=[{"Total": 142, "Healthy": 127, "Low": 9, "Critical": 6}],
        # 1,200 + 200 + 100 + 20 + 0.6 pallets = 1,520.6 at 15 sq ft = 22,809.
        packaging_stock=[
            {"Warehouse": "BH-PM", "ItemCode": "PM0000003", "Pieces": Decimal("300000"), "Value": Decimal("5550000"), "Unpriced": 0},
            {"Warehouse": "BH-BS", "ItemCode": "PM0000057", "Pieces": Decimal("140000"), "Value": Decimal("588000"), "Unpriced": 0},
            {"Warehouse": "BH-NM", "ItemCode": "PM0000005", "Pieces": Decimal("60000"), "Value": Decimal("480000"), "Unpriced": 0},
            {"Warehouse": "BH-PM", "ItemCode": "PM0000059", "Pieces": Decimal("1200000"), "Value": Decimal("744000"), "Unpriced": 0},
            {"Warehouse": "BH-PC", "ItemCode": "PM0000014", "Pieces": Decimal("420000"), "Value": Decimal("130200"), "Unpriced": 0},
            {"Warehouse": "BH-NM", "ItemCode": "ZZ0000001", "Pieces": Decimal("5000"), "Value": Decimal("0"), "Unpriced": 1},
        ],
        non_moving=[
            {"ItemCode": "PM0000201", "WhsCode": "BH-NM", "Quantity": 20000, "Value": Decimal("380000"), "Days": 210},
            {"ItemCode": "PM0000201", "WhsCode": "BH-PM", "Quantity": 5000, "Value": Decimal("95000"), "Days": 210},
            {"ItemCode": "PM0000202", "WhsCode": "BH-NM", "Quantity": 3000, "Value": Decimal("120000"), "Days": 48},
            {"ItemCode": "PM0000203", "WhsCode": "BH-BS", "Quantity": 10000, "Value": Decimal("60000"), "Days": 35},
            {"ItemCode": "PM0000204", "WhsCode": "BH-PM", "Quantity": 2000, "Value": Decimal("15000"), "Days": 12},
            {"ItemCode": "PM0000205", "WhsCode": "BH-PC", "Quantity": 1000, "Value": Decimal("2000"), "Days": 3},
        ],
        # Planned litres: 9,00,000 + 6,00,000 + 90,000 + 60,000 = 16,50,000 (1,650 t).
        plan_lines=[
            {"ItemCode": "FG0000101", "PlannedQty": Decimal("900000"), "PiecesPerCase": 12, "LitresPerUnit": Decimal("1")},
            {"ItemCode": "FG0000102", "PlannedQty": Decimal("120000"), "PiecesPerCase": 4, "LitresPerUnit": Decimal("5")},
            {"ItemCode": "FG0000103", "PlannedQty": Decimal("6000"), "PiecesPerCase": 1, "LitresPerUnit": Decimal("15")},
            {"ItemCode": "FG0000104", "PlannedQty": Decimal("300000"), "PiecesPerCase": 48, "LitresPerUnit": Decimal("0.2")},
            {"ItemCode": "FG0000105", "PlannedQty": Decimal("20000"), "PiecesPerCase": 24, "LitresPerUnit": Decimal("0")},
        ],
        # On the plan's own items: 8,11,500 litres (811.5 t).
        plan_produced=[
            {"ItemCode": "FG0000101", "ProducedQty": Decimal("430000")},
            {"ItemCode": "FG0000102", "ProducedQty": Decimal("64000")},
            {"ItemCode": "FG0000103", "ProducedQty": Decimal("2500")},
            {"ItemCode": "FG0000104", "ProducedQty": Decimal("120000")},
            {"ItemCode": "FG0000105", "ProducedQty": Decimal("9000")},
        ],
        # The whole floor: 9,05,000 litres (905 t) and 12,80,000 pieces over 14 days.
        floor_production=[
            {
                "Day": datetime(2026, 9, day),
                "Pieces": Decimal("84000" if day == 16 else "92000"),
                "Litres": Decimal("60000" if day == 16 else "65000"),
            }
            for day in plan_days
        ],
        floor_stock=[
            {"ItemCode": "FG0000101", "OnHand": 120000, "PiecesPerBox": 12, "LitresPerPiece": Decimal("1"), "StockValue": Decimal("18600000")},
            {"ItemCode": "FG0000102", "OnHand": 30000, "PiecesPerBox": 4, "LitresPerPiece": Decimal("5"), "StockValue": Decimal("23250000")},
            {"ItemCode": "FG0000103", "OnHand": 1500, "PiecesPerBox": 1, "LitresPerPiece": Decimal("15"), "StockValue": Decimal("3375000")},
            {"ItemCode": "FG0000104", "OnHand": 60000, "PiecesPerBox": 48, "LitresPerPiece": Decimal("0.2"), "StockValue": Decimal("1500000")},
            {"ItemCode": "FG0000105", "OnHand": 4000, "PiecesPerBox": 24, "LitresPerPiece": Decimal("0"), "StockValue": Decimal("2000000")},
            {"ItemCode": "FG0000106", "OnHand": -20, "PiecesPerBox": 0, "LitresPerPiece": Decimal("1"), "StockValue": Decimal("-3000")},
        ],
        last_out=[
            {"ItemCode": "FG0000101", "LastOut": datetime(2026, 9, 15)},  # 1 day: fresh
            {"ItemCode": "FG0000102", "LastOut": datetime(2026, 9, 10)},  # 6 days
            {"ItemCode": "FG0000103", "LastOut": datetime(2026, 8, 30)},  # 17 days
            {"ItemCode": "FG0000105", "LastOut": datetime(2026, 9, 14)},  # 2 days, unweighed
            # FG0000104 has never left BH-PF.
        ],
        classify=[
            {"ItemCode": "PM0000057", "ItemGroup": "PACKAGING MATERIAL"},
            {"ItemCode": "PM0000014", "ItemGroup": "PACKAGING MATERIAL"},
            {"ItemCode": "PM0000059", "ItemGroup": "PACKAGING MATERIAL"},
            {"ItemCode": "PM0000003", "ItemGroup": "PACKAGING MATERIAL"},
            {"ItemCode": "PM0000071", "ItemGroup": "PACKAGING MATERIAL"},
            {"ItemCode": "RM0000001", "ItemGroup": "RAW MATERIAL"},
        ],
        litres=[
            {"ItemCode": "FG0000101", "Litres": Decimal("1")},
            {"ItemCode": "FG0000102", "Litres": Decimal("5")},
            {"ItemCode": "FG0000105", "Litres": Decimal("0")},
        ],
    )


def blowing_days():
    rows = [
        (2, 2, 21000, 300, 2, 52000, 41000, 20700),
        (3, 2, 22500, 250, 2, 55000, 43500, 22250),
        (5, 1, 11000, 120, 1, 27500, 21300, 10880),
        (10, 2, 24000, 260, 2, 59000, 46500, 23740),
        (11, 2, 23000, 240, 2, 56500, 44600, 22760),
        (12, 2, 25500, 310, 2, 62500, 49400, 25190),
        (14, 2, 20000, 200, 1, 26000, 20500, 9900),
        (15, 2, 26000, 280, 2, 64000, 50300, 25720),
        (16, 1, 9000, 90, 0, 0, 0, 0),
    ]
    return [
        {
            "day": d(day),
            "runs": runs,
            "bottles": bottles,
            "rejected": rejected,
            "costed_runs": costed,
            "net_cost": Decimal(net),
            "preform_cost": Decimal(preform),
            "good_bottles": good,
        }
        for day, runs, bottles, rejected, costed, net, preform, good in rows
    ]


def factory_answers():
    """The factory database for the same day."""
    return dict(
        company=[{"id": COMPANY_ID}],
        store_settings=[
            {"warehouse": "BH-PM", "capacity_tonnes": Decimal("120"), "last_audit_date": date(2026, 7, 1)},
            {"warehouse": "BH-BS", "capacity_tonnes": Decimal("60"), "last_audit_date": date(2026, 8, 20)},
            {"warehouse": "BH-PC", "capacity_tonnes": None, "last_audit_date": date(2026, 9, 1)},
        ],
        plant_settings=[{"sqft_per_pallet": Decimal("15.00")}],
        pm_in=[
            {"vehicles": 4, "pos": 6, "lines": 11, "received": Decimal("182500"),
             "accepted": Decimal("96000"), "rejected": Decimal("1500")}
        ],
        blowing_days=blowing_days(),
        blowing_running=[
            {"runs": 2, "bottles": 14200, "preform_cost": Decimal("39800"), "blowing_cost": Decimal("7350"),
             "uncosted": 0, "started_earlier": 0}
        ],
        today_runs=[
            {"id": 81, "line_id": 1, "status": "COMPLETED", "required_qty": Decimal("1200"),
             "total_production": Decimal("1200"), "pieces_per_case": 12, "segment_cases": Decimal("1180")},
            {"id": 82, "line_id": 1, "status": "IN_PROGRESS", "required_qty": Decimal("800"),
             "total_production": Decimal("0"), "pieces_per_case": 20, "segment_cases": Decimal("410")},
            {"id": 83, "line_id": 2, "status": "IN_PROGRESS", "required_qty": Decimal("1500"),
             "total_production": Decimal("0"), "pieces_per_case": 4, "segment_cases": Decimal("950")},
            {"id": 84, "line_id": 3, "status": "DRAFT", "required_qty": Decimal("600"),
             "total_production": Decimal("0"), "pieces_per_case": 12, "segment_cases": Decimal("0")},
            {"id": 85, "line_id": 3, "status": "DRAFT", "required_qty": Decimal("700"),
             "total_production": Decimal("0"), "pieces_per_case": None, "segment_cases": Decimal("0")},
        ],
        waste_logs=[
            # 14 September, typed two days later: the run's date is the day.
            {"material_code": "PM0000057", "wastage_qty": Decimal("320"), "wastage_approval_status": "FULLY_APPROVED",
             "uom": "PCS", "production_run_id": 71, "run_date": d(14), "typed_on": d(16)},
            {"material_code": "PM0000014", "wastage_qty": Decimal("900"), "wastage_approval_status": "PENDING",
             "uom": "pcs", "production_run_id": 71, "run_date": d(14), "typed_on": d(16)},
            {"material_code": "PM0000059", "wastage_qty": Decimal("1500"), "wastage_approval_status": "PENDING",
             "uom": "PCS", "production_run_id": 72, "run_date": d(14), "typed_on": d(16)},
            {"material_code": "PM0000003", "wastage_qty": Decimal("40"), "wastage_approval_status": "PENDING",
             "uom": "PCS", "production_run_id": 72, "run_date": d(14), "typed_on": d(16)},
            {"material_code": "RM0000001", "wastage_qty": Decimal("85"), "wastage_approval_status": "PENDING",
             "uom": "LTR", "production_run_id": 72, "run_date": d(14), "typed_on": d(16)},
            {"material_code": "PM0000071", "wastage_qty": Decimal("12"), "wastage_approval_status": "PENDING",
             "uom": "KG", "production_run_id": 72, "run_date": d(14), "typed_on": d(16)},
            # Earlier in the week.
            {"material_code": "PM0000057", "wastage_qty": Decimal("200"), "wastage_approval_status": "FULLY_APPROVED",
             "uom": "PCS", "production_run_id": 60, "run_date": d(10), "typed_on": d(11)},
            {"material_code": "PM0000014", "wastage_qty": Decimal("600"), "wastage_approval_status": "FULLY_APPROVED",
             "uom": "PCS", "production_run_id": 63, "run_date": d(11), "typed_on": d(12)},
            {"material_code": "PM0000059", "wastage_qty": Decimal("900"), "wastage_approval_status": "PENDING",
             "uom": "PCS", "production_run_id": 66, "run_date": d(12), "typed_on": d(14)},
            # Standalone: no run, so it keeps the day it was typed.
            {"material_code": "PM0000003", "wastage_qty": Decimal("25"), "wastage_approval_status": "PENDING",
             "uom": "PCS", "production_run_id": None, "run_date": None, "typed_on": d(11)},
        ],
        waste_prices=[
            {"production_run_id": 60, "material_code": "PM0000057", "unit_price": Decimal("4.1")},
            {"production_run_id": 63, "material_code": "PM0000014", "unit_price": Decimal("0.30")},
            {"production_run_id": 66, "material_code": "PM0000059", "unit_price": Decimal("0.60")},
            {"production_run_id": 71, "material_code": "PM0000057", "unit_price": Decimal("4.2")},
            {"production_run_id": 71, "material_code": "PM0000014", "unit_price": Decimal("0.31")},
            {"production_run_id": 72, "material_code": "PM0000059", "unit_price": Decimal("0.62")},
            {"production_run_id": 72, "material_code": "PM0000003", "unit_price": Decimal("18.5")},
            {"production_run_id": 72, "material_code": "RM0000001", "unit_price": Decimal("118")},
            {"production_run_id": 72, "material_code": "PM0000071", "unit_price": Decimal("165")},
        ],
        declared=[
            {"movement_id": 1, "destination_kind": "GODOWN", "to_warehouse": "BH-BT", "is_active": True,
             "item_code": "FG0000101", "pieces": 24000, "pieces_per_box": 12, "litres_per_piece": Decimal("1")},
            {"movement_id": 1, "destination_kind": "GODOWN", "to_warehouse": "BH-BT", "is_active": True,
             "item_code": "FG0000102", "pieces": 4000, "pieces_per_box": 4, "litres_per_piece": Decimal("5")},
            {"movement_id": 2, "destination_kind": "DISPATCH", "to_warehouse": "", "is_active": True,
             "item_code": "FG0000103", "pieces": 300, "pieces_per_box": 1, "litres_per_piece": Decimal("15")},
            {"movement_id": 3, "destination_kind": "GODOWN", "to_warehouse": "BH-BT", "is_active": False,
             "item_code": "FG0000101", "pieces": 6000, "pieces_per_box": 12, "litres_per_piece": Decimal("1")},
            {"movement_id": 4, "destination_kind": "GODOWN", "to_warehouse": "GP-FG", "is_active": True,
             "item_code": "FG0000104", "pieces": 12000, "pieces_per_box": 48, "litres_per_piece": Decimal("0.2")},
        ],
        shipped=[
            {"transfer_id": 901, "sap_to_warehouse": "BH-BT", "source_type": "STOCK_TRANSFER", "item_code": "FG0000101",
             "pieces": Decimal("19200"), "boxes": 1600, "rejected_pieces": Decimal("0"), "rejected_boxes": 0},
            {"transfer_id": 901, "sap_to_warehouse": "BH-BT", "source_type": "STOCK_TRANSFER", "item_code": "FG0000102",
             "pieces": Decimal("3200"), "boxes": 800, "rejected_pieces": Decimal("0"), "rejected_boxes": 0},
            {"transfer_id": 902, "sap_to_warehouse": "", "source_type": "INVOICE", "item_code": "FG0000101",
             "pieces": Decimal("12000"), "boxes": 1000, "rejected_pieces": Decimal("240"), "rejected_boxes": 20},
            {"transfer_id": 902, "sap_to_warehouse": "", "source_type": "INVOICE", "item_code": "FG0000105",
             "pieces": Decimal("960"), "boxes": 40, "rejected_pieces": Decimal("0"), "rejected_boxes": 0},
        ],
        workforce=[
            {"department": "oil_production_company", "label": "", "band": "", "kind": "", "employees": 42, "salary_monthly": Decimal("1302000")},
            {"department": "oil_production_outside", "label": "", "band": "", "kind": "", "employees": 65, "salary_monthly": Decimal("1170000")},
            {"department": "bottle_blowing_company", "label": "", "band": "", "kind": "", "employees": 8, "salary_monthly": Decimal("240000")},
            {"department": "bottle_blowing_outside", "label": "", "band": "", "kind": "", "employees": 14, "salary_monthly": Decimal("252000")},
            {"department": "pm_warehouse", "label": "", "band": "", "kind": "", "employees": 6, "salary_monthly": Decimal("168000")},
        ],
    )


def fake_hana(**overrides):
    answers = {**sap_answers(), **overrides}
    hana = FakeHana()
    for name, rows in answers.items():
        hana.add(marker(name), rows)
    return hana


def fake_postgres(**overrides):
    answers = {**factory_answers(), **overrides}
    postgres = FakePostgres()
    for name, rows in answers.items():
        postgres.add(marker(name), rows)
    return postgres


class RaisingPostgres(FakePostgres):
    """A factory database that refuses one statement with a query error."""

    def __init__(self, failing, answers=None):
        super().__init__(answers)
        self.failing = marker(failing)

    def rows(self, sql, params=None):
        if self.failing in sql:
            self.calls.append((sql, params))
            raise FactoryQueryError("column does not exist")
        return super().rows(sql, params)


class RaisingHana(FakeHana):
    """SAP that rejects one statement (a data error, which does not latch)."""

    def __init__(self, failing, answers=None):
        super().__init__(answers)
        self.failing = marker(failing)

    def rows(self, sql, params=None):
        if self.failing in sql:
            self.calls.append((sql, params))
            raise SAPDataError("SAP rejected this query: invalid column name")
        return super().rows(sql, params)


def service(postgres=None, hana=None, **kwargs):
    postgres = postgres if postgres is not None else fake_postgres()
    hana = hana if hana is not None else fake_hana()
    return PlantBoardService("JIVO_OIL", postgres=postgres, hana_for=hana_factory(hana), today=TODAY, **kwargs)


def build(postgres=None, hana=None):
    return service(postgres, hana).build()


def calls_for(fake, name):
    return [(sql, params) for sql, params in fake.calls if marker(name) in sql]


# ------------------------------------------------------------------ the plan


class PlanTests(SimpleTestCase):

    def test_the_plan_whose_dates_contain_today_is_the_window(self):
        board = build()

        self.assertEqual(
            board["plan"],
            {
                "abs_id": 24,
                "code": "OIL-SEP-26",
                "name": "OIL Monthly Production Planning for the September 2026",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "is_current": True,
                "days_total": 30,
                "days_elapsed": 16,
            },
        )
        self.assertEqual(board["meta"]["plan"], board["plan"])
        self.assertEqual(board["meta"]["warnings"][:1], ["1 packaging items in the stores carry no purchase price, so the stock value understates what is held."])

    def test_no_plan_covering_today_falls_back_to_the_newest_and_says_so(self):
        plans = sap_answers()["plans"]
        hana = fake_hana(plans=plans[1:])  # only August

        board = build(hana=hana)

        self.assertEqual(board["plan"]["abs_id"], 23)
        self.assertFalse(board["plan"]["is_current"])
        self.assertEqual(board["plan"]["days_elapsed"], 31)  # capped at the plan's end
        self.assertIn(
            "No SAP plan covers today; showing the most recent plan "
            "(OIL Monthly Production Planning for the August 2026).",
            board["meta"]["warnings"],
        )

    def test_no_plan_in_sap_is_an_answer_and_the_plan_tiles_say_why(self):
        board = build(hana=fake_hana(plans=[]))

        self.assertIsNone(board["plan"])
        self.assertNotIn("plan", board["meta"]["degraded"])
        for name in ("purchase_plan", "purchased", "open_pos", "monthly_planning"):
            self.assertIsNone(board[name], name)
            self.assertEqual(
                board["meta"]["reasons"][name], "LookupError: SAP holds no production plan for this company."
            )
        # Not measured against the plan, so still read.
        self.assertIsNotNone(board["benchmark"])
        self.assertIsNotNone(board["floor_stock"])
        # Without a plan the waste window is the calendar month.
        self.assertEqual(board["wastage"]["window_from"], "2026-09-01")

    def test_an_unread_plan_is_not_mistaken_for_an_absent_one(self):
        board = build(hana=_raising_hana("plans"))

        self.assertIsNone(board["plan"])
        self.assertIn("plan", board["meta"]["degraded"])
        self.assertIn("could not be read", board["meta"]["reasons"]["purchase_plan"])
        html = render(board)
        self.assertIn("<em>Plan</em> not read", html)

    def test_the_plan_list_is_newest_first_and_bounded(self):
        hana = fake_hana()
        build(hana=hana)
        sql, params = calls_for(hana, "plans")[0]
        self.assertIn('ORDER BY H."AbsID" DESC', sql)
        self.assertIn("LIMIT 24", sql)
        self.assertEqual(params, [])


def _raising_hana(failing, **overrides):
    hana = RaisingHana(failing)
    for name, rows in {**sap_answers(), **overrides}.items():
        hana.add(marker(name), rows)
    return hana


def _raising_postgres(failing, **overrides):
    postgres = RaisingPostgres(failing)
    for name, rows in {**factory_answers(), **overrides}.items():
        postgres.add(marker(name), rows)
    return postgres


# ------------------------------------------------------------ band 1: purchase


class PurchaseTests(SimpleTestCase):

    def test_plan_this_month_is_the_requirement_in_three_states(self):
        tile = build()["purchase_plan"]

        self.assertEqual(tile["planning_value"], 3813000.0)
        self.assertEqual(tile["issued_value"], 1532700.0)
        self.assertEqual(tile["on_hand_value"], 643000.0)
        # What is still to BUY: the plan less what the floor drew and the stores hold.
        self.assertEqual(tile["to_purchase_value"], 1637300.0)
        self.assertEqual(tile["unpriced_count"], 1)
        self.assertEqual(tile["planning_unit"], "PCS")
        self.assertEqual(tile["window_from"], "2026-09-01")
        self.assertEqual(tile["window_to"], "2026-09-16")

    def test_the_requirement_is_read_once_for_both_tiles_that_need_it(self):
        hana = fake_hana()
        build(hana=hana)
        for name in ("requirement", "pm_received", "pm_on_hand", "pm_open_po"):
            self.assertEqual(len(calls_for(hana, name)), 1, name)

    def test_the_requirement_reads_the_floor_and_the_feeding_stores(self):
        hana = fake_hana()
        build(hana=hana)

        self.assertEqual(calls_for(hana, "requirement")[0][1], [24])
        sql, params = calls_for(hana, "pm_received")[0]
        self.assertEqual(params, [d(1), d(16), "BH-PC"])
        sql, params = calls_for(hana, "pm_on_hand")[0]
        self.assertEqual(params, ["BH-BS", "BH-PM", "BH-NM"])
        sql, _ = calls_for(hana, "requirement")[0]
        self.assertIn('C."Quantity" / NULLIF(T."Qauntity", 0)', sql)
        self.assertIn("\"TreeType\" = 'P'", sql)

    def test_purchased_ties_ordered_to_received_plus_open(self):
        tile = build()["purchased"]

        self.assertEqual(tile["po_count"], 3)
        self.assertEqual(tile["po_lines"], 5)
        self.assertEqual(tile["ordered_value"], 1276000.0)
        self.assertEqual(tile["po_open_value"], 816000.0)
        self.assertEqual(tile["po_received_value"], 460000.0)
        self.assertEqual(tile["po_closed_lines"], 1)
        self.assertEqual(tile["ordered_value"], tile["po_open_value"] + tile["po_received_value"])

    def test_purchased_is_scoped_to_the_plans_own_items_and_window(self):
        hana = fake_hana()
        build(hana=hana)
        sql, params = calls_for(hana, "purchase_orders")[0]

        self.assertEqual(params[:2], [d(1), d(16)])
        self.assertEqual(params[2:], ["PM0000003", "PM0000014", "PM0000057", "PM0000059", "PM0000999"])
        self.assertIn('L."ItemCode" IN (?, ?, ?, ?, ?)', sql)

    def test_a_long_item_list_is_chunked(self):
        lines = [
            {"ItemCode": f"PM{n:07d}", "PlanningQty": 10, "SkuCount": 1, "UnitPrice": 1} for n in range(450)
        ]
        hana = fake_hana(requirement=lines)
        build(hana=hana)
        chunks = calls_for(hana, "purchase_orders")
        self.assertEqual([len(params) - 2 for _, params in chunks], [400, 50])

    def test_the_benchmark_counts_the_three_statuses_with_the_pages_filters(self):
        hana = fake_hana()
        tile = build(hana=hana)["benchmark"]

        self.assertEqual(tile["sku_count"], 142)
        self.assertEqual(tile["below_benchmark_count"], 15)
        self.assertEqual((tile["low_count"], tile["critical_count"], tile["healthy_count"]), (9, 6, 127))
        sql, params = calls_for(hana, "benchmark")[0]
        self.assertEqual(params, ["BH-PM", "BH-BS", "BH-NM", "BH-PC", "PACKAGING MATERIAL"])
        self.assertIn("g.days_since <= 30", sql)
        self.assertIn("g.min_stock > 0", sql)
        self.assertIn("IN (15, 60, 202)", sql)
        self.assertIn('GROUP BY w."ItemCode"', sql)

    def test_open_pos_split_by_age_adds_back_to_the_headline(self):
        tile = build()["open_pos"]

        self.assertEqual(tile["open_po_value"], 1130000.0)
        self.assertEqual(tile["open_po_count"], 3)
        self.assertEqual(tile["open_po_recent_value"], 816000.0)
        self.assertEqual(tile["open_po_older_value"], 314000.0)
        self.assertAlmostEqual(tile["open_po_recent_value"] + tile["open_po_older_value"], tile["open_po_value"])
        self.assertEqual(tile["open_po_recent_lines"], 3)
        self.assertEqual(tile["open_po_older_lines"], 2)

    def test_overdue_counts_only_the_short_rows_leaning_on_a_late_order(self):
        # Caps and PET are past due and short; the cartons are due after today.
        self.assertEqual(build()["open_pos"]["open_po_overdue_count"], 2)

        # The cartons' order falls overdue while they are short: counted.
        open_po = sap_answers()["pm_open_po"]
        open_po[1]["EarliestDue"] = datetime(2026, 9, 1)
        board = build(hana=fake_hana(pm_open_po=open_po))
        self.assertEqual(board["open_pos"]["open_po_overdue_count"], 3)

        # The same late order on cartons the stores more than cover is no
        # shortage's problem: not counted.
        on_hand = [row for row in sap_answers()["pm_on_hand"] if row["ItemCode"] != "PM0000003"]
        on_hand.append({"ItemCode": "PM0000003", "OnHand": Decimal("500000")})
        board = build(hana=fake_hana(pm_open_po=open_po, pm_on_hand=on_hand))
        self.assertEqual(board["open_pos"]["open_po_overdue_count"], 2)

    def test_an_item_sap_reports_no_open_line_for_stays_in_the_older_half(self):
        ages = [row for row in sap_answers()["open_po_age"] if row["ItemCode"] != "PM0000003"]
        tile = build(hana=fake_hana(open_po_age=ages))["open_pos"]

        self.assertEqual(tile["open_po_older_value"], 314000.0 + 555000.0)
        self.assertEqual(tile["open_po_recent_value"] + tile["open_po_older_value"], 1130000.0)


# --------------------------------------------------------------- band 2: store


class StoreTests(SimpleTestCase):

    def test_stock_space_reports_the_oldest_count_and_no_partial_capacity(self):
        tile = build()["stock_space"]

        self.assertEqual(tile["last_audit_date"], "2026-07-01")
        self.assertEqual(tile["last_audit_warehouse"], "BH-PM")
        self.assertEqual(tile["audit_days_ago"], 77)
        self.assertEqual(tile["audit_configured"], 3)
        # BH-NM has no row and BH-PC no capacity: a partial total would make
        # the stores look fuller than they are.
        self.assertIsNone(tile["capacity_tonnes"])
        self.assertEqual(tile["capacity_configured"], 2)
        self.assertEqual([s["warehouse"] for s in tile["stores"]], ["BH-PM", "BH-BS", "BH-NM", "BH-PC"])

    def test_the_floor_is_pallets_per_item_times_the_footprint(self):
        area = build()["stock_space"]["area"]

        self.assertEqual(area["sqft"], 38900)
        self.assertEqual([b["sqft"] for b in area["blocks"]], [16900, 15000, 7000])
        self.assertEqual(area["pallets"], 1520.6)
        self.assertEqual(area["occupied_sqft"], 22809.0)
        self.assertEqual(area["free_sqft"], 16091.0)
        self.assertEqual(area["occupied_pct"], 58.6)
        self.assertEqual(area["held_pieces"], 2125000.0)
        self.assertEqual(area["held_value"], 7492200.0)
        self.assertEqual(area["unmeasured_items"], 1)
        self.assertEqual(area["unmeasured_pieces"], 5000.0)
        self.assertEqual(area["stacking_measured_on"], "2026-09-11")
        by_store = {row["warehouse"]: row for row in area["by_store"]}
        self.assertEqual(by_store["BH-PM"]["pallets"], 1220.0)
        self.assertEqual(sum(row["pieces"] for row in area["by_store"]), area["held_pieces"])

    def test_unpriced_and_unmeasured_stock_is_named_in_the_warnings(self):
        warnings = build()["meta"]["warnings"]
        self.assertIn("1 packaging items have no pallet figure, so the floor in use is understated.", warnings)

    def test_the_measured_default_stands_until_somebody_sets_one(self):
        area = build(postgres=fake_postgres(plant_settings=[]))["stock_space"]["area"]
        self.assertEqual(area["sqft_per_pallet"], 15.0)
        self.assertEqual(area["occupied_pct"], 58.6)

    def test_a_cleared_footprint_withholds_the_percentage(self):
        area = build(postgres=fake_postgres(plant_settings=[{"sqft_per_pallet": None}]))["stock_space"]["area"]
        self.assertIsNone(area["sqft_per_pallet"])
        self.assertIsNone(area["occupied_pct"])
        self.assertIsNone(area["occupied_sqft"])
        self.assertIsNone(area["by_store"][0]["occupied_sqft"])

    def test_a_settings_table_not_yet_migrated_falls_back_to_the_default(self):
        board = build(postgres=_raising_postgres("plant_settings"))
        self.assertEqual(board["stock_space"]["area"]["sqft_per_pallet"], 15.0)
        self.assertNotIn("stock_space", board["meta"]["degraded"])

    def test_the_stacking_sheet_is_a_positive_factor_per_item(self):
        self.assertEqual(len(PIECES_PER_PALLET), 398)
        self.assertEqual(PIECES_PER_PALLET["PM0000003"], 250.0)
        self.assertEqual(PIECES_PER_PALLET["PM0000059"], 60000.0)
        self.assertTrue(all(value > 0 for value in pieces_per_pallet().values()))

    def test_non_moving_keeps_the_freshest_movement_and_drops_the_recent(self):
        tile = build()["non_moving"]

        self.assertEqual(tile["item_count"], 3)
        self.assertEqual(tile["total_value"], 655000.0)
        self.assertEqual(tile["non_moving_count"], 2)
        self.assertEqual(tile["slow_moving_count"], 1)
        self.assertEqual(tile["oldest_days"], 210)
        self.assertEqual(tile["recent_count"], 2)

        # The same SKU moved last week in another store: no longer idle.
        rows = sap_answers()["non_moving"] + [
            {"ItemCode": "PM0000201", "WhsCode": "BH-PC", "Quantity": 10, "Value": Decimal("190"), "Days": 5}
        ]
        tile = build(hana=fake_hana(non_moving=rows))["non_moving"]
        self.assertEqual(tile["item_count"], 2)
        self.assertEqual(tile["recent_count"], 3)

    def test_non_moving_ages_packaging_on_production_never_on_a_transfer(self):
        hana = fake_hana()
        build(hana=hana)
        sql, params = calls_for(hana, "non_moving")[0]
        self.assertIn('N."TransType" IN (60, 59)', sql)
        self.assertIn('COALESCE(N."TransType", 0) <> 67', sql)
        self.assertEqual(params, [105, "BH-PM", "BH-BS", "BH-NM", "BH-PC"])

    def test_the_thresholds_are_the_dashboards(self):
        self.assertEqual(movement_status(29), "recent")
        self.assertEqual(movement_status(30), "slow-moving")
        self.assertEqual(movement_status(45), "slow-moving")
        self.assertEqual(movement_status(46), "non-moving")

    def test_packing_material_in_counts_trucks_and_what_awaits_a_grpo(self):
        postgres = fake_postgres()
        tile = build(postgres=postgres)["pm_in"]

        self.assertEqual(tile["count"], 4)
        self.assertEqual(tile["po_count"], 6)
        self.assertEqual(tile["line_count"], 11)
        self.assertEqual(tile["awaiting_grpo_qty"], 85000.0)
        sql, params = calls_for(postgres, "pm_in")[0]
        self.assertEqual(params, [COMPANY_ID, "Asia/Kolkata", TODAY, ["BH-PM", "BH-BS", "BH-NM", "BH-PC"]])
        self.assertIn("COUNT(DISTINCT pr.vehicle_entry_id)", sql)
        self.assertIn("(ve.entry_time AT TIME ZONE %s)::date = %s", sql)

    def test_blowing_is_the_calendar_month_with_the_week_as_bars(self):
        postgres = fake_postgres()
        tile = build(postgres=postgres)["blowing"]

        self.assertEqual(tile["bottles_made"], 182000)
        self.assertEqual(tile["cost"], 402500.0)
        self.assertEqual(tile["runs"], 16)
        self.assertEqual(tile["uncosted_runs"], 2)
        self.assertEqual(tile["active_days"], 9)
        self.assertEqual(tile["avg_bottles_per_day"], 20222.2)
        self.assertEqual(tile["cost_per_bottle"], round(402500 / 161140, 4))
        self.assertEqual(tile["conversion_cost"], 85400.0)
        self.assertEqual(
            [(row["date"][8:], row["bottles"]) for row in tile["daily"]],
            [("10", 24000), ("11", 23000), ("12", 25500), ("13", 0), ("14", 20000), ("15", 26000), ("16", 9000)],
        )
        self.assertEqual(calls_for(postgres, "blowing_days")[0][1], [COMPANY_ID, d(1), TODAY])
        self.assertIn("2 of 16 blowing runs this month have no cost row yet.", build()["meta"]["warnings"])

    def test_running_means_an_open_segment_not_a_status(self):
        postgres = fake_postgres()
        tile = build(postgres=postgres)["blowing"]
        self.assertEqual(tile["running_runs"], 2)
        self.assertEqual(tile["running_cost"], 47150.0)
        sql, _ = calls_for(postgres, "blowing_running")[0]
        self.assertIn("s.end_time IS NULL AND s.is_active", sql)
        self.assertNotIn("status", sql)


# ---------------------------------------------------------- band 3: production


class ProductionTests(SimpleTestCase):

    def test_today_counts_drafts_as_planned_and_open_runs_from_their_segments(self):
        tile = build()["today_lines"]

        self.assertEqual(tile["planned_cases"], 4800.0)
        # The completed run's own total, then segments for the open ones.
        self.assertEqual(tile["produced_cases"], 1200.0 + 410.0 + 950.0)
        self.assertEqual(tile["runs"], 5)
        self.assertEqual(tile["lines"], 3)
        self.assertEqual(tile["completed_runs"], 1)
        # Pieces per run on its own factor; the run with none is named.
        self.assertEqual(tile["planned_qty"], 1200 * 12 + 800 * 20 + 1500 * 4 + 600 * 12)
        self.assertEqual(tile["produced_qty"], 1200 * 12 + 410 * 20 + 950 * 4)
        self.assertEqual(tile["unconverted_runs"], 1)

    def test_a_day_with_no_runs_has_no_attainment_rather_than_zero(self):
        tile = build(postgres=fake_postgres(today_runs=[]))["today_lines"]
        self.assertEqual(tile["runs"], 0)
        self.assertIsNone(tile["attainment_pct"])

    def test_discarded_runs_are_not_a_plan(self):
        postgres = fake_postgres()
        build(postgres=postgres)
        sql, params = calls_for(postgres, "today_runs")[0]
        self.assertIn("NOT pr.is_deleted", sql)
        self.assertEqual(params, [COMPANY_ID, TODAY])

    def test_output_is_the_whole_floor_not_the_plans_share_of_it(self):
        tile = build()["monthly_planning"]

        self.assertEqual(tile["planned_tons"], 1650.0)
        self.assertEqual(tile["produced_tons"], 905.0)
        self.assertEqual(tile["produced_qty"], 1280000.0)
        self.assertEqual(tile["produced_planned_tons"], 811.5)
        self.assertEqual(tile["produced_unplanned_tons"], 93.5)
        self.assertEqual(tile["attainment_tons_pct"], 54.8)
        self.assertEqual(tile["attainment_pct"], round(1280000 / 1346000 * 100, 1))
        self.assertEqual(tile["produced_planned_qty"], 625500.0)
        self.assertEqual(tile["active_days"], 14)
        self.assertEqual(tile["avg_qty_per_active_day"], round(1280000 / 14, 1))
        self.assertEqual(tile["elapsed_days"], 16)
        self.assertEqual(len(tile["daily"]), 12)
        self.assertEqual(tile["daily"][-1], {"date": "2026-09-16", "qty": 84000.0})

    def test_a_sku_with_no_litre_volume_is_counted_out_loud(self):
        tile = build()["monthly_planning"]
        self.assertEqual(tile["unweighed_lines"], 1)
        self.assertEqual(tile["planned_cases"], round(75000 + 30000 + 6000 + 6250 + 833.33, 2))

    def test_the_plan_actuals_are_read_over_the_whole_plan_period(self):
        hana = fake_hana()
        build(hana=hana)
        _, params = calls_for(hana, "plan_produced")[0]
        self.assertEqual(params[:2], [d(1), d(30)])
        _, params = calls_for(hana, "floor_production")[0]
        self.assertEqual(params, ["BH-PF", d(1), d(16)])

    def test_stock_is_aged_from_when_it_last_left(self):
        tile = build()["floor_stock"]
        age = tile["age"]

        self.assertEqual(tile["tons"], 304.5)
        self.assertEqual(age["fresh"]["tons"], 120.0)
        self.assertEqual(age["fresh"]["items"], 2)  # FG0000101 and the unweighed FG0000105
        self.assertEqual(age["d4_7"]["tons"], 150.0)
        self.assertEqual(age["d7_plus"]["tons"], 22.5)
        self.assertEqual(age["never_shipped"]["tons"], 12.0)
        self.assertEqual(age["never_shipped"]["items"], 1)
        self.assertEqual(tile["unweighed_items"], 1)
        # The negative row is counted in the warehouse's own figures, never in a bucket.
        self.assertEqual(tile["item_count"], 6)
        self.assertEqual(tile["stock_value"], 48722000.0)
        self.assertEqual(sum(bucket["items"] for bucket in age.values()), 5)

    def test_waste_is_dated_by_the_run_and_valued_at_the_runs_own_price(self):
        tile = build()["wastage"]
        latest = tile["logged_latest"]

        self.assertEqual(tile["logged_latest_date"], "2026-09-14")
        self.assertEqual(tile["logged_days_behind"], 2)
        self.assertEqual(latest["logs"], 6)
        self.assertEqual(latest["pm_value"], round(320 * 4.2 + 900 * 0.31 + 1500 * 0.62 + 40 * 18.5 + 12 * 165, 2))
        self.assertEqual(latest["rm_value"], 85 * 118.0)
        # Units are kept apart: the kilos of shrink are not pieces.
        self.assertEqual(latest["pm_pieces"], 2760.0)
        self.assertEqual(latest["pm_other"], [{"uom": "KG", "qty": 12.0}])
        self.assertEqual(latest["rm_litres"], 85.0)
        self.assertEqual(tile["logged_today"]["logs"], 0)
        self.assertEqual(tile["logged_standalone_count"], 1)

    def test_the_week_is_every_day_present_even_with_no_logs(self):
        daily = build()["wastage"]["logged_daily"]
        self.assertEqual([row["date"][8:] for row in daily], ["10", "11", "12", "13", "14", "15", "16"])
        by_day = {row["date"][8:]: row for row in daily}
        self.assertEqual(by_day["13"]["total_value"], 0.0)
        # The standalone row keeps the day it was typed: 600 labels and 25 cartons.
        self.assertEqual(by_day["11"]["total_value"], round(600 * 0.30 + 25 * 18.5, 2))

    def test_a_run_with_no_price_falls_back_and_a_row_with_none_is_counted(self):
        prices = [row for row in factory_answers()["waste_prices"] if row["production_run_id"] != 71]
        tile = build(postgres=fake_postgres(waste_prices=prices))["wastage"]
        latest = tile["logged_latest"]
        # Run 71's labels fall back to the newest known price (0.30); its
        # bottles to run 60's 4.1.
        self.assertEqual(latest["pm_value"], round(320 * 4.1 + 900 * 0.30 + 1500 * 0.62 + 40 * 18.5 + 12 * 165, 2))
        self.assertEqual(tile["logged_unpriced_count"], 0)

        prices = [row for row in prices if row["material_code"] != "PM0000071"]
        tile = build(postgres=fake_postgres(waste_prices=prices))["wastage"]
        self.assertEqual(tile["logged_unpriced_count"], 1)
        self.assertEqual(tile["logged_latest"]["unpriced"], 1)

    def test_the_waste_window_and_the_typed_date_use_the_factorys_zone(self):
        postgres = fake_postgres()
        build(postgres=postgres)
        sql, params = calls_for(postgres, "waste_logs")[0]
        self.assertEqual(params, ["Asia/Kolkata", COMPANY_ID, d(1), d(16), "Asia/Kolkata", d(1), "Asia/Kolkata", d(16)])
        self.assertIn("pr.date >= %s AND pr.date <= %s", sql)

    def test_an_empty_register_reports_no_day_rather_than_a_zero(self):
        tile = build(postgres=fake_postgres(waste_logs=[]))["wastage"]
        self.assertIsNone(tile["logged_latest"])
        self.assertIsNone(tile["logged_days_behind"])

    def test_material_is_classified_on_the_sap_item_group(self):
        self.assertEqual(classify_material("PACKAGING MATERIAL"), "PACKAGING")
        self.assertEqual(classify_material("RAW MATERIAL"), "RAW")
        self.assertEqual(classify_material("OIL BULK"), "RAW")
        self.assertEqual(classify_material("FINISHED"), "OTHER")
        self.assertEqual(classify_material(None), "OTHER")


# ------------------------------------------------------------ band 4: shifting


class ShiftingTests(SimpleTestCase):

    def test_declared_reads_the_keepers_register_down_the_bottling_row(self):
        tile = build()["declared"]

        self.assertEqual(tile["transfers"], 3)
        self.assertEqual(tile["retracted_movements"], 1)
        self.assertEqual(tile["retracted_pieces"], 6000.0)
        self.assertTrue(tile["tonnage_available"])
        self.assertEqual(tile["total_tons"], 50.9)
        self.assertEqual(tile["total_pieces"], 40300.0)
        self.assertEqual(tile["boxes"], 2000 + 1000 + 300 + 250)
        routes = {row["route"]: row for row in tile["routes"]}
        self.assertEqual(list(routes), ["BH-BT", "ELSEWHERE"])
        self.assertEqual(routes["BH-BT"]["tons"], 44.0)
        # A declared dispatch and the retired Gupta code fold into Elsewhere,
        # which names what it folded.
        self.assertEqual(routes["ELSEWHERE"]["codes"], ["DISPATCH", "GP-FG"])
        self.assertEqual(routes["ELSEWHERE"]["tons"], 6.9)

    def test_declared_never_asks_sap(self):
        board = service(hana=HanaDown()).build()
        self.assertEqual(board["declared"]["total_tons"], 50.9)

    def test_nothing_declared_still_draws_the_bottling_row(self):
        tile = build(postgres=fake_postgres(declared=[]))["declared"]
        self.assertEqual(tile["transfers"], 0)
        self.assertEqual([row["route"] for row in tile["routes"]], ["BH-BT"])
        self.assertEqual(tile["routes"][0]["pieces"], 0.0)

    def test_shipped_reads_the_bst_register_by_route(self):
        postgres = fake_postgres()
        tile = build(postgres=postgres)["shipped"]

        self.assertEqual(tile["transfers"], 2)
        self.assertEqual(tile["boxes"], 3440)
        self.assertEqual(tile["total_pieces"], 35360.0)
        self.assertEqual(tile["total_tons"], 47.2)
        routes = {row["route"]: row for row in tile["routes"]}
        self.assertEqual(list(routes), ["BH-BT", "DISPATCH"])
        self.assertEqual(routes["BH-BT"]["tons"], 35.2)
        # An invoice transfer is a sale: Dispatch, whatever warehouse it names.
        self.assertEqual(routes["DISPATCH"]["tons"], 12.0)
        self.assertEqual(tile["unweighed_items"], 1)
        self.assertEqual(tile["rejected_pieces"], 240.0)
        self.assertEqual(tile["rejected_tons"], 0.24)
        sql, params = calls_for(postgres, "shipped")[0]
        self.assertEqual(params, [COMPANY_ID, "BH-PF", "Asia/Kolkata", TODAY])
        self.assertIn("t.status <> 'CANCELLED'", sql)
        self.assertIn("(t.dispatched_at AT TIME ZONE %s)::date = %s", sql)

    def test_the_two_halves_are_never_netted(self):
        board = build()
        self.assertNotEqual(board["declared"]["total_tons"], board["shipped"]["total_tons"])
        self.assertNotIn("variance", json.dumps(board["shipped"]))

    def test_losing_sap_costs_the_unit_and_not_the_tile(self):
        board = service(hana=HanaDown()).build()
        tile = board["shipped"]

        self.assertNotIn("shipped", board["meta"]["degraded"])
        self.assertFalse(tile["tonnage_available"])
        self.assertIsNone(tile["total_tons"])
        self.assertIsNone(tile["rejected_tons"])
        self.assertEqual(tile["total_pieces"], 35360.0)
        self.assertTrue(all(row["tons"] is None for row in tile["routes"]))
        self.assertEqual(tile["unweighed_items"], 0)

    def test_litres_are_asked_once_per_build_through_the_litre_gate(self):
        hana = fake_hana()
        build(hana=hana)
        calls = calls_for(hana, "litres")
        self.assertEqual(len(calls), 1)
        self.assertIn("U_IsLitre", calls[0][0])
        self.assertEqual(calls[0][1], ["FG0000101", "FG0000102", "FG0000105"])


# ------------------------------------------------------------------ workforce


class WorkforceTests(SimpleTestCase):

    def test_the_company_and_outside_split_fills_each_strip(self):
        workforce = build()["workforce"]
        bands = workforce["bands"]

        self.assertEqual(bands["production"]["employees"], 42)
        self.assertEqual(bands["production"]["labour"], 65)
        # Two departments share the Store band's staff half.
        self.assertEqual(bands["store"]["employees"], 14)
        self.assertEqual(bands["store"]["labour"], 14)
        self.assertEqual(bands["store"]["employee_salary_monthly"], 408000.0)
        # Purchase has nobody by design; it is a rule, not a zero.
        self.assertIsNone(bands["purchase"]["employees"])
        self.assertIsNone(bands["purchase"]["labour"])

    def test_the_monthly_bill_is_divided_by_calendar_days(self):
        workforce = build()["workforce"]
        self.assertEqual(workforce["days_in_month"], 30)
        self.assertEqual(workforce["bands"]["production"]["employee_cost_per_day"], 43400.0)
        self.assertEqual(workforce["total_salary_monthly"], 3132000.0)
        self.assertEqual(workforce["total_cost_per_day"], 104400.0)
        self.assertEqual(workforce["total_people"], 135)

    def test_an_unconfigured_department_is_named(self):
        workforce = build()["workforce"]
        self.assertEqual(workforce["unconfigured"], ["Fg Shifting"])
        self.assertIsNone(workforce["bands"]["shifting"]["employees"])

    def test_an_added_department_joins_its_band_and_a_bandless_row_is_skipped(self):
        rows = factory_answers()["workforce"] + [
            {"department": "canteen", "label": "Canteen", "band": "production", "kind": "labour", "employees": 5, "salary_monthly": None},
            {"department": "retired_dept", "label": "Old", "band": "", "kind": "", "employees": 9, "salary_monthly": None},
        ]
        workforce = build(postgres=fake_postgres(workforce=rows))["workforce"]
        self.assertEqual(workforce["bands"]["production"]["labour"], 70)
        self.assertEqual(workforce["total_people"], 140)
        self.assertEqual(workforce["bands"]["production"]["departments"][-1]["label"], "Canteen")

    def test_nothing_configured_is_a_rule_not_an_empty_factory(self):
        workforce = build(postgres=fake_postgres(workforce=[]))["workforce"]
        self.assertIsNone(workforce["total_people"])
        self.assertIsNone(workforce["total_salary_monthly"])
        self.assertEqual(len(workforce["unconfigured"]), 6)

    def test_the_figures_are_this_companys(self):
        postgres = fake_postgres()
        build(postgres=postgres)
        self.assertEqual(calls_for(postgres, "workforce")[0][1], ["JIVO_OIL"])


# ------------------------------------------------------------ degradation


class DegradedTests(SimpleTestCase):

    def test_sap_down_costs_the_sap_tiles_and_nothing_else(self):
        board = service(hana=HanaDown()).build()

        for name in SAP_TILES:
            self.assertIsNone(board[name], name)
            self.assertEqual(board["meta"]["reasons"][name], "SAP HANA could not be reached.")
        for name in ("pm_in", "blowing", "today_lines", "declared", "shipped", "workforce"):
            self.assertIsNotNone(board[name], name)
        self.assertIn("plan", board["meta"]["degraded"])
        self.assertTrue(any("SAP did not answer" in warning for warning in board["meta"]["warnings"]))

    def test_one_sap_outage_costs_one_timeout_not_one_per_tile(self):
        class CountingDown(HanaDown):
            attempts = 0

            def table(self, name):
                CountingDown.attempts += 1
                return super().table(name)

        service(hana=CountingDown()).build()
        self.assertEqual(CountingDown.attempts, 1)

    def test_the_factory_database_down_costs_its_tiles_and_nothing_else(self):
        stand_in = Unavailable(FactoryUnavailable("The factory database could not be reached: refused"))
        board = build(postgres=stand_in)

        for name in FACTORY_TILES:
            self.assertIsNone(board[name], name)
            self.assertIn("could not be reached", board["meta"]["reasons"][name])
        for name in ("purchase_plan", "purchased", "benchmark", "open_pos", "non_moving", "monthly_planning", "floor_stock"):
            self.assertIsNotNone(board[name], name)
        self.assertTrue(any("factory database" in warning for warning in board["meta"]["warnings"]))

    def test_one_failing_statement_costs_one_tile(self):
        board = build(postgres=_raising_postgres("blowing_days"))

        self.assertIsNone(board["blowing"])
        self.assertEqual(board["meta"]["degraded"], ["blowing"])
        self.assertEqual(board["meta"]["reasons"]["blowing"], "FactoryQueryError: column does not exist")
        self.assertIsNotNone(board["pm_in"])

    def test_a_rejected_sap_statement_costs_its_tiles_without_latching(self):
        board = build(hana=_raising_hana("pm_open_po"))

        # Both tiles that read the requirement report the same reason, and
        # the requirement was asked for once.
        self.assertIsNone(board["purchase_plan"])
        self.assertIsNone(board["open_pos"])
        self.assertIn("SAP rejected this query", board["meta"]["reasons"]["open_pos"])
        self.assertIsNotNone(board["purchased"])
        self.assertIsNotNone(board["benchmark"])
        self.assertIsNotNone(board["floor_stock"])
        self.assertEqual(sorted(board["meta"]["degraded"]), ["open_pos", "purchase_plan"])

    def test_an_unknown_company_is_a_reason_on_the_factory_tiles(self):
        board = build(postgres=fake_postgres(company=[]))
        for name in ("pm_in", "blowing", "today_lines", "wastage", "declared", "shipped"):
            self.assertIsNone(board[name], name)
            self.assertIn("No company with code 'JIVO_OIL'", board["meta"]["reasons"][name])
        # Typed by company code, not by the company row.
        self.assertIsNotNone(board["workforce"])
        self.assertIsNotNone(board["stock_space"])

    def test_a_section_never_raises_out_of_build(self):
        board = build(hana=fake_hana(floor_stock=[{"ItemCode": "X", "OnHand": "lots"}]))
        self.assertIsNotNone(board["floor_stock"])  # bad numbers read as zero
        board = build(postgres=fake_postgres(today_runs=[{"status": None}]))
        self.assertEqual(board["today_lines"]["runs"], 1)


# ------------------------------------------------------------ meta and shape


class PayloadTests(SimpleTestCase):

    def test_the_payload_is_plain_json_with_one_key_per_tile(self):
        board = build()

        json.dumps(board)
        self.assertEqual(list(board), ["plan", *TILES, "workforce", "meta"])
        meta = board["meta"]
        self.assertEqual(meta["refresh_seconds"], 60)
        self.assertEqual(meta["as_of"], "2026-09-16")
        self.assertEqual(meta["company_code"], "JIVO_OIL")
        self.assertEqual(meta["degraded"], [])
        self.assertEqual(meta["withheld"], [])
        self.assertIn("1000 litres = 1 ton", meta["tonnage_basis"])

    def test_every_statement_is_named_and_uses_its_sources_placeholders(self):
        postgres, hana = fake_postgres(), fake_hana()
        build(postgres, hana)

        for sql, _ in postgres.calls:
            with self.subTest(sql=sql.splitlines()[0]):
                self.assertRegex(sql, r"^-- plant: [a-z_]+\n")
                self.assertNotIn("?", sql)
        for sql, _ in hana.calls:
            with self.subTest(sql=sql.splitlines()[0]):
                self.assertRegex(sql, r"^-- plant: [a-z_]+\n")
                self.assertNotIn("%s", sql)

    def test_every_sap_table_is_qualified_with_the_schema(self):
        hana = fake_hana()
        build(hana=hana)
        bare = re.compile(r'(?:FROM|JOIN)\s+"?(O[A-Z]{3}|[A-Z]{3}1)"?\s')
        for sql, _ in hana.calls:
            with self.subTest(sql=sql.splitlines()[0]):
                self.assertIsNone(bare.search(sql), sql)
                self.assertIn('"TEST_SCHEMA".', sql)

    def test_the_factory_reads_are_scoped_to_the_company(self):
        postgres = fake_postgres()
        build(postgres=postgres)
        self.assertEqual(calls_for(postgres, "company")[0][1], ["JIVO_OIL"])
        for sql, params in postgres.calls:
            with self.subTest(sql=sql.splitlines()[0]):
                self.assertTrue(COMPANY_ID in params or "JIVO_OIL" in params, sql)

    def test_the_registry_points_at_this_service(self):
        slide = registry.get_slide("plant")
        self.assertIs(slide.service_class(), PlantBoardService)
        for path in (*slide.css, *slide.js):
            self.assertIsNotNone(finders.find(path), path)

    @override_settings(FACTORY_DB=FACTORY_OFF, FACTORY_HANA=HANA_OFF, FACTORY_COMPANY_CODE="JIVO_OIL")
    def test_compose_builds_it_like_any_other_board(self):
        postgres = fake_postgres()
        with mock.patch("boards.compose.connect") as connect_mock, mock.patch(
            "boards.compose.hana_for", hana_factory(fake_hana())
        ):
            connect_mock.return_value.__enter__.return_value = postgres
            payload = build_board(registry.get_slide("plant"))

        self.assertEqual(payload["meta"]["slide"], "plant")
        self.assertEqual(payload["meta"]["company"], "JIVO_OIL")
        self.assertEqual(payload["monthly_planning"]["produced_tons"], 905.0)

    @override_settings(FACTORY_DB=FACTORY_OFF, FACTORY_HANA=HANA_OFF, FACTORY_COMPANY_CODE="JIVO_OIL")
    def test_with_both_sources_off_every_tile_says_why(self):
        payload = build_board(registry.get_slide("plant"))

        for name in (*TILES, "workforce"):
            self.assertIsNone(payload[name], name)
        self.assertIn("not configured", payload["meta"]["reasons"]["benchmark"])
        self.assertIn("not configured", payload["meta"]["reasons"]["blowing"])
        html = render(payload)
        self.assertEqual(html.count('class="ops-unread"'), len(TILES))


# ------------------------------------------------------------ the page


def render(payload):
    return render_to_string(
        "boards/fragments/plant.html",
        {"board": payload, "meta": payload["meta"], "slide": registry.get_slide("plant")},
    )


class ViewTests(SimpleTestCase):
    """The page's arithmetic, ported from PlantBoardDashboardPage.tsx."""

    def tiles(self, board=None):
        page = present(board or build())
        return {tile["key"]: tile for band in page["bands"] for tile in band["tiles"]}, page

    def test_the_header_names_the_plan_and_carries_the_workforce(self):
        _, page = self.tiles()
        top = page["topbar"]

        self.assertEqual(top["scope"], "JIVO_OIL · OIL Monthly Production Planning for the September 2026")
        chips = {chip["label"]: chip["value"] for chip in top["chips"]}
        self.assertEqual(chips["Plan"], "day 16 of 30")
        self.assertEqual(chips["Tonnage"], "1000 L = 1 t · oil and FG only")
        self.assertEqual(chips["Feed"], "all tiles current")
        self.assertTrue(chips["Check"].startswith("1 packaging items in the stores carry no purchase price"))
        totals = {total["caption"]: total for total in top["totals"]}
        self.assertEqual(totals["Total wage bill"]["value"], "₹31.3 L")
        self.assertEqual(totals["Total wage bill"]["sub"], "₹1.0 L a day")
        self.assertEqual(totals["People on roll"]["value"], "135")
        self.assertEqual(totals["People on roll"]["sub"], "1 department not counted")
        self.assertEqual(totals["Employees against labour"]["value"], "56 · 79")

    def test_the_bands_are_in_the_order_material_moves(self):
        _, page = self.tiles()
        self.assertEqual([band["domain"] for band in page["bands"]], ["purchase", "store", "production", "shifting"])
        self.assertEqual([len(band["tiles"]) for band in page["bands"]], [4, 4, 4, 2])

    def test_the_purchase_tiles(self):
        tiles, _ = self.tiles()

        plan_tile = tiles["purchase_plan"]
        self.assertEqual(plan_tile["value"], "₹38.1 L")
        self.assertEqual(plan_tile["tag"], {"label": "43% still to buy", "tone": "neut"})
        self.assertEqual(plan_tile["sub"], "1 components carry no price")
        self.assertEqual(
            [(s["label"], s["figure"]) for s in plan_tile["viz"]["keys"]],
            [("Already drawn", "₹15.3 L"), ("PM on hand", "₹6.4 L"), ("PM to purchase", "₹16.4 L")],
        )
        self.assertEqual(tiles["purchased"]["tag"]["label"], "3 POs raised")
        self.assertEqual(tiles["benchmark"]["tag"], {"label": "11% of the range", "tone": "bad"})
        self.assertEqual(tiles["benchmark"]["value"], "15")
        self.assertEqual(tiles["open_pos"]["tag"], {"label": "2 overdue", "tone": "bad"})

    def test_the_store_tiles(self):
        tiles, _ = self.tiles()

        self.assertEqual(tiles["stock_space"]["value"], "58.6")
        self.assertEqual(tiles["stock_space"]["unit"], "% of the floor in use")
        self.assertEqual(tiles["stock_space"]["tag"], {"label": "counted 77d ago", "tone": "warn"})
        self.assertEqual(tiles["non_moving"]["value"], "₹6.5 L")
        self.assertEqual(tiles["non_moving"]["sub"], "Oldest 210 days · 2 SKUs still moving")
        self.assertEqual(
            [s["label"] for s in tiles["pm_in"]["viz"]["keys"]], ["Booked to SAP", "Rejected", "Awaiting GRPO"]
        )
        blowing = tiles["blowing"]
        self.assertEqual(blowing["value"], "1.82 L")
        self.assertEqual(blowing["tag"], {"label": "2 running · ₹47.1k so far", "tone": "ok"})
        days = blowing["viz"]["days"]
        self.assertEqual([day["label"] for day in days], ["10", "11", "12", "13", "14", "15", "Today"])
        self.assertEqual(days[3]["height"], "0.00")
        self.assertEqual(days[3]["value"], "")
        self.assertEqual(days[5]["height"], "100.00")

    def test_without_a_footprint_the_floor_is_shown_as_measured(self):
        board = build(postgres=fake_postgres(plant_settings=[{"sqft_per_pallet": None}]))
        tiles, _ = self.tiles(board)
        tile = tiles["stock_space"]
        self.assertEqual(tile["value"], "38,900")
        self.assertEqual(tile["unit"], "sq ft of floor")
        self.assertEqual(
            [s["label"] for s in tile["viz"]["keys"]], ["BH-PM, BH-NM, BH-BS", "BH-PM", "BH-PC", "In use"]
        )

    def test_the_production_tiles(self):
        tiles, _ = self.tiles()

        self.assertEqual(tiles["today_lines"]["value"], "2,560")
        self.assertEqual(tiles["today_lines"]["tag"]["label"], "5 runs · 3 lines")
        planning = tiles["monthly_planning"]
        self.assertEqual(planning["value"], "1,650")
        self.assertEqual(planning["tag"], {"label": "54.8% produced", "tone": "bad"})
        self.assertEqual(planning["sub"], "905 t produced · 12.80 L pcs")
        self.assertEqual(
            [(s["label"], s["figure"]) for s in planning["viz"]["keys"]],
            [("Left to make", "745 t"), ("Unplanned", "94 t"), ("No litre volume", "1 SKUs")],
        )
        stock = tiles["floor_stock"]
        self.assertEqual(stock["value"], "304.5")
        self.assertEqual(stock["tag"]["label"], "₹4.87 Cr")
        self.assertEqual(stock["viz"]["keys"][2]["figure"], "34.5 t")  # over 7 days plus never shipped
        waste = tiles["wastage"]
        self.assertEqual(waste["tag"], {"label": "14 Sep · 2d behind", "tone": "neut"})
        self.assertEqual(waste["value"], "₹5.3k")
        self.assertEqual(waste["sub"], "Raw material ₹10.0k · packing 2,760 pcs, 12 KG")

    def test_the_shifting_tiles(self):
        tiles, _ = self.tiles()

        declared = tiles["declared"]
        self.assertEqual(declared["value"], "50.9")
        self.assertEqual(declared["unit"], "tonnes")
        self.assertEqual(declared["tag"], {"label": "1 retracted", "tone": "warn"})
        self.assertEqual(
            [s["label"] for s in declared["viz"]["keys"]], ["BH-BT basement", "Elsewhere · DISPATCH, GP-FG"]
        )
        shipped = tiles["shipped"]
        self.assertEqual(shipped["tag"], {"label": "0.2 t rejected", "tone": "bad"})
        self.assertEqual(
            [s["label"] for s in shipped["viz"]["keys"]], ["BH-BT basement", "Dispatch to Mart", "No litre volume"]
        )

    def test_sap_down_turns_the_shipped_unit_into_pieces(self):
        tiles, page = self.tiles(service(hana=HanaDown()).build())
        self.assertEqual(tiles["shipped"]["value"], "35,360")
        self.assertEqual(tiles["shipped"]["unit"], "pcs, SAP not answering")
        self.assertEqual(tiles["shipped"]["tag"]["label"], "240 pcs rejected")
        chips = {chip["label"]: chip["value"] for chip in page["topbar"]["chips"]}
        self.assertEqual(chips["Plan"], "not read")
        self.assertEqual(chips["Feed"], "10 tiles unread")

    def test_people_strips_are_rules_where_nobody_is_configured(self):
        _, page = self.tiles()
        strips = {band["domain"]: band["people"] for band in page["bands"]}

        self.assertIsNone(strips["purchase"]["on_shift"])
        self.assertIsNone(strips["purchase"]["split"])
        self.assertEqual(strips["production"]["on_shift"], 107)
        self.assertEqual(strips["production"]["roles"][0]["cost"], "₹43.4k a day")
        self.assertEqual(strips["store"]["split"], {"employees": "50.00", "labour": "50.00"})
        self.assertIsNone(strips["shifting"]["on_shift"])


class TemplateTests(SimpleTestCase):

    def test_a_full_board_renders_as_the_factorys_page(self):
        html = render(build())

        self.assertIn('class="plant-board ops-board"', html)
        self.assertIn('class="ops-board__inner"', html)
        self.assertIn('class="ops-topbar"', html)
        self.assertIn("ops-pill-live", html)
        self.assertIn('<div class="t" data-clock>', html)
        self.assertIn('<main class="ops-stack">', html)
        for domain in ("purchase", "store", "production", "shifting"):
            self.assertIn(f'class="ops-band ops-b-{domain}"', html)
        self.assertEqual(html.count('class="ops-people"'), 4)
        self.assertEqual(html.count('class="ops-grp'), 14)
        self.assertIn("Plant Control", html)
        self.assertIn("Purchase <em>BH-PM · BH-BS · BH-NM · BH-PC</em>", html)
        self.assertIn("grid-template-columns: minmax(0, 1.05fr) minmax(0, 1.15fr) minmax(0, 1fr) minmax(0, 1fr)", html)
        self.assertIn('<b class="ops-nm">Plan this month</b>', html)
        self.assertIn('<span class="ops-tag ops-t-bad">54.8% produced</span>', html)
        self.assertIn("<b>₹38.1 L</b><u>total PM required</u>", html)
        self.assertIn('<span class="ops-k-main">PM to purchase<b>₹16.4 L</b></span>', html)
        self.assertIn('class="ops-bars"', html)
        self.assertIn('<span class="ops-now">Today</span>', html)
        self.assertIn("min-width: var(--meter-h)", html)
        self.assertIn("Elsewhere · DISPATCH, GP-FG", html)
        self.assertNotIn("ops-unread", html)
        self.assertNotIn("None", html)
        self.assertNotIn("nan", html)
        # Warnings ride in the shared meta strip as well as the Check chip.
        self.assertIn("bcx-meta", html)

    def test_a_tile_that_is_none_is_an_unread_tile_never_a_zero(self):
        board = build(postgres=_raising_postgres("blowing_days"))
        html = render(board)

        self.assertIn('data-tile="blowing"', html)
        self.assertIn('<div class="ops-unread"><b>Not read</b>FactoryQueryError: column does not exist</div>', html)
        self.assertIn('<span class="ops-tag ops-t-nil">not read</span>', html)
        self.assertRegex(html, re.compile(r"blowing: not read", re.I))  # the shared _meta include
        self.assertIn("<em>Feed</em> 1 tile unread", html)
        self.assertNotIn("bottles</u>", html)

    def test_an_all_none_board_still_renders_whole(self):
        payload = {name: None for name in ("plan", *TILES, "workforce")}
        payload["meta"] = {
            "degraded": ["plan", *TILES, "workforce"],
            "reasons": {name: "SAP HANA could not be reached." for name in ("plan", *TILES)},
            "warnings": [],
            "withheld": [],
        }
        payload["meta"]["reasons"]["workforce"] = "The factory database could not be reached: refused"
        html = render(payload)

        self.assertEqual(html.count('class="ops-unread"'), 14)
        self.assertEqual(html.count('class="ops-band'), 4)
        self.assertIn("data-clock", html)
        self.assertIn("<em>Plan</em> not read", html)
        self.assertIn("no production plan", html)
        self.assertNotIn("₹0", html)
        self.assertNotIn("None", html)
        # Every people strip and header total is a rule.
        self.assertEqual(html.count('class="n ops-nil"'), 3)
        self.assertIn('<b><span class="ops-nil">—</span></b>', html)

    def test_an_empty_day_reads_as_one(self):
        quiet = dict(
            pm_in=[{"vehicles": 0, "pos": 0, "lines": 0, "received": 0, "accepted": 0, "rejected": 0}],
            blowing_days=[],
            blowing_running=[{"runs": 0}],
            today_runs=[],
            waste_logs=[],
            declared=[],
            shipped=[],
        )
        html = render(build(postgres=fake_postgres(**quiet)))

        self.assertIn('<span class="ops-tag ops-t-nil">no line running</span>', html)
        self.assertIn('<span class="ops-tag ops-t-nil">nothing planned</span>', html)
        self.assertIn('<span class="ops-tag ops-t-nil">nothing logged</span>', html)
        self.assertIn('<span class="ops-tag ops-t-nil">nothing declared</span>', html)
        self.assertIn("0 transfers out", html)
        self.assertNotIn("ops-unread", html)

    def test_the_stylesheet_is_sized_in_the_board_unit(self):
        path = finders.find("boards/plant.css")
        with open(path, encoding="utf-8") as handle:
            css = handle.read()
        self.assertIn(".ops-wall .plant-board.ops-board .ops-stack", css)
        self.assertIn("grid-template-rows: repeat(4, minmax(0, 1fr))", css)
        lengths = re.findall(r"\b\d+(?:\.\d+)?(?:px|rem)\b", css)
        # Only the unit's own clamp bounds are in pixels.
        self.assertEqual(sorted(set(lengths)), ["30px", "6px"])
