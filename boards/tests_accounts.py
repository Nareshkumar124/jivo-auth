"""
Tests for the accounts board, ported from the factory's ``accounts_board``.

Nothing here opens a connection: the service takes a ``FakePostgres`` that
answers each statement by the ``-- accounts: <name>`` line it opens with,
and ``SimpleTestCase`` refuses any query that would reach a real database.

The centre of gravity is :class:`PeriodTests`: three of the four headline
figures follow the month and the fourth, cash in hand, must not. The rest
guard the decisions a later reader would otherwise "tidy up": advances and
reimbursements are not netted, a person settled to zero is dropped, a
negative balance is kept, a bucket with no source is not a zero, and every
name is masked.
"""

import json
import re
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings

from . import registry
from .compose import build_board
from .services import accounts
from .services.accounts import (
    LATEST,
    MAX_HEAD_ROWS,
    MAX_PEOPLE_ROWS,
    MAX_PENDING_ROWS,
    AccountsBoardService,
    month_bounds,
    parse_period,
)
from .sources import FactoryQueryError, FactoryUnavailable, Unavailable
from .testing import FakePostgres, hana_factory

TODAY = date(2026, 9, 16)
COMPANY_ID = 3

#: Two heads that land in different board lines.
FREIGHT = ("5670001", "FREIGHT AND CARTAGE OUTWARD-INDIRECT EXP")  # vendor_ap
REFRESHMENT = ("5630004", "REFRESHMENT")  # expenses
STAFF_DEBTOR = ("1101015", "SUNDRY DEBTORS STAFF")  # salary_adjustment

#: A real-looking name and email that must never reach the payload.
REAL_NAME = "Bunty Sharma"
REAL_EMAIL = "bunty@jivo.in"

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
    return f"-- accounts: {name}\n"


def fake(**answers):
    """A factory database answering each named statement with its rows."""
    postgres = FakePostgres()
    for name, rows in answers.items():
        postgres.add(marker(name), rows)
    return postgres


class RaisingPostgres(FakePostgres):
    """A fake that refuses one statement with a query error."""

    def __init__(self, failing: str, answers=None):
        super().__init__(answers)
        self.failing = marker(failing)

    def rows(self, sql, params=None):
        if self.failing in sql:
            self.calls.append((sql, params))
            raise FactoryQueryError("column does not exist")
        return super().rows(sql, params)


def company_rows():
    return [{"id": COMPANY_ID, "name": "Jivo Oil"}]


def board_answers():
    """One full September book, hand-built so every figure can be checked.

    Reconciliation: 1,50,000 in; 50,000 approved out; 400 awaiting.
    Holders: one person holding 15,000, one settled to zero, one owed 4,000.
    So cash in hand = 1,50,000 - 50,000 - 400 - 15,000 + 4,000 = 88,600.
    """
    return dict(
        company=company_rows(),
        latest_period=[{"month": date(2026, 9, 1)}],
        periods=[{"month": date(2026, 9, 1)}, {"month": date(2026, 8, 1)}],
        reconciliation=[
            {"cash_in": Decimal("150000.00"), "approved_out": Decimal("50000.00"), "awaiting_out": Decimal("400.00")}
        ],
        holders=[
            {"person_id": 7, "balance": Decimal("15000.00"), "last_cleared": date(2026, 8, 9)},
            {"person_id": 9, "balance": Decimal("0.00"), "last_cleared": date(2026, 7, 1)},
            {"person_id": 4, "balance": Decimal("-4000.00"), "last_cleared": None},
        ],
        loads=[{"total": Decimal("40000.00"), "count": 1}],
        loads_span=[{"first": date(2026, 9, 5), "last": date(2026, 9, 5)}],
        imprest_rows=[
            {
                "id": 11,
                "name": "Imprest Debit Card (Vishal)",
                "amount": Decimal("40000.00"),
                "received_on": date(2026, 9, 5),
                "detail": "Imprest received",
            }
        ],
        imprest_cards=[
            {"id": 1, "name": "Imprest Debit Card (Vishal)", "total": Decimal("40000.00"), "count": 1, "last": date(2026, 9, 5)}
        ],
        card_balances=[
            {"id": 1, "opening_balance": Decimal("19538.00"), "paid_on": Decimal("120000.00"), "drawn_off": Decimal("80000.00")},
            {"id": 2, "opening_balance": Decimal("0.00"), "paid_on": Decimal("500.00"), "drawn_off": Decimal("0.00")},
        ],
        receipts=[
            {"total": Decimal("50000.00"), "count": 2, "off_card": Decimal("45000.00"), "off_card_count": 1}
        ],
        payment_states=[
            {
                "total": Decimal("20400.00"),
                "count": 4,
                "approved": Decimal("20000.00"),
                "approved_count": 3,
                "pending": Decimal("400.00"),
                "pending_count": 1,
                "rejected": Decimal("0.00"),
                "rejected_count": 0,
            }
        ],
        payments_span=[{"first": date(2026, 9, 2), "last": date(2026, 9, 14)}],
        by_branch=[
            {"branch": "Oil", "amount": Decimal("18000.00"), "count": 3},
            {"branch": None, "amount": Decimal("2400.00"), "count": 1},
        ],
        handouts=[{"total": Decimal("15000.00"), "count": 2, "people": 1}],
        pending_total=[{"total": Decimal("5000.00"), "count": 1}],
        pending_rows=[
            {
                "id": 21,
                "item": "Diesel",
                "gl_account_name": "FUEL - VEHICLES",
                "detail": "Diesel for the generator",
                "amount": Decimal("5000.00"),
                "entry_date": date(2026, 9, 12),
                "approval_state": "APPROVED",
                "branch": "Oil",
                "bunch": 48,
            }
        ],
        bunches=[{"unsent": 1, "sent": 47}],
        by_code=[
            {"gl_account_code": FREIGHT[0], "gl_account_name": FREIGHT[1], "amount": Decimal("1000.00"), "count": 1},
            {"gl_account_code": REFRESHMENT[0], "gl_account_name": REFRESHMENT[1], "amount": Decimal("2400.00"), "count": 2},
            {"gl_account_code": STAFF_DEBTOR[0], "gl_account_name": STAFF_DEBTOR[1], "amount": Decimal("3000.00"), "count": 1},
            {"gl_account_code": "9999999", "gl_account_name": "SOMETHING NEW", "amount": Decimal("75.00"), "count": 1},
        ],
        unagreed=[{"amount": Decimal("400.00"), "count": 1}],
        salary_total=[{"total": Decimal("3000.00"), "count": 1}],
        salary_rows=[
            {"id": 31, "amount": Decimal("3000.00"), "entry_date": date(2026, 9, 10), "gl_account_name": STAFF_DEBTOR[1]}
        ],
        salary_heads=[
            {"gl_account_code": STAFF_DEBTOR[0], "gl_account_name": STAFF_DEBTOR[1], "amount": Decimal("3000.00"), "count": 1}
        ],
        salary_span=[{"first": date(2026, 9, 10), "last": date(2026, 9, 10)}],
    )


def service(postgres, **kwargs):
    return AccountsBoardService("JIVO_OIL", postgres=postgres, hana_for=hana_factory(), today=TODAY, **kwargs)


def build(postgres, **kwargs):
    return service(postgres, **kwargs).build()


def calls_for(postgres, name):
    """Every (sql, params) recorded for one named statement."""
    return [(sql, params) for sql, params in postgres.calls if marker(name) in sql]


class AccountsTestCase(SimpleTestCase):
    def assertNoName(self, payload):
        text = json.dumps(payload)
        for secret in (REAL_NAME, "Bunty", REAL_EMAIL, "full_name"):
            self.assertNotIn(secret, text)


# ---------------------------------------------------------------- the period


class MonthBoundsTests(SimpleTestCase):
    """The range is inclusive at both ends."""

    def test_february_in_a_leap_year(self):
        self.assertEqual(month_bounds(2024, 2), (date(2024, 2, 1), date(2024, 2, 29)))

    def test_december_does_not_roll_into_january(self):
        self.assertEqual(month_bounds(2026, 12), (date(2026, 12, 1), date(2026, 12, 31)))


class PeriodValidationTests(SimpleTestCase):
    """The period is validated, not trusted: the factory's 400s, as ValueErrors."""

    def test_the_default_is_latest(self):
        self.assertEqual(parse_period(), LATEST)
        self.assertEqual(service(fake()).requested, LATEST)

    def test_a_month_may_be_named_without_touching_period(self):
        self.assertEqual(parse_period(year=2026, month=9), (2026, 9))
        self.assertEqual(parse_period(year="2026", month="9"), (2026, 9))

    def test_none_is_the_whole_book(self):
        self.assertIsNone(parse_period(period=None))

    def test_month_13_is_rejected_not_clamped(self):
        with self.assertRaises(ValueError) as raised:
            service(fake(), year=2026, month=13)
        self.assertEqual(str(raised.exception), "month must be between 1 and 12.")

    def test_year_zero_is_rejected(self):
        with self.assertRaises(ValueError) as raised:
            parse_period(year=0, month=1)
        self.assertEqual(str(raised.exception), "year must be between 1900 and 2999.")

    def test_a_year_alone_does_not_mean_january(self):
        with self.assertRaises(ValueError) as raised:
            parse_period(year=2026)
        self.assertEqual(str(raised.exception), "Send year and month together, or neither.")

    def test_only_latest_is_a_period(self):
        with self.assertRaises(ValueError) as raised:
            parse_period(period="now")
        self.assertEqual(str(raised.exception), "period may only be 'latest'. Use year and month otherwise.")

    def test_latest_and_a_month_together_are_refused(self):
        with self.assertRaises(ValueError) as raised:
            parse_period(period=LATEST, year=2026, month=9)
        self.assertEqual(str(raised.exception), "Send period=latest or year and month, not both.")

    def test_words_are_not_months(self):
        with self.assertRaises(ValueError) as raised:
            parse_period(year="twenty", month="9")
        self.assertEqual(str(raised.exception), "year and month must be whole numbers.")


class PeriodTests(AccountsTestCase):
    """Flows follow the month. The balance does not."""

    def test_latest_resolves_to_the_newest_month_with_entries(self):
        postgres = fake(**board_answers())
        board = build(postgres)

        self.assertEqual(
            board["meta"]["period"],
            {"year": 2026, "month": 9, "from": "2026-09-01", "to": "2026-09-30", "label": "September 2026"},
        )
        # Every flow statement carries September's bounds...
        for name in ("loads", "receipts", "payment_states", "handouts", "by_branch", "by_code", "salary_total"):
            with self.subTest(statement=name):
                sql, params = calls_for(postgres, name)[0]
                self.assertIn(">= %s", sql)
                self.assertEqual(params[-2:], [date(2026, 9, 1), date(2026, 9, 30)])
        # ...and the balances carry none.
        for name in ("reconciliation", "holders", "card_balances", "pending_total", "pending_rows", "bunches"):
            with self.subTest(statement=name):
                sql, params = calls_for(postgres, name)[0]
                self.assertNotIn("entry_date >=", sql)
                self.assertNotIn(date(2026, 9, 1), params)

    def test_a_named_month_is_used_as_given(self):
        postgres = fake(**board_answers())
        board = build(postgres, year=2026, month=8)

        self.assertEqual(board["meta"]["period"]["label"], "August 2026")
        self.assertEqual(calls_for(postgres, "latest_period"), [])
        sql, params = calls_for(postgres, "loads")[0]
        self.assertEqual(params, [COMPANY_ID, date(2026, 8, 1), date(2026, 8, 31)])

    def test_no_period_means_the_whole_book(self):
        postgres = fake(**board_answers())
        board = build(postgres, period=None)

        self.assertIsNone(board["meta"]["period"])
        sql, params = calls_for(postgres, "loads")[0]
        self.assertNotIn(">= %s", sql)
        self.assertEqual(params, [COMPANY_ID])

    def test_cash_in_hand_ignores_the_period_entirely(self):
        """The drawer does not reset on the first of the month."""
        balances = set()
        for kwargs in ({}, {"period": None}, {"year": 2026, "month": 8}, {"year": 2026, "month": 1}):
            balances.add(build(fake(**board_answers()), **kwargs)["headline"]["cash_in_hand"])
        self.assertEqual(balances, {88600.0})

    def test_an_empty_book_resolves_latest_to_the_whole_book(self):
        """No newest month exists; inventing one would open on a month that
        never was. Everything is present and zero, nothing is degraded."""
        board = build(fake(company=company_rows()))

        self.assertIsNone(board["meta"]["period"])
        self.assertEqual(board["meta"]["periods"], [])
        self.assertEqual(board["meta"]["degraded"], [])
        self.assertEqual(board["meta"]["warnings"], [])
        headline = board["headline"]
        self.assertEqual(headline["imprest_issued"], 0.0)
        self.assertEqual(headline["cash_issued"], 0.0)
        self.assertEqual(headline["cash_in_hand"], 0.0)
        self.assertFalse(headline["in_hand_negative"])
        self.assertTrue(headline["reconciliation"]["balances"])
        self.assertEqual(board["imprest"]["rows"], [])
        self.assertEqual(board["imprest"]["card_balance"], 0.0)
        self.assertEqual(board["imprest"]["span"], {"from": None, "to": None})
        self.assertEqual(board["cash_issued"]["holders"], {
            "holding": {"rows": [], "people": 0, "total": 0.0},
            "owed": {"rows": [], "people": 0, "total": 0.0},
        })
        self.assertEqual(board["pending_ho"]["sent_bunches"], 0)
        self.assertEqual(board["detail"]["total"], 0.0)
        self.assertEqual([b["amount"] for b in board["detail"]["buckets"]], [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(board["salary"]["rows"], [])

    def test_only_months_with_entries_are_offered(self):
        board = build(fake(**board_answers()))
        self.assertEqual([p["label"] for p in board["meta"]["periods"]], ["September 2026", "August 2026"])


# ---------------------------------------------------------------- the figures


class HeadlineTests(AccountsTestCase):

    def test_the_four_figures_and_their_companions(self):
        headline = build(fake(**board_answers()))["headline"]

        self.assertEqual(headline["imprest_issued"], 40000.0)
        self.assertEqual(headline["imprest_count"], 1)
        self.assertEqual(headline["cash_issued"], 45000.0)
        self.assertEqual(headline["cash_issued_count"], 1)
        self.assertEqual(headline["into_box"], 50000.0)
        self.assertEqual(headline["into_box_count"], 2)
        self.assertEqual(headline["paid_out"], 20400.0)
        self.assertEqual(headline["paid_out_count"], 4)
        self.assertEqual(headline["pending_ho"], 5000.0)
        self.assertEqual(headline["pending_ho_count"], 1)

    def test_cash_in_hand_is_the_registers_identity_not_a_subtraction(self):
        """cash in - approved out - awaiting - advances + owed.

        Not imprest issued less cash issued, and not into-box less paid-out:
        the money out with people is part of the answer, and the 4,000
        somebody laid out themselves is owed back, not netted away.
        """
        headline = build(fake(**board_answers()))["headline"]

        self.assertEqual(headline["cash_in_hand"], 88600.0)
        self.assertFalse(headline["in_hand_negative"])
        self.assertEqual(
            headline["reconciliation"],
            {
                "cash_in": 150000.0,
                "cash_out": 50000.0,
                "awaiting_approval": 400.0,
                "advance_given": 15000.0,
                "owed_to_people": 4000.0,
                "difference": 0.0,
                "balances": True,
            },
        )
        self.assertNotEqual(headline["cash_in_hand"], headline["imprest_issued"] - headline["cash_issued"])
        self.assertNotEqual(headline["cash_in_hand"], headline["into_box"] - headline["paid_out"])

    def test_the_reconciliation_reads_the_whole_book_in_one_statement(self):
        postgres = fake(**board_answers())
        build(postgres)

        (sql, params), = calls_for(postgres, "reconciliation")
        self.assertEqual(params, [COMPANY_ID])
        self.assertIn("approval_state = 'APPROVED'", sql)
        self.assertIn("IN ('PENDING', 'REJECTED')", sql)
        self.assertIn("is_active", sql)

    def test_a_negative_balance_survives_intact(self):
        answers = board_answers()
        answers["reconciliation"] = [{"cash_in": Decimal("1000"), "approved_out": Decimal("5000"), "awaiting_out": 0}]
        answers["holders"] = []
        headline = build(fake(**answers))["headline"]

        self.assertEqual(headline["cash_in_hand"], -4000.0)
        self.assertTrue(headline["in_hand_negative"])
        self.assertTrue(headline["reconciliation"]["balances"])


class ImprestTests(AccountsTestCase):

    def test_the_total_is_what_was_loaded_onto_the_card(self):
        imprest = build(fake(**board_answers()))["imprest"]

        self.assertEqual(imprest["total"], 40000.0)
        self.assertEqual(imprest["count"], 1)
        self.assertEqual(imprest["holders"], 1)
        self.assertFalse(imprest["truncated"])
        self.assertEqual(
            imprest["rows"],
            [
                {
                    "id": 11,
                    "name": "Imprest Debit Card (Vishal)",
                    "amount": 40000.0,
                    "last_updated": "2026-09-05",
                    "detail": "Imprest received",
                }
            ],
        )
        self.assertEqual(
            imprest["cards"],
            [{"id": 1, "name": "Imprest Debit Card (Vishal)", "amount": 40000.0, "count": 1, "last_updated": "2026-09-05"}],
        )
        self.assertEqual(imprest["span"], {"from": "2026-09-05", "to": "2026-09-05"})

    def test_the_drawer_is_reported_apart_and_never_added(self):
        imprest = build(fake(**board_answers()))["imprest"]

        self.assertEqual(
            imprest["into_box"],
            {
                "total": 50000.0,
                "count": 2,
                "drawn_off_card": 45000.0,
                "drawn_off_card_count": 1,
                "handed_in": 5000.0,
                "handed_in_count": 1,
            },
        )

    def test_the_card_balance_is_opening_plus_loaded_less_drawn_per_live_card(self):
        """Not this period's on minus off: 19,538 + 1,20,000 - 80,000, plus
        the second card's 500."""
        postgres = fake(**board_answers())
        board = build(postgres)

        self.assertEqual(board["imprest"]["card_balance"], 60038.0)
        self.assertNotEqual(
            board["imprest"]["card_balance"],
            board["headline"]["imprest_issued"] - board["headline"]["cash_issued"],
        )
        (sql, params), = calls_for(postgres, "card_balances")
        self.assertEqual(params, [COMPANY_ID])
        self.assertIn("a.is_active", sql)
        self.assertIn("e.direction = 'IN'", sql)

    def test_a_long_list_is_cut_and_says_so(self):
        answers = board_answers()
        answers["loads"] = [{"total": Decimal("1"), "count": MAX_PENDING_ROWS + 3}]
        imprest = build(fake(**answers))["imprest"]
        self.assertTrue(imprest["truncated"])
        # The limit travels as a parameter of the rows statement.
        postgres = fake(**answers)
        build(postgres)
        self.assertEqual(calls_for(postgres, "imprest_rows")[0][1][-1], MAX_PENDING_ROWS)


class CashIssuedTests(AccountsTestCase):

    def test_states_branches_and_handouts(self):
        cash_issued = build(fake(**board_answers()))["cash_issued"]

        self.assertEqual(cash_issued["total"], 20400.0)
        self.assertEqual(cash_issued["debits"], 4)
        self.assertEqual(
            cash_issued["states"],
            [
                {"key": "APPROVED", "label": "Approved", "amount": 20000.0, "count": 3},
                {"key": "PENDING", "label": "Awaiting approval", "amount": 400.0, "count": 1},
                {"key": "REJECTED", "label": "Sent back", "amount": 0.0, "count": 0},
            ],
        )
        self.assertEqual(
            cash_issued["by_branch"],
            [{"label": "Oil", "amount": 18000.0, "count": 3}, {"label": "Unassigned", "amount": 2400.0, "count": 1}],
        )
        self.assertEqual(cash_issued["handouts"], {"total": 15000.0, "count": 2, "people": 1})
        self.assertEqual(cash_issued["span"], {"from": "2026-09-02", "to": "2026-09-14"})

    def test_approved_only_spend_is_the_approved_state(self):
        """What the register counts as spent is the approved part alone;
        awaiting and sent-back money is reported beside it, never folded in."""
        board = build(fake(**board_answers()))
        approved = board["cash_issued"]["states"][0]
        self.assertEqual(approved["amount"], board["headline"]["reconciliation"]["cash_out"] - 30000.0)
        self.assertEqual(approved["amount"] + 400.0, board["headline"]["paid_out"])


class HolderTests(AccountsTestCase):
    """Who holds our cash, and who we owe. Never one figure, never a name."""

    def test_advances_and_reimbursements_are_not_netted(self):
        holders = build(fake(**board_answers()))["cash_issued"]["holders"]

        self.assertEqual(holders["holding"]["total"], 15000.0)
        self.assertEqual(holders["holding"]["people"], 1)
        self.assertEqual(holders["owed"]["total"], 4000.0)
        self.assertEqual(holders["owed"]["people"], 1)

    def test_people_are_masked_and_numbered_by_their_place_in_the_full_list(self):
        """"Person N" is the factory's numbering: the position in the whole
        holder list, zeros included, so somebody settled to zero is dropped
        but keeps the number for everyone after them."""
        holders = build(fake(**board_answers()))["cash_issued"]["holders"]

        self.assertEqual(
            holders["holding"]["rows"],
            [{"name": "Person 1", "amount": 15000.0, "last_updated": "2026-08-09"}],
        )
        self.assertEqual(
            holders["owed"]["rows"],
            [{"name": "Person 3", "amount": 4000.0, "last_updated": None}],
        )

    def test_somebody_settled_back_to_zero_is_dropped(self):
        answers = board_answers()
        answers["holders"] = [{"person_id": 9, "balance": Decimal("0.00"), "last_cleared": None}]
        holders = build(fake(**answers))["cash_issued"]["holders"]

        self.assertEqual(holders["holding"]["rows"], [])
        self.assertEqual(holders["owed"]["rows"], [])

    def test_the_list_is_sorted_by_balance_and_capped(self):
        answers = board_answers()
        answers["holders"] = [
            {"person_id": n, "balance": Decimal(100 * (n + 1)), "last_cleared": None} for n in range(MAX_PEOPLE_ROWS + 5)
        ]
        holding = build(fake(**answers))["cash_issued"]["holders"]["holding"]

        self.assertEqual(holding["people"], MAX_PEOPLE_ROWS + 5)
        self.assertEqual(len(holding["rows"]), MAX_PEOPLE_ROWS)
        self.assertEqual(holding["rows"][0]["amount"], 3000.0)
        self.assertEqual(holding["rows"][0]["name"], "Person 1")

    def test_the_holder_statement_reads_both_registers_and_no_name(self):
        postgres = fake(**board_answers())
        build(postgres)

        (sql, params), = calls_for(postgres, "holders")
        self.assertEqual(params, [COMPANY_ID] * 4)
        self.assertIn("direction = 'GIVEN'", sql)
        self.assertIn("direction <> 'GIVEN'", sql)
        self.assertIn("advance_holder_id", sql)
        self.assertIn("MAX(entry_date) AS last_cleared", sql)
        self.assertNotIn("full_name", sql)
        self.assertNotIn("u.email,", sql)
        self.assertRegex(sql, r"ORDER BY balance DESC, u\.email\s*$")

    def test_holders_are_read_once_for_the_headline_and_the_people_panel(self):
        postgres = fake(**board_answers())
        build(postgres)
        self.assertEqual(len(calls_for(postgres, "holders")), 1)


class PendingHeadOfficeTests(AccountsTestCase):

    def test_what_is_waiting(self):
        pending = build(fake(**board_answers()))["pending_ho"]

        self.assertEqual(pending["total"], 5000.0)
        self.assertEqual(pending["count"], 1)
        self.assertEqual(pending["unsent_bunches"], 1)
        self.assertEqual(pending["sent_bunches"], 47)
        self.assertFalse(pending["truncated"])
        self.assertEqual(
            pending["rows"][0],
            {
                "id": 21,
                "item": "Diesel",
                "amount": 5000.0,
                "entry_date": "2026-09-12",
                "gl_account_name": "FUEL - VEHICLES",
                "branch": "Oil",
                "approval_state": "APPROVED",
                "bunch": 48,
                "detail": "Diesel for the generator",
            },
        )

    def test_both_populations_and_no_period(self):
        """Entries in an unsent bunch, plus approved entries in no bunch at
        all; whenever they were recorded."""
        postgres = fake(**board_answers())
        build(postgres, year=2026, month=9)

        for name in ("pending_total", "pending_rows"):
            with self.subTest(statement=name):
                sql, params = calls_for(postgres, name)[0]
                self.assertIn("e.bunch_id IS NOT NULL AND b.sent_at IS NULL", sql)
                self.assertIn("e.bunch_id IS NULL AND e.approval_state = 'APPROVED'", sql)
                self.assertIn("e.direction = 'OUT'", sql)
                self.assertNotIn("entry_date >=", sql)
                self.assertEqual(params[0], COMPANY_ID)

    def test_a_voucher_with_no_item_still_has_a_name(self):
        answers = board_answers()
        answers["pending_rows"] = [
            {"id": 1, "item": "", "gl_account_name": "REFRESHMENT", "detail": "x", "amount": 1, "entry_date": date(2026, 9, 1),
             "approval_state": "APPROVED", "branch": None, "bunch": None},
            {"id": 2, "item": "", "gl_account_name": "", "detail": "  Paid the courier for the samples sent to the lab in Delhi on Monday  ",
             "amount": 1, "entry_date": date(2026, 9, 1), "approval_state": "APPROVED", "branch": None, "bunch": None},
            {"id": 3, "item": "", "gl_account_name": "", "detail": "", "amount": 1, "entry_date": date(2026, 9, 1),
             "approval_state": "APPROVED", "branch": None, "bunch": None},
        ]
        rows = build(fake(**answers))["pending_ho"]["rows"]

        self.assertEqual(rows[0]["item"], "REFRESHMENT")
        self.assertEqual(rows[1]["item"], "Paid the courier for the samples sent to the lab in Delhi on Monday"[:60])
        self.assertEqual(rows[2]["item"], "Unnamed voucher")
        self.assertIsNone(rows[0]["bunch"])


class DetailBucketTests(AccountsTestCase):

    def buckets(self, **answers):
        board = build(fake(**{**board_answers(), **answers}))
        return board, {b["key"]: b for b in board["detail"]["buckets"]}

    def test_heads_land_in_their_configured_line(self):
        board, buckets = self.buckets()

        self.assertEqual(buckets["vendor_ap"]["amount"], 1000.0)
        self.assertEqual(buckets["expenses"]["amount"], 2400.0)
        self.assertEqual(buckets["expenses"]["count"], 2)
        self.assertEqual(buckets["salary_adjustment"]["amount"], 3000.0)
        self.assertEqual(buckets["expenses"]["heads"], [{"code": "5630004", "name": "REFRESHMENT", "amount": 2400.0, "count": 2}])
        self.assertEqual([b["key"] for b in board["detail"]["buckets"]], ["vendor_ap", "expenses", "salary_adjustment", "payment_pending"])
        for bucket in board["detail"]["buckets"]:
            self.assertTrue(bucket["has_source"], bucket["key"])
            self.assertTrue(bucket["note"])

    def test_payment_pending_reads_the_approval_state_not_a_head(self):
        board, buckets = self.buckets()
        pending = buckets["payment_pending"]

        self.assertEqual(pending["amount"], 400.0)
        self.assertEqual(pending["count"], 1)
        self.assertTrue(pending["has_source"])
        self.assertEqual(pending["heads"], [])

        postgres = fake(**board_answers())
        build(postgres)
        (sql, params), = calls_for(postgres, "unagreed")
        self.assertEqual(params[:2], [COMPANY_ID, ["PENDING", "REJECTED"]])

    def test_an_unclassified_head_is_shown_but_not_warned_about(self):
        board, _ = self.buckets()

        self.assertEqual(board["detail"]["other"]["amount"], 75.0)
        self.assertEqual(board["detail"]["other"]["count"], 1)
        self.assertEqual(board["detail"]["other"]["heads"][0]["name"], "SOMETHING NEW")
        self.assertEqual(board["meta"]["warnings"], [])

    def test_buckets_and_other_account_for_every_payment(self):
        board, buckets = self.buckets()
        counted = sum(b["amount"] for b in board["detail"]["buckets"] if b["key"] != "payment_pending")
        counted += board["detail"]["other"]["amount"]

        self.assertEqual(counted, board["detail"]["total"])
        self.assertEqual(board["detail"]["total"], 6475.0)
        self.assertEqual(board["detail"]["debits"], 5)

    def test_heads_are_sorted_largest_first_and_the_remainder_is_capped(self):
        by_code = [
            {"gl_account_code": f"8{n:06d}", "gl_account_name": f"HEAD {n}", "amount": Decimal(n), "count": 1}
            for n in range(1, MAX_HEAD_ROWS + 4)
        ]
        board, _ = self.buckets(by_code=by_code)
        other = board["detail"]["other"]

        self.assertEqual(other["count"], MAX_HEAD_ROWS + 3)
        self.assertEqual(len(other["heads"]), MAX_HEAD_ROWS)
        self.assertEqual(other["heads"][0]["name"], f"HEAD {MAX_HEAD_ROWS + 3}")

    def test_a_bucket_with_no_source_is_not_a_zero(self):
        """``has_source`` False, amount 0 and the template says "not kept",
        never 0.00. Nothing is in that state today, so a fifth line is
        patched in to prove the rule."""
        unsourced = ({"key": "penalty", "label": "Payment penalty", "note": "No head records this.", "codes": ()},)
        with mock.patch.object(accounts, "DETAIL_BUCKETS", accounts.DETAIL_BUCKETS + unsourced):
            board, buckets = self.buckets()
            html = render(board)

        self.assertFalse(buckets["penalty"]["has_source"])
        self.assertEqual(buckets["penalty"]["amount"], 0.0)
        self.assertIn("Not kept in the cash book", html)
        self.assertNotIn("₹0</span>", html.split("Payment penalty")[1].split("</div>")[0])


class SalaryTests(AccountsTestCase):

    def test_only_salary_heads_count(self):
        postgres = fake(**board_answers())
        salary = build(postgres)["salary"]

        self.assertEqual(salary["total"], 3000.0)
        self.assertEqual(salary["count"], 1)
        self.assertEqual(salary["heads"], [{"code": "1101015", "name": "SUNDRY DEBTORS STAFF", "amount": 3000.0, "count": 1}])
        self.assertEqual(salary["span"], {"from": "2026-09-10", "to": "2026-09-10"})
        self.assertFalse(salary["truncated"])
        for name in ("salary_total", "salary_rows", "salary_heads", "salary_span"):
            with self.subTest(statement=name):
                sql, params = calls_for(postgres, name)[0]
                self.assertIn("gl_account_code = ANY(%s)", sql)
                self.assertEqual(params[1], ["1101015", "5630001"])

    def test_every_voucher_is_a_masked_row(self):
        answers = board_answers()
        answers["salary_rows"] = [
            {"id": 31, "amount": Decimal("3000"), "entry_date": date(2026, 9, 10), "gl_account_name": STAFF_DEBTOR[1]},
            {"id": 30, "amount": Decimal("1000"), "entry_date": date(2026, 9, 3), "gl_account_name": STAFF_DEBTOR[1]},
        ]
        answers["salary_total"] = [{"total": Decimal("4000"), "count": 2}]
        salary = build(fake(**answers))["salary"]

        self.assertEqual(
            salary["rows"],
            [
                {"id": 31, "description": "Voucher 1", "amount": 3000.0, "entry_date": "2026-09-10",
                 "gl_account_name": STAFF_DEBTOR[1], "detail": ""},
                {"id": 30, "description": "Voucher 2", "amount": 1000.0, "entry_date": "2026-09-03",
                 "gl_account_name": STAFF_DEBTOR[1], "detail": ""},
            ],
        )
        self.assertEqual(sum(r["amount"] for r in salary["rows"]), salary["total"])

    def test_the_voucher_statement_never_selects_the_words_that_name_somebody(self):
        postgres = fake(**board_answers())
        build(postgres)
        (sql, params), = calls_for(postgres, "salary_rows")
        self.assertNotIn("item", sql)
        self.assertNotIn("detail", sql)
        self.assertNotIn("advance_holder", sql)


class NameMaskingTests(AccountsTestCase):
    """A wall screen gets the figures. It does not get the staff list."""

    def test_names_are_never_visible(self):
        answers = board_answers()
        # A fake that leaks a name anyway must not get it into the payload.
        answers["holders"][0]["full_name"] = REAL_NAME
        answers["holders"][0]["email"] = REAL_EMAIL
        board = build(fake(**answers))

        self.assertFalse(board["meta"]["names_visible"])
        self.assertNoName(board)
        self.assertEqual(board["cash_issued"]["holders"]["holding"]["rows"][0]["name"], "Person 1")
        self.assertEqual(board["salary"]["rows"][0]["description"], "Voucher 1")
        self.assertEqual(board["salary"]["rows"][0]["detail"], "")

    def test_masking_hides_the_name_and_nothing_else(self):
        board = build(fake(**board_answers()))
        self.assertEqual(board["cash_issued"]["holders"]["holding"]["total"], 15000.0)
        self.assertEqual(board["cash_issued"]["holders"]["holding"]["people"], 1)
        self.assertEqual(board["salary"]["total"], 3000.0)

    def test_a_card_is_named_even_when_people_are_not(self):
        """A card's name is printed on the card. It is not personal data."""
        board = build(fake(**board_answers()))
        self.assertEqual(board["imprest"]["rows"][0]["name"], "Imprest Debit Card (Vishal)")

    def test_masked_people_stay_distinguishable(self):
        answers = board_answers()
        answers["holders"] = [
            {"person_id": 7, "balance": Decimal("15000"), "last_cleared": None},
            {"person_id": 8, "balance": Decimal("8000"), "last_cleared": None},
        ]
        rows = build(fake(**answers))["cash_issued"]["holders"]["holding"]["rows"]
        self.assertEqual([r["name"] for r in rows], ["Person 1", "Person 2"])


# ------------------------------------------------------------ meta and shape


class PayloadTests(AccountsTestCase):

    def test_the_payload_is_plain_json_with_the_factorys_keys(self):
        board = build(fake(**board_answers()))

        json.dumps(board)
        self.assertEqual(
            list(board), ["headline", "imprest", "cash_issued", "pending_ho", "detail", "salary", "meta"]
        )
        meta = board["meta"]
        self.assertEqual(meta["refresh_seconds"], 300)
        self.assertEqual(meta["as_of"], "2026-09-16")
        self.assertEqual(meta["company"], "Jivo Oil")
        self.assertEqual(meta["company_name"], "Jivo Oil")
        self.assertFalse(meta["names_visible"])
        self.assertEqual(meta["withheld"], [])
        self.assertEqual(meta["degraded"], [])
        self.assertIn("generated_at", meta)

    def test_the_company_is_looked_up_by_code(self):
        postgres = fake(**board_answers())
        build(postgres)
        self.assertEqual(calls_for(postgres, "company")[0][1], ["JIVO_OIL"])

    def test_every_statement_is_named_and_scoped_to_the_company(self):
        postgres = fake(**board_answers())
        build(postgres)

        for sql, params in postgres.calls:
            with self.subTest(sql=sql.splitlines()[0]):
                self.assertRegex(sql, r"^-- accounts: [a-z_]+\n")
                if marker("company") not in sql:
                    self.assertIn("company_id = %s", sql)
                    self.assertEqual(params[0], COMPANY_ID)

    def test_no_sap_is_read(self):
        class Explodes:
            def __call__(self, company_code):
                raise AssertionError("the accounts board must not touch SAP")

        board = AccountsBoardService("JIVO_OIL", postgres=fake(**board_answers()), hana_for=Explodes(), today=TODAY).build()
        self.assertEqual(board["meta"]["degraded"], [])

    def test_the_registry_points_at_this_service(self):
        slide = registry.get_slide("accounts")
        self.assertIs(slide.service_class(), AccountsBoardService)
        for path in (*slide.css, *slide.js):
            self.assertIsNotNone(finders.find(path), path)

    @override_settings(FACTORY_DB=FACTORY_OFF, FACTORY_HANA=HANA_OFF, FACTORY_COMPANY_CODE="JIVO_OIL")
    def test_compose_builds_it_like_any_other_board(self):
        postgres = fake(**board_answers())
        with mock.patch("boards.compose.connect") as connect_mock:
            connect_mock.return_value.__enter__.return_value = postgres
            payload = build_board(registry.get_slide("accounts"))

        self.assertEqual(payload["meta"]["slide"], "accounts")
        self.assertEqual(payload["meta"]["company"], "JIVO_OIL")
        self.assertEqual(payload["meta"]["company_name"], "Jivo Oil")
        self.assertEqual(payload["meta"]["refresh_seconds"], 300)
        self.assertEqual(payload["headline"]["cash_in_hand"], 88600.0)


class DegradedTests(AccountsTestCase):

    def test_a_database_that_is_down_degrades_every_section_with_the_reason(self):
        stand_in = Unavailable(FactoryUnavailable("The factory database could not be reached: refused"))
        board = build(stand_in)

        for name in ("headline", "imprest", "cash_issued", "pending_ho", "detail", "salary"):
            self.assertIsNone(board[name], name)
        self.assertEqual(board["meta"]["degraded"], ["headline", "imprest", "cash_issued", "pending_ho", "detail", "salary"])
        self.assertIn("could not be reached", board["meta"]["reasons"]["salary"])
        self.assertIsNone(board["meta"]["period"])
        self.assertEqual(board["meta"]["periods"], [])
        self.assertTrue(any("factory database" in w for w in board["meta"]["warnings"]))
        self.assertEqual(board["meta"]["withheld"], [])

    def test_an_unknown_company_degrades_every_section(self):
        board = build(fake())

        self.assertIsNone(board["headline"])
        self.assertEqual(len(board["meta"]["degraded"]), 6)
        self.assertIn("No company with code 'JIVO_OIL'", board["meta"]["reasons"]["headline"])
        self.assertEqual(board["meta"]["company"], "")

    def test_one_failing_statement_costs_one_section(self):
        postgres = RaisingPostgres("by_code")
        for name, rows in board_answers().items():
            postgres.add(marker(name), rows)
        board = build(postgres)

        self.assertIsNone(board["detail"])
        self.assertEqual(board["meta"]["degraded"], ["detail"])
        self.assertEqual(board["meta"]["reasons"]["detail"], "FactoryQueryError: column does not exist")
        self.assertEqual(board["headline"]["cash_in_hand"], 88600.0)
        self.assertEqual(board["salary"]["total"], 3000.0)

    def test_a_section_never_raises_out_of_build(self):
        answers = board_answers()
        answers["reconciliation"] = [{"cash_in": "not a number"}]
        board = build(fake(**answers))

        self.assertIsNone(board["headline"])
        self.assertIn("headline", board["meta"]["degraded"])
        self.assertIsNotNone(board["imprest"])


# --------------------------------------------------------------- the template


def render(payload):
    return render_to_string(
        "boards/fragments/accounts.html",
        {"board": payload, "meta": payload["meta"], "slide": registry.get_slide("accounts")},
    )


class TemplateTests(AccountsTestCase):

    def test_the_figures_and_their_sub_labels(self):
        html = render(build(fake(**board_answers())))

        self.assertIn('class="ops-board ab-board"', html)
        self.assertIn('class="ops-board__inner"', html)
        self.assertIn('class="ops-topbar"', html)
        self.assertIn("ops-pill-live", html)
        self.assertIn('class="ops-clock" data-clock', html)
        self.assertIn("September 2026", html)
        self.assertIn("in September 2026", html)
        self.assertIn("closing balance, all time", html)
        self.assertIn("₹40,000", html)  # imprest issued
        self.assertIn("₹45,000", html)  # cash issued
        self.assertIn("₹20,400 spent out of the box", html)
        self.assertIn("₹5,000", html)  # pending from HO
        self.assertIn("₹88,600", html)  # cash in hand
        self.assertIn("₹60,038 on the card", html)
        self.assertIn("47 bunches already sent", html)
        self.assertIn("ab-stat--filled", html)
        self.assertNotIn("ab-stat--alert", html)
        self.assertIn("The book balances", html)
        self.assertIn("Imprest Debit Card (Vishal)", html)
        self.assertIn("Diesel", html)
        self.assertIn("Person 1", html)
        self.assertIn("Person 3", html)
        self.assertIn("Voucher 1", html)
        self.assertIn("Other heads ₹75 over 1 voucher", html)
        self.assertIn("Names are hidden on this screen", html)
        self.assertNotIn("ops-unread", html)
        self.assertNotIn("bcx-meta", html)

    def test_the_buckets_carry_their_keys_for_the_colours(self):
        html = render(build(fake(**board_answers())))
        for key in ("vendor_ap", "expenses", "salary_adjustment", "payment_pending"):
            self.assertIn(f'class="ab-bucket__fill" data-bucket="{key}"', html)
            self.assertIn(f'data-ab-stage data-bucket="{key}"', html)
        self.assertIn("1 / 4", html)
        self.assertIn("data-ab-progress", html)
        # Only the first line is on stage before the script runs.
        self.assertEqual(html.count("data-ab-stage"), 4)
        self.assertEqual(len(re.findall(r'data-ab-stage data-bucket="[a-z_]+" hidden', html)), 3)
        self.assertIn("Vendor A/P", html)
        self.assertIn("not an outstanding balance", html)

    def test_no_real_name_reaches_the_screen(self):
        answers = board_answers()
        answers["holders"][0]["full_name"] = REAL_NAME
        html = render(build(fake(**answers)))
        self.assertNotIn(REAL_NAME, html)
        self.assertNotIn("Bunty", html)

    def test_a_negative_balance_is_shown_in_full(self):
        answers = board_answers()
        answers["reconciliation"] = [{"cash_in": Decimal("1000"), "approved_out": Decimal("5000"), "awaiting_out": 0}]
        answers["holders"] = []
        html = render(build(fake(**answers)))

        self.assertIn("ab-stat--alert", html)
        self.assertIn("−₹4,000", html)
        self.assertNotIn("₹-4,000", html)
        self.assertIn("Below zero", html)

    def test_a_section_that_is_none_is_an_unread_tile_never_a_zero(self):
        postgres = RaisingPostgres("salary_total")
        for name, rows in board_answers().items():
            postgres.add(marker(name), rows)
        payload = build(postgres)
        html = render(payload)

        self.assertIn('<div class="ops-unread"><b>Not read</b>FactoryQueryError: column does not exist</div>', html)
        self.assertIn("Salary: not read", html)  # the _meta include
        self.assertIn("bcx-meta__chip--degraded", html)
        self.assertNotIn("No salary advance in this period", html)
        self.assertIn("₹88,600", html)

    def test_everything_unread_still_renders_a_whole_board(self):
        payload = build(Unavailable(FactoryUnavailable("The factory database could not be reached: refused")))
        html = render(payload)

        self.assertEqual(html.count("ops-unread"), 11)  # four stat slots + seven panels
        self.assertIn("could not be reached", html)
        self.assertIn("Whole book", html)
        self.assertIn("data-clock", html)
        self.assertNotIn("₹0", html)

    def test_an_empty_book_says_so_rather_than_showing_nothing(self):
        html = render(build(fake(company=company_rows())))

        self.assertIn("No imprest card was loaded in this period.", html)
        self.assertIn("Nothing waiting. All 0 bunches have gone to head office.", html)
        self.assertIn("No salary advance in this period.", html)
        self.assertIn("Nobody is holding the factory's cash.", html)
        self.assertNotIn("ops-unread", html)

    def test_long_lists_are_cut_and_say_so(self):
        answers = board_answers()
        answers["pending_rows"] = [
            {**answers["pending_rows"][0], "id": n, "item": f"Item {n}"} for n in range(8)
        ]
        answers["pending_total"] = [{"total": Decimal("40000"), "count": 8}]
        answers["holders"] = [
            {"person_id": n, "balance": Decimal(1000), "last_cleared": None} for n in range(9)
        ]
        html = render(build(fake(**answers)))

        self.assertIn("Showing the 5 most recent of 8", html)
        self.assertIn("Item 4", html)
        self.assertNotIn("Item 5", html)
        self.assertIn("+3 more", html)
        self.assertIn("Person 6", html)
        self.assertNotIn("Person 7", html)
