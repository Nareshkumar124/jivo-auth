"""
The accounts board: the factory's cash box, summarised, read straight from
the factory's PostgreSQL. A port of the factory's ``accounts_board`` (its
``services.py``, ``constants.py`` and ``views.py``), payload for payload.

WHAT IT SHOWS
-------------
What came in, what went out, what is still in the drawer, what is out with
people, and what is waiting to go to head office. Every figure is read off
``cash_book``, the custodian's own register, and nothing else: no SAP call
is made anywhere, so every section passes ``needs_sap=False`` and a HANA
outage cannot touch this board.

THE FOUR HEADLINE FIGURES
-------------------------
``imprest_issued``  paid ONTO the imprest card(s), over N top-ups.
``cash_issued``     drawn OFF them at a machine, over N withdrawals.
``pending_ho``      vouchers not yet sent to head office.
``cash_in_hand``    what should physically be in the drawer.

The first two are one float seen from each end, so they are comparable; what
separates them over the card's life is what is still on it
(``imprest.card_balance``), computed properly rather than by subtracting one
tile from the other. Spending is deliberately not one of the four: it is a
third population again and travels as ``paid_out``, the figure the breakdown
totals.

A PERIOD, AND ONE FIGURE THAT REFUSES TO HAVE ONE
--------------------------------------------------
The movement tiles follow the month: what was received, what was paid out,
what each head came to. Those are flows, and a flow without a period is
meaningless. **Cash in hand is a balance and ignores the period.** It is what
should be in the drawer right now; a "September cash in hand" is not a
smaller truth, it is a fabricated one, because the drawer does not reset on
the first of the month. Three tiles answer "during September" and one
answers "as of now", they sit in one column, and only their labels stop
somebody subtracting one from another.

CASH IN HAND IS THE REGISTER'S OWN IDENTITY
--------------------------------------------
Not ``imprest issued - cash issued`` (the card's two ends say nothing about
the drawer) and not the last ``balance_after`` either: the running balance
is kept in recording order (``id``), not date order, and it stops at the
drawer's in-and-out, ignoring the money sitting out with people. The
factory's ``cash_book.services.reconciliation`` is what the register heads
itself with, so it is what this board reads too::

    cash in - approved out - awaiting approval - advances + owed = cash in hand

Advances and reimbursements are never netted: a positive balance is our
cash in somebody's pocket, a negative one is their cash in our till.

**The balance can be negative, and on the live book it has been.** It means
more has left the box than ever entered it, which is real, reportable, and
almost always a receipt nobody has typed yet. It is never clamped at zero;
``in_hand_negative`` flags it so no consumer re-derives the sign.

NAMES ARE ALWAYS MASKED
-----------------------
The carousel is a wall screen in a corridor. The factory masks every
per-person row for a reader without ``cash_book.can_view_cash_book``, which
is exactly the display login's position; here there is no reader at all, so
that mask is permanent: holders are "Person N", salary vouchers are
"Voucher N" with an empty narrative, and the queries never select a name or
a narrative that could carry one. The totals and counts are the point of the
board; only who goes. ``meta.names_visible`` is always False.

A BUCKET WITH NO HEADS IS NOT ZERO
-----------------------------------
A breakdown line whose code list is empty reports ``has_source: False`` and
the template prints "not kept in the cash book" rather than a confident
0.00: the two look identical on a screen and mean opposite things.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import formatting
from ..sections import SectionBuilder

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

#: How often the screen re-reads itself. The cash book is typed up by one
#: person a few times a day, so a faster poll would only add load to show
#: the same figure. The factory's ``ACCOUNTS_BOARD_REFRESH_SECONDS``.
REFRESH_SECONDS = 300

#: Asks for the newest month this book has entries in, whatever that is. The
#: factory page's default, resolved on the server so the wall opens on the
#: month somebody is working in rather than on a five-month total.
LATEST = "latest"

#: SAP B1's own lower bound for a posting date is 1900; anything below it is
#: a typo rather than a period. The upper bound only excludes nonsense.
MIN_YEAR = 1900
MAX_YEAR = 2999

#: How many people one people-table sends down.
MAX_PEOPLE_ROWS = 25
#: How many G/L heads the "other" remainder names.
MAX_HEAD_ROWS = 12
#: How many vouchers / top-ups a list names before reporting ``truncated``.
MAX_PENDING_ROWS = 50

#: The two approval states that mean "out of the drawer, nobody has agreed
#: it". Rejected belongs with pending: the money is just as gone, and the
#: voucher is now somebody's to fix.
UNAGREED_STATES = ("PENDING", "REJECTED")

#: What a masked person is called. Numbered, so thirteen holders still read
#: as thirteen people rather than collapsing into one anonymous row.
MASKED_LABEL = "Person {n}"
#: The same for a salary voucher, whose label is free text that names
#: somebody mid-sentence. Masked whole: there is no safe way to show part of
#: a narrative.
MASKED_VOUCHER_LABEL = "Voucher {n}"

# ---------------------------------------------------------------------------
# The four detail lines (the factory's accounts_board/constants.py)
#
# The heads are listed by CODE, not pattern-matched on their names: the name
# is not the classification. A head that belongs to no bucket falls into
# ``other``, where it is visible rather than silently dropped.
# ---------------------------------------------------------------------------

#: Cash paid to outside suppliers for goods and services. NOT an
#: accounts-payable balance: the cash book keeps no vendor ledger, so this is
#: a spend and the board labels it "paid to suppliers", never "outstanding".
VENDOR_AP_CODES = (
    "5670001",  # FREIGHT AND CARTAGE OUTWARD-INDIRECT EXP
    "5680028",  # FREIGHT INWARD-INDIRECT
    "5670002",  # UNLOADING/LOADING CHARGES-INDIRECT EXPENSE
    "5100015",  # CONSUMABLE/DIRECT EXPENSE
    "5650016",  # REPAIR AND MAINTENANCE PLANT & MACHINERY
    "5650001",  # REPAIR & MAINTENANCE OFFICE & BUILDING
    "5650002",  # REPAIR & MAINTENANCE VEHICLE
    "1205001",  # ELECTRICAL APPLIANCES
)

#: Running the site: the heads a petty cash box exists for.
EXPENSE_CODES = (
    "5630004",  # REFRESHMENT
    "5630003",  # STAFF WELFARE
    "5690002",  # CONVEYANCE
    "5680015",  # HOUSE KEEPING
    "5680023",  # POSTAGE & COURIER
    "5680012",  # PRINTING AND STATIONERY
    "5680013",  # LAB AND TESTING
    "5650015",  # FUEL - VEHICLES
    "5680003",  # TELEPHONE MOBILE AND INTERNET
    "5680025",  # LEGAL AND PROFESSIONAL
    "5660002",  # RENT
    "5610003",  # BANK CHARGES
)

#: Money that moves a person's pay rather than buying anything. One is a
#: balance-sheet account and the other a P&L one, which is exactly why they
#: are one line: from the cash box both are "we paid a person something that
#: is not an expense of running the factory today".
SALARY_ADJUSTMENT_CODES = (
    "1101015",  # SUNDRY DEBTORS STAFF -- advance against salary
    "5630001",  # SALARY EXPENSE -- increments and arrears paid in cash
)

#: The fourth line is NOT a G/L bucket. The whiteboard's "payment penalty"
#: had no head to read; "payment pending" (out of the drawer, not yet agreed)
#: has an exact source, the approval state, so it is computed from that.
PAYMENT_PENDING_CODES: Tuple[str, ...] = ()

#: The four lines of the detail box, in the order the whiteboard draws them.
#: ``codes`` empty means the register has no source for this line and the
#: service reports ``has_source: False`` rather than a zero.
DETAIL_BUCKETS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "vendor_ap",
        "label": "Vendor A/P",
        "note": "Cash paid to suppliers out of the box. The register keeps no "
        "vendor ledger, so this is a spend, not an outstanding balance.",
        "codes": VENDOR_AP_CODES,
    },
    {
        "key": "expenses",
        "label": "Expenses",
        "note": "Running the site: refreshment, welfare, conveyance, "
        "housekeeping, stationery and the rest.",
        "codes": EXPENSE_CODES,
    },
    {
        "key": "salary_adjustment",
        "label": "Salary adjustment",
        "note": "Advances against salary, and increments paid in cash.",
        "codes": SALARY_ADJUSTMENT_CODES,
    },
    {
        "key": "payment_pending",
        "label": "Payment pending",
        "note": "Out of the drawer, not yet agreed. Read off the approval "
        "state, not a G/L head.",
        "codes": PAYMENT_PENDING_CODES,
        # "Compute me from the approval state instead of summing codes." A
        # flag rather than a fourth empty list, so the service cannot
        # accidentally report it as a bucket with no source.
        "from_approval_state": True,
    },
)

#: Every code that belongs to a named bucket. Anything else lands in ``other``.
CLASSIFIED_CODES = frozenset(code for bucket in DETAIL_BUCKETS for code in bucket["codes"])

#: The heads that make a payment a salary matter, for the voucher list. The
#: same pair the ``salary_adjustment`` bucket totals, named again rather than
#: aliased because the two answer different questions.
SALARY_PERSON_CODES = SALARY_ADJUSTMENT_CODES


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _dec(value: Any) -> Decimal:
    """A Decimal from whatever the driver (or a fake) hands back; None is 0."""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _count(value: Any) -> int:
    return int(value or 0)


def _money(value: Any) -> float:
    """A Decimal as JSON. Float, because every consumer is a formatted tile."""
    return float(_dec(value))


def month_bounds(year: int, month: int) -> Tuple[date, date]:
    """First and last day of a month, inclusive both ends.

    Inclusive because ``entry_date`` is a date: a half-open range would need
    the first of the next month and invites the off-by-one where every
    month silently borrows the next one's first day.
    """
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def month_label(year: int, month: int) -> str:
    return f"{calendar.month_name[month]} {year}"


#: "Nothing was asked": latest unless a month is named.
_DEFAULT = object()


def parse_period(period: Any = _DEFAULT, year: Any = None, month: Any = None):
    """What month to report on: ``(year, month)``, ``LATEST`` or ``None``.

    The factory's ``_parse_period`` (its 400s become ``ValueError`` here),
    with one difference of default: the factory API's "nothing" means the
    whole book, while its page always asks for ``latest``. The wall shows
    what the page shows, so ``latest`` is the default here unless a month is
    named. ``period=None`` with no month is the whole book, which is a real
    choice rather than a missing default.

    ``month=13`` and ``year=0`` are rejected rather than clamped: clamping
    would answer a question nobody asked and label it with the month they
    did ask for, which on a finance screen is worse than an error.
    """
    if period is _DEFAULT:
        period = LATEST if (year is None and month is None) else None

    if period is not None:
        if period != LATEST:
            raise ValueError("period may only be 'latest'. Use year and month otherwise.")
        if year is not None or month is not None:
            raise ValueError("Send period=latest or year and month, not both.")
        return LATEST

    if year is None and month is None:
        return None
    if year is None or month is None:
        raise ValueError("Send year and month together, or neither.")

    try:
        year = int(year)
        month = int(month)
    except (TypeError, ValueError):
        raise ValueError("year and month must be whole numbers.") from None

    if not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(f"year must be between {MIN_YEAR} and {MAX_YEAR}.")
    if not 1 <= month <= 12:
        raise ValueError("month must be between 1 and 12.")

    return (year, month)


# ---------------------------------------------------------------------------
# SQL
#
# Every statement opens with a ``-- accounts: <name>`` line. It names the
# read in pg_stat_activity, and it is what the tests' fakes match on.
# Tables follow Django's ``<app>_<model>`` naming; the factory's BaseModel
# adds ``is_active`` (a cancelled voucher is kept by the register but is not
# money, so it is excluded everywhere).
# ---------------------------------------------------------------------------

SQL_COMPANY = """-- accounts: company
SELECT id, name FROM company_company WHERE code = %s
"""

SQL_LATEST_PERIOD = """-- accounts: latest_period
SELECT DATE_TRUNC('month', entry_date)::date AS month
FROM cash_book_cashentry
WHERE company_id = %s AND is_active
GROUP BY 1
ORDER BY 1 DESC
LIMIT 1
"""

SQL_PERIODS = """-- accounts: periods
SELECT DATE_TRUNC('month', entry_date)::date AS month
FROM cash_book_cashentry
WHERE company_id = %s AND is_active
GROUP BY 1
ORDER BY 1 DESC
"""

#: The register's own identity, read whole: never period-filtered.
SQL_RECONCILIATION = """-- accounts: reconciliation
SELECT
    COALESCE(SUM(CASE WHEN direction = 'IN' THEN amount END), 0) AS cash_in,
    COALESCE(SUM(CASE WHEN direction = 'OUT' AND approval_state = 'APPROVED'
                      THEN amount END), 0) AS approved_out,
    COALESCE(SUM(CASE WHEN direction = 'OUT' AND approval_state IN ('PENDING', 'REJECTED')
                      THEN amount END), 0) AS awaiting_out
FROM cash_book_cashentry
WHERE company_id = %s AND is_active
"""

#: Everyone who has ever held a float here, with what they hold now. Cash
#: handed over raises the balance; anything that is not a handout lowers it
#: (cash handed back, money they laid out themselves) and so does every
#: payment booked against them as ``advance_holder``, which is the act of
#: explaining what the money went on. ``last_cleared`` is the newest such
#: payment, over the whole book. Sorted the way the factory sorts (balance,
#: then the user's email), but no name or email is selected: the wall never
#: shows one.
SQL_HOLDERS = """-- accounts: holders
WITH people AS (
    SELECT person_id AS id
    FROM cash_book_advanceentry
    WHERE company_id = %s AND is_active
    UNION
    SELECT advance_holder_id
    FROM cash_book_cashentry
    WHERE company_id = %s AND is_active AND advance_holder_id IS NOT NULL
),
handed AS (
    SELECT person_id,
           COALESCE(SUM(CASE WHEN direction = 'GIVEN' THEN amount END), 0) AS given,
           COALESCE(SUM(CASE WHEN direction <> 'GIVEN' THEN amount END), 0) AS returned
    FROM cash_book_advanceentry
    WHERE company_id = %s AND is_active
    GROUP BY person_id
),
explained AS (
    SELECT advance_holder_id AS person_id,
           COALESCE(SUM(amount), 0) AS total,
           MAX(entry_date) AS last_cleared
    FROM cash_book_cashentry
    WHERE company_id = %s AND is_active AND direction = 'OUT'
      AND advance_holder_id IS NOT NULL
    GROUP BY advance_holder_id
)
SELECT p.id AS person_id,
       COALESCE(h.given, 0) - COALESCE(h.returned, 0) - COALESCE(x.total, 0) AS balance,
       x.last_cleared
FROM people p
JOIN accounts_user u ON u.id = p.id
LEFT JOIN handed h ON h.person_id = p.id
LEFT JOIN explained x ON x.person_id = p.id
ORDER BY balance DESC, u.email
"""

#: Money paid ONTO the imprest card(s): the card, not the drawer.
SQL_LOADS = """-- accounts: loads
SELECT COALESCE(SUM(r.amount), 0) AS total, COUNT(r.id) AS count
FROM cash_book_atmreceipt r
JOIN cash_book_atmaccount a ON a.id = r.account_id
WHERE a.company_id = %s AND r.is_active{period}
"""

SQL_LOADS_SPAN = """-- accounts: loads_span
SELECT MIN(r.received_on) AS first, MAX(r.received_on) AS last
FROM cash_book_atmreceipt r
JOIN cash_book_atmaccount a ON a.id = r.account_id
WHERE a.company_id = %s AND r.is_active{period}
"""

#: One row per top-up, newest first. A card's name is printed on the card,
#: so it is not personal data and stays unmasked.
SQL_IMPREST_ROWS = """-- accounts: imprest_rows
SELECT r.id, a.name, r.amount, r.received_on, r.detail
FROM cash_book_atmreceipt r
JOIN cash_book_atmaccount a ON a.id = r.account_id
WHERE a.company_id = %s AND r.is_active{period}
ORDER BY r.received_on DESC, r.id DESC
LIMIT %s
"""

SQL_IMPREST_CARDS = """-- accounts: imprest_cards
SELECT a.id, a.name,
       COALESCE(SUM(r.amount), 0) AS total,
       COUNT(r.id) AS count,
       MAX(r.received_on) AS last
FROM cash_book_atmreceipt r
JOIN cash_book_atmaccount a ON a.id = r.account_id
WHERE a.company_id = %s AND r.is_active{period}
GROUP BY a.id, a.name
ORDER BY total DESC
"""

#: What is left on each live card: opening, plus everything ever paid on,
#: less everything ever drawn off. Withdrawals are the cash receipts that
#: name the card, because that is the only place they are recorded.
SQL_CARD_BALANCES = """-- accounts: card_balances
SELECT a.id, a.opening_balance,
       COALESCE((SELECT SUM(r.amount) FROM cash_book_atmreceipt r
                 WHERE r.account_id = a.id AND r.is_active), 0) AS paid_on,
       COALESCE((SELECT SUM(e.amount) FROM cash_book_cashentry e
                 WHERE e.atm_account_id = a.id AND e.is_active AND e.direction = 'IN'), 0) AS drawn_off
FROM cash_book_atmaccount a
WHERE a.company_id = %s AND a.is_active
"""

#: Cash that reached the drawer, split by whether a card produced it.
SQL_RECEIPTS = """-- accounts: receipts
SELECT COALESCE(SUM(amount), 0) AS total,
       COUNT(id) AS count,
       COALESCE(SUM(CASE WHEN atm_account_id IS NOT NULL THEN amount END), 0) AS off_card,
       COUNT(CASE WHEN atm_account_id IS NOT NULL THEN id END) AS off_card_count
FROM cash_book_cashentry
WHERE company_id = %s AND is_active AND direction = 'IN'{period}
"""

#: The period's payments by approval state.
SQL_PAYMENT_STATES = """-- accounts: payment_states
SELECT COALESCE(SUM(amount), 0) AS total,
       COUNT(id) AS count,
       COALESCE(SUM(CASE WHEN approval_state = 'APPROVED' THEN amount END), 0) AS approved,
       COUNT(CASE WHEN approval_state = 'APPROVED' THEN id END) AS approved_count,
       COALESCE(SUM(CASE WHEN approval_state = 'PENDING' THEN amount END), 0) AS pending,
       COUNT(CASE WHEN approval_state = 'PENDING' THEN id END) AS pending_count,
       COALESCE(SUM(CASE WHEN approval_state = 'REJECTED' THEN amount END), 0) AS rejected,
       COUNT(CASE WHEN approval_state = 'REJECTED' THEN id END) AS rejected_count
FROM cash_book_cashentry
WHERE company_id = %s AND is_active AND direction = 'OUT'{period}
"""

SQL_PAYMENTS_SPAN = """-- accounts: payments_span
SELECT MIN(entry_date) AS first, MAX(entry_date) AS last
FROM cash_book_cashentry
WHERE company_id = %s AND is_active AND direction = 'OUT'{period}
"""

SQL_BY_BRANCH = """-- accounts: by_branch
SELECT br.name AS branch, COALESCE(SUM(e.amount), 0) AS amount, COUNT(e.id) AS count
FROM cash_book_cashentry e
LEFT JOIN cash_book_cashbranch br ON br.id = e.branch_id
WHERE e.company_id = %s AND e.is_active AND e.direction = 'OUT'{period}
GROUP BY br.name
ORDER BY amount DESC
"""

#: Cash handed to people in the period, and to how many of them.
SQL_HANDOUTS = """-- accounts: handouts
SELECT COALESCE(SUM(amount), 0) AS total,
       COUNT(id) AS count,
       COUNT(DISTINCT person_id) AS people
FROM cash_book_advanceentry
WHERE company_id = %s AND is_active AND direction = 'GIVEN'{period}
"""

#: Vouchers that have not gone to head office: entries in a bunch not marked
#: sent, plus approved entries in no bunch at all (a step further back, and
#: leaving it out would report a reassuring zero on a book where nothing has
#: been bundled yet). Not period-filtered: what is waiting is waiting,
#: whenever it was recorded, and a September filter would hide the August
#: voucher that has been stuck longest.
PENDING_WHERE = """e.company_id = %s AND e.is_active AND e.direction = 'OUT'
  AND ((e.bunch_id IS NOT NULL AND b.sent_at IS NULL)
       OR (e.bunch_id IS NULL AND e.approval_state = 'APPROVED'))"""

SQL_PENDING_TOTAL = f"""-- accounts: pending_total
SELECT COALESCE(SUM(e.amount), 0) AS total, COUNT(e.id) AS count
FROM cash_book_cashentry e
LEFT JOIN cash_book_cashbunch b ON b.id = e.bunch_id
WHERE {PENDING_WHERE}
"""

SQL_PENDING_ROWS = f"""-- accounts: pending_rows
SELECT e.id, e.item, e.gl_account_name, e.detail, e.amount, e.entry_date,
       e.approval_state, br.name AS branch, b.number AS bunch
FROM cash_book_cashentry e
LEFT JOIN cash_book_cashbunch b ON b.id = e.bunch_id
LEFT JOIN cash_book_cashbranch br ON br.id = e.branch_id
WHERE {PENDING_WHERE}
ORDER BY e.entry_date DESC, e.id DESC
LIMIT %s
"""

#: Why a zero is an answer: "nothing waiting, 47 already gone" is a
#: different statement from "no data", and the two look identical without
#: the count of sent bunches beside the tile.
SQL_BUNCHES = """-- accounts: bunches
SELECT COUNT(CASE WHEN sent_at IS NULL THEN id END) AS unsent,
       COUNT(CASE WHEN sent_at IS NOT NULL THEN id END) AS sent
FROM cash_book_cashbunch
WHERE company_id = %s AND is_active
"""

#: The period's payments per G/L head, for the buckets and the remainder.
SQL_BY_CODE = """-- accounts: by_code
SELECT gl_account_code, gl_account_name,
       COALESCE(SUM(amount), 0) AS amount, COUNT(id) AS count
FROM cash_book_cashentry
WHERE company_id = %s AND is_active AND direction = 'OUT'{period}
GROUP BY gl_account_code, gl_account_name
"""

#: The fourth line: out of the drawer, not yet agreed (or sent back).
SQL_UNAGREED = """-- accounts: unagreed
SELECT COALESCE(SUM(amount), 0) AS amount, COUNT(id) AS count
FROM cash_book_cashentry
WHERE company_id = %s AND is_active AND direction = 'OUT'
  AND approval_state = ANY(%s){period}
"""

#: Salary advances and cash increments, one row per voucher. No payroll is
#: read. NOT grouped by ``advance_holder``: that means "whose float this
#: clears", not "who this was for", and on the live register the one salary
#: voucher carrying one names the wrong person. The voucher's own words
#: (item, narrative) are what the factory would show a reader who may see
#: names; this wall may not, so they are not even selected.
SQL_SALARY_TOTAL = """-- accounts: salary_total
SELECT COALESCE(SUM(amount), 0) AS total, COUNT(id) AS count
FROM cash_book_cashentry
WHERE company_id = %s AND is_active AND direction = 'OUT'
  AND gl_account_code = ANY(%s){period}
"""

SQL_SALARY_ROWS = """-- accounts: salary_rows
SELECT id, amount, entry_date, gl_account_name
FROM cash_book_cashentry
WHERE company_id = %s AND is_active AND direction = 'OUT'
  AND gl_account_code = ANY(%s){period}
ORDER BY entry_date DESC, id DESC
LIMIT %s
"""

SQL_SALARY_HEADS = """-- accounts: salary_heads
SELECT gl_account_code, gl_account_name,
       COALESCE(SUM(amount), 0) AS amount, COUNT(id) AS count
FROM cash_book_cashentry
WHERE company_id = %s AND is_active AND direction = 'OUT'
  AND gl_account_code = ANY(%s){period}
GROUP BY gl_account_code, gl_account_name
ORDER BY amount DESC
"""

SQL_SALARY_SPAN = """-- accounts: salary_span
SELECT MIN(entry_date) AS first, MAX(entry_date) AS last
FROM cash_book_cashentry
WHERE company_id = %s AND is_active AND direction = 'OUT'
  AND gl_account_code = ANY(%s){period}
"""


class AccountsBoardService(SectionBuilder):
    """The whole accounts board, for one company, in one read."""

    REFRESH_SECONDS = REFRESH_SECONDS
    LATEST = LATEST
    #: A wall screen never names who holds the factory's cash.
    NAMES_VISIBLE = False

    def __init__(
        self,
        company_code: str,
        *,
        postgres,
        hana_for: Optional[Callable[[str], Any]] = None,
        today: Optional[date] = None,
        period: Any = _DEFAULT,
        year: Any = None,
        month: Any = None,
    ):
        """``period`` is ``LATEST`` (the default), ``None`` for the whole book,
        or a month given as ``year`` and ``month``; see :func:`parse_period`.

        Nothing is read here. The company row and a ``latest`` period are
        resolved inside the first section that needs them, so a database
        that is down degrades every section with its reason instead of
        breaking the constructor that ``compose`` retries with a stand-in.
        """
        self.company_code = company_code
        self.postgres = postgres
        # No SAP is read by this board; the reader factory is accepted so
        # every service is built the same way, and never called.
        self.hana_for = hana_for
        self.today = today or formatting.today()
        self.requested = parse_period(period, year, month)
        self.may_name = self.NAMES_VISIBLE
        self._company: Optional[Tuple[int, str]] = None
        self._period_resolved = False
        self._period: Optional[Tuple[int, int]] = None
        self._period_from: Optional[date] = None
        self._period_to: Optional[date] = None
        self._holder_rows: Optional[List[Dict[str, Any]]] = None
        self._init_sections()

    # ------------------------------------------------------------------ build

    def build(self) -> Dict[str, Any]:
        """Every band, each absent-able on its own, all ``needs_sap=False``."""

        def band(name: str, fn: Callable[[], Any]) -> Any:
            return self.section(name, fn, needs_sap=False)

        payload: Dict[str, Any] = {
            "headline": band("headline", self._headline),
            "imprest": band("imprest", self._imprest),
            "cash_issued": band("cash_issued", self._cash_issued),
            "pending_ho": band("pending_ho", self._pending_ho),
            "detail": band("detail", self._detail),
            "salary": band("salary", self._salary),
        }

        period = self._quietly(self._period_meta, None)
        periods = self._quietly(self._available_periods, [])

        payload["meta"] = {
            # ``compose`` overwrites ``company`` with the code; the name is
            # kept beside it for the title.
            "company": self.company_name,
            "company_name": self.company_name,
            "as_of": self.today.isoformat(),
            "generated_at": formatting.now().isoformat(),
            "refresh_seconds": self.REFRESH_SECONDS,
            "period": period,
            "periods": periods,
            # So no template infers from the shape of the data whether a
            # name is missing because there is none or because it may not
            # be shown. Here it is always the latter.
            "names_visible": self.may_name,
            **self.section_meta(),
        }
        return payload

    # --------------------------------------------------------------- company

    def _company_row(self) -> Tuple[int, str]:
        if self._company is None:
            row = self.postgres.one(SQL_COMPANY, [self.company_code])
            if not row:
                raise LookupError(
                    f"No company with code {self.company_code!r} in the factory database."
                )
            self._company = (int(row["id"]), str(row.get("name") or ""))
        return self._company

    @property
    def company_id(self) -> int:
        return self._company_row()[0]

    @property
    def company_name(self) -> str:
        return self._company[1] if self._company else ""

    # ---------------------------------------------------------------- period

    def _resolve_period(self) -> None:
        if self._period_resolved:
            return
        period = self.requested
        if period == LATEST:
            # A book with no entries resolves to None: there is no newest
            # month, and inventing one would open the screen on a month that
            # never existed.
            period = self._latest_period()
        self._period = period
        self._period_from, self._period_to = month_bounds(*period) if period else (None, None)
        self._period_resolved = True

    @property
    def period(self) -> Optional[Tuple[int, int]]:
        self._resolve_period()
        return self._period

    def _latest_period(self) -> Optional[Tuple[int, int]]:
        row = self.postgres.one(SQL_LATEST_PERIOD, [self.company_id])
        newest = formatting.as_date(row["month"]) if row else None
        return (newest.year, newest.month) if newest else None

    def _available_periods(self) -> List[Dict[str, Any]]:
        """The months this book actually has entries in, newest first, so a
        selector cannot land on a month that was never traded."""
        periods = []
        for row in self.postgres.rows(SQL_PERIODS, [self.company_id]):
            when = formatting.as_date(row.get("month"))
            if when:
                periods.append(
                    {"year": when.year, "month": when.month, "label": month_label(when.year, when.month)}
                )
        return periods

    def _period_meta(self) -> Optional[Dict[str, Any]]:
        period = self.period
        if not period:
            return None
        return {
            "year": period[0],
            "month": period[1],
            "from": self._period_from.isoformat(),
            "to": self._period_to.isoformat(),
            "label": month_label(*period),
        }

    def _in_period(self, column: str) -> Tuple[str, List[Any]]:
        """The SQL and params that restrict ``column`` to the period, or
        nothing at all for the whole book. ``column`` is always a literal
        from this module."""
        if not self.period:
            return "", []
        return f" AND {column} >= %s AND {column} <= %s", [self._period_from, self._period_to]

    def _quietly(self, build: Callable[[], Any], default: Any) -> Any:
        """A ``meta`` value with no section of its own. When the database is
        down the sections have already said so; any other failure is a
        caveat, not a reason to lose the board."""
        if self._factory_down:
            return default
        try:
            return build()
        except Exception as exc:  # noqa: BLE001 - meta must not break the board
            logger.warning("Accounts board meta: %s", exc)
            self.warn(f"The month list could not be read: {type(exc).__name__}: {exc}")
            return default

    # --------------------------------------------------------------- helpers

    def _one(self, sql: str, params: List[Any]) -> Dict[str, Any]:
        """An aggregate row, or an empty dict so every read degrades to the
        factory's zeros rather than to a KeyError."""
        return self.postgres.one(sql, params) or {}

    def _span(self, sql: str, params: List[Any]) -> Dict[str, Optional[str]]:
        """First and last date in a set. Every total that is not
        period-bound carries one: a lifetime total with no range on it looks
        exactly like a month-to-date one."""
        row = self._one(sql, params)
        return {"from": formatting.iso(row.get("first")), "to": formatting.iso(row.get("last"))}

    def _person_label(self, index: int) -> str:
        """A stable stand-in for a person. The factory shows the name to a
        reader holding ``can_view_cash_book``; the wall is never that reader."""
        return MASKED_LABEL.format(n=index + 1)

    def _voucher_label(self, index: int) -> str:
        """What a salary voucher is called on screen. Masked wholesale: the
        narrative names somebody in the middle of a sentence, so there is no
        safe way to show some of it."""
        return MASKED_VOUCHER_LABEL.format(n=index + 1)

    def _loads(self) -> Dict[str, Any]:
        clause, params = self._in_period("r.received_on")
        return self._one(SQL_LOADS.format(period=clause), [self.company_id, *params])

    def _receipts(self) -> Dict[str, Any]:
        clause, params = self._in_period("entry_date")
        return self._one(SQL_RECEIPTS.format(period=clause), [self.company_id, *params])

    def _payment_states(self) -> Dict[str, Any]:
        clause, params = self._in_period("entry_date")
        return self._one(SQL_PAYMENT_STATES.format(period=clause), [self.company_id, *params])

    def _holders_rows(self) -> List[Dict[str, Any]]:
        """Everyone who has ever held a float, with what they hold now, in
        the factory's order. Read once per build: the headline and the
        people panel must not disagree about the same balances."""
        if self._holder_rows is None:
            rows = [
                {
                    "person_id": row.get("person_id"),
                    "balance": _dec(row.get("balance")),
                    "last_cleared": formatting.iso(row.get("last_cleared")),
                }
                for row in self.postgres.rows(SQL_HOLDERS, [self.company_id] * 4)
            ]
            # The statement orders by balance then email; a stable sort on
            # the balance alone keeps that tie-break without reading it.
            rows.sort(key=lambda row: -row["balance"])
            self._holder_rows = rows
        return self._holder_rows

    def _reconciliation(self) -> Dict[str, Decimal]:
        """The six figures the register heads itself with, and their check.

        Split at the person, not the row: somebody can be handed cash twice
        and pay for a third thing themselves, and what matters is where they
        end up. It comes to zero because every term is read off the same
        ledger; it stops being zero the moment something in it does not.
        """
        totals = self._one(SQL_RECONCILIATION, [self.company_id])
        cash_in = _dec(totals.get("cash_in"))
        approved_out = _dec(totals.get("approved_out"))
        awaiting_out = _dec(totals.get("awaiting_out"))

        balances = [row["balance"] for row in self._holders_rows()]
        advances = sum((b for b in balances if b > ZERO), ZERO)
        owed = -sum((b for b in balances if b < ZERO), ZERO)

        in_hand = (cash_in - approved_out - awaiting_out) - advances + owed
        return {
            "cash_in": cash_in,
            "cash_out": approved_out,
            "awaiting_approval": awaiting_out,
            "cash_in_hand": in_hand,
            "advance_given": advances,
            "owed_to_people": owed,
            "difference": cash_in - approved_out - awaiting_out - in_hand - advances + owed,
        }

    # -------------------------------------------------------------- headline

    def _headline(self) -> Dict[str, Any]:
        """The four big figures. Three are flows and follow the period; the
        fourth is a balance and does not."""
        recon = self._reconciliation()
        in_hand = recon["cash_in_hand"]

        period_in = self._loads()
        receipts = self._receipts()
        spend = self._payment_states()
        pending = self._one(SQL_PENDING_TOTAL, [self.company_id])

        return {
            # --- flows, inside the selected period -------------------------
            "imprest_issued": _money(period_in.get("total")),
            "imprest_count": _count(period_in.get("count")),
            # Taken OFF the card at a machine, over N withdrawals.
            "cash_issued": _money(receipts.get("off_card")),
            "cash_issued_count": _count(receipts.get("off_card_count")),
            # Every rupee that reached the drawer, however it got there: the
            # withdrawals above PLUS cash handed straight in. Reported
            # rather than assumed equal.
            "into_box": _money(receipts.get("total")),
            "into_box_count": _count(receipts.get("count")),
            # What was actually spent out of the box: the breakdown's total.
            "paid_out": _money(spend.get("total")),
            "paid_out_count": _count(spend.get("count")),
            "pending_ho": _money(pending.get("total")),
            "pending_ho_count": _count(pending.get("count")),
            # --- a balance, always whole -----------------------------------
            "cash_in_hand": _money(in_hand),
            "in_hand_negative": in_hand < ZERO,
            "reconciliation": {
                "cash_in": _money(recon["cash_in"]),
                "cash_out": _money(recon["cash_out"]),
                "awaiting_approval": _money(recon["awaiting_approval"]),
                "advance_given": _money(recon["advance_given"]),
                "owed_to_people": _money(recon["owed_to_people"]),
                "difference": _money(recon["difference"]),
                "balances": recon["difference"] == ZERO,
            },
        }

    # --------------------------------------------------------------- imprest

    def _imprest(self) -> Dict[str, Any]:
        """Money paid ONTO the imprest cards, top-up by top-up.

        The card, not the drawer. Money is paid onto the card and later
        drawn off it at a machine, and that withdrawal is the cash receipt;
        reporting both under one heading would double the money, so the
        drawer is returned separately as ``into_box`` and never added.
        Rows are individual top-ups, newest first: with a single card on the
        live book, one row per card answered nothing.
        """
        clause, params = self._in_period("r.received_on")
        totals = self._loads()

        rows = [
            {
                "id": row.get("id"),
                "name": row.get("name") or "",
                "amount": _money(row.get("amount")),
                "last_updated": formatting.iso(row.get("received_on")),
                "detail": row.get("detail") or "",
            }
            for row in self.postgres.rows(
                SQL_IMPREST_ROWS.format(period=clause), [self.company_id, *params, MAX_PENDING_ROWS]
            )
        ]

        cards = [
            {
                "id": card.get("id"),
                "name": card.get("name") or "",
                "amount": _money(card.get("total")),
                "count": _count(card.get("count")),
                "last_updated": formatting.iso(card.get("last")),
            }
            for card in self.postgres.rows(SQL_IMPREST_CARDS.format(period=clause), [self.company_id, *params])
        ]

        drawer = self._receipts()
        into_box = _dec(drawer.get("total"))
        from_card = _dec(drawer.get("off_card"))
        count = _count(drawer.get("count"))
        card_count = _count(drawer.get("off_card_count"))

        # A BALANCE, so like cash in hand it ignores the period: the card's
        # opening figure plus everything ever loaded less everything ever
        # drawn, per live card. A month's arithmetic would give a different
        # number and look just as plausible.
        card_balance = sum(
            (
                _dec(account.get("opening_balance")) + _dec(account.get("paid_on")) - _dec(account.get("drawn_off"))
                for account in self.postgres.rows(SQL_CARD_BALANCES, [self.company_id])
            ),
            ZERO,
        )

        total_count = _count(totals.get("count"))
        return {
            "total": _money(totals.get("total")),
            "count": total_count,
            "holders": len(cards),
            "rows": rows,
            "truncated": total_count > len(rows),
            "cards": cards,
            "card_balance": _money(card_balance),
            "into_box": {
                "total": _money(into_box),
                "count": count,
                "drawn_off_card": _money(from_card),
                "drawn_off_card_count": card_count,
                "handed_in": _money(into_box - from_card),
                "handed_in_count": count - card_count,
            },
            "span": self._span(SQL_LOADS_SPAN.format(period=clause), [self.company_id, *params]),
        }

    # ----------------------------------------------------------- cash issued

    def _cash_issued(self) -> Dict[str, Any]:
        """Money out of the box: how much, over how many vouchers, to whom.

        "To how many persons" is answered off the ADVANCE register, not the
        payment rows: a payment's payee is free text in the narrative and
        cannot be counted, while an advance names a real person.
        """
        clause, params = self._in_period("entry_date")
        joined_clause, _ = self._in_period("e.entry_date")
        state = self._payment_states()

        handouts = self._one(SQL_HANDOUTS.format(period=clause), [self.company_id, *params])

        by_branch = [
            {
                "label": row.get("branch") or "Unassigned",
                "amount": _money(row.get("amount")),
                "count": _count(row.get("count")),
            }
            for row in self.postgres.rows(SQL_BY_BRANCH.format(period=joined_clause), [self.company_id, *params])
        ]

        return {
            "total": _money(state.get("total")),
            "debits": _count(state.get("count")),
            "states": [
                {
                    "key": "APPROVED",
                    "label": "Approved",
                    "amount": _money(state.get("approved")),
                    "count": _count(state.get("approved_count")),
                },
                {
                    "key": "PENDING",
                    "label": "Awaiting approval",
                    "amount": _money(state.get("pending")),
                    "count": _count(state.get("pending_count")),
                },
                {
                    "key": "REJECTED",
                    "label": "Sent back",
                    "amount": _money(state.get("rejected")),
                    "count": _count(state.get("rejected_count")),
                },
            ],
            "by_branch": by_branch,
            "handouts": {
                "total": _money(handouts.get("total")),
                "count": _count(handouts.get("count")),
                "people": _count(handouts.get("people")),
            },
            "holders": self._holders(),
            "span": self._span(SQL_PAYMENTS_SPAN.format(period=clause), [self.company_id, *params]),
        }

    def _holders(self) -> Dict[str, Any]:
        """Who is holding the factory's cash, and who the factory owes.

        Never netted: one figure covering both states neither. Balances are
        as-of-now and deliberately not period-filtered: what somebody holds
        is a position, like cash in hand. People settled back to zero are
        dropped: a dashboard answers "what is outstanding now". The masked
        number follows the factory's: it is the person's position in the
        full list, zeros included, so the same person keeps the same number
        whichever list they land in.
        """
        holding: List[Dict[str, Any]] = []
        owed: List[Dict[str, Any]] = []
        for index, row in enumerate(self._holders_rows()):
            balance = row["balance"]
            if balance == ZERO:
                continue
            record = {
                "name": self._person_label(index),
                "amount": _money(abs(balance)),
                # When they last explained some of it; null means never,
                # which is exactly the row worth looking at.
                "last_updated": row["last_cleared"],
            }
            (holding if balance > ZERO else owed).append(record)

        def summed(bucket: List[Dict[str, Any]]) -> Dict[str, Any]:
            return {
                "rows": bucket[:MAX_PEOPLE_ROWS],
                "people": len(bucket),
                "total": _money(sum((Decimal(str(r["amount"])) for r in bucket), ZERO)),
            }

        return {"holding": summed(holding), "owed": summed(owed)}

    # ------------------------------------------------------------ pending HO

    def _pending_ho(self) -> Dict[str, Any]:
        """What is still waiting to go to head office, item by item.

        Honest about its blind spot: a bunch that HAS gone but has not been
        reimbursed is indistinguishable here, because the register records
        no reimbursement. The count of already-sent bunches travels with the
        tile so a zero reads as "nothing waiting, 47 already gone".
        """
        rows = [
            {
                "id": row.get("id"),
                # The sheet's Item column: what was bought. Falls back to the
                # G/L head, then the narrative, so a row is never nameless.
                "item": (
                    (row.get("item") or "")
                    or (row.get("gl_account_name") or "")
                    or (row.get("detail") or "").strip()[:60]
                    or "Unnamed voucher"
                ),
                "amount": _money(row.get("amount")),
                "entry_date": formatting.iso(row.get("entry_date")),
                "gl_account_name": row.get("gl_account_name") or "",
                "branch": row.get("branch"),
                "approval_state": row.get("approval_state"),
                "bunch": row.get("bunch"),
                "detail": row.get("detail") or "",
            }
            for row in self.postgres.rows(SQL_PENDING_ROWS, [self.company_id, MAX_PENDING_ROWS])
        ]

        totals = self._one(SQL_PENDING_TOTAL, [self.company_id])
        bunches = self._one(SQL_BUNCHES, [self.company_id])

        count = _count(totals.get("count"))
        return {
            "total": _money(totals.get("total")),
            "count": count,
            "rows": rows,
            "truncated": count > len(rows),
            "unsent_bunches": _count(bunches.get("unsent")),
            "sent_bunches": _count(bunches.get("sent")),
        }

    # ------------------------------------------------------------ detail box

    def _detail(self) -> Dict[str, Any]:
        """The outstanding breakdown: four lines over the period's payments.

        Three are sums over configured G/L heads. The fourth, payment
        pending, is read off the approval state instead. Heads no bucket
        claims are shown in ``other`` rather than dropped, and not warned
        about: the fact travels with its number.
        """
        clause, params = self._in_period("entry_date")

        by_code: Dict[str, Dict[str, Any]] = {}
        for row in self.postgres.rows(SQL_BY_CODE.format(period=clause), [self.company_id, *params]):
            by_code[row.get("gl_account_code") or ""] = {
                "gl_account_code": row.get("gl_account_code") or "",
                "gl_account_name": row.get("gl_account_name") or "",
                "amount": _dec(row.get("amount")),
                "count": _count(row.get("count")),
            }

        def head_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return sorted(
                (
                    {
                        "code": r["gl_account_code"],
                        "name": r["gl_account_name"],
                        "amount": _money(r["amount"]),
                        "count": r["count"],
                    }
                    for r in rows
                ),
                key=lambda head: -head["amount"],
            )

        buckets = []
        for spec in DETAIL_BUCKETS:
            if spec.get("from_approval_state"):
                unagreed = self._one(
                    SQL_UNAGREED.format(period=clause), [self.company_id, list(UNAGREED_STATES), *params]
                )
                buckets.append(
                    {
                        "key": spec["key"],
                        "label": spec["label"],
                        "note": spec["note"],
                        "has_source": True,
                        "amount": _money(unagreed.get("amount")),
                        "count": _count(unagreed.get("count")),
                        "heads": [],
                    }
                )
                continue

            rows = [by_code[c] for c in spec["codes"] if c in by_code]
            buckets.append(
                {
                    "key": spec["key"],
                    "label": spec["label"],
                    "note": spec["note"],
                    "has_source": bool(spec["codes"]),
                    "amount": _money(sum((r["amount"] for r in rows), ZERO)),
                    "count": sum(r["count"] for r in rows),
                    "heads": head_rows(rows),
                }
            )

        unclassified = [row for code, row in by_code.items() if code not in CLASSIFIED_CODES]
        every = list(by_code.values())

        return {
            "buckets": buckets,
            "other": {
                "amount": _money(sum((r["amount"] for r in unclassified), ZERO)),
                "count": sum(r["count"] for r in unclassified),
                "heads": head_rows(unclassified)[:MAX_HEAD_ROWS],
            },
            # The whole payment side, so the four lines read as shares of
            # something. The same rows summed, so it cannot disagree with them.
            "total": _money(sum((r["amount"] for r in every), ZERO)),
            "debits": sum(r["count"] for r in every),
        }

    # ---------------------------------------------------------------- salary

    def _salary(self) -> Dict[str, Any]:
        """Salary advances and cash increments, voucher by voucher. No
        payroll is read: this is the cash book's own record of what was
        handed to somebody against their pay. Every row is masked."""
        clause, params = self._in_period("entry_date")
        codes = list(SALARY_PERSON_CODES)
        totals = self._one(SQL_SALARY_TOTAL.format(period=clause), [self.company_id, codes, *params])

        rows = [
            {
                "id": row.get("id"),
                "description": self._voucher_label(index),
                "amount": _money(row.get("amount")),
                "entry_date": formatting.iso(row.get("entry_date")),
                "gl_account_name": row.get("gl_account_name") or "",
                # The narrative is empty for a masked reader: a name in
                # prose is still a name.
                "detail": "",
            }
            for index, row in enumerate(
                self.postgres.rows(
                    SQL_SALARY_ROWS.format(period=clause), [self.company_id, codes, *params, MAX_PENDING_ROWS]
                )
            )
        ]

        heads = [
            {
                "code": row.get("gl_account_code") or "",
                "name": row.get("gl_account_name") or "",
                "amount": _money(row.get("amount")),
                "count": _count(row.get("count")),
            }
            for row in self.postgres.rows(SQL_SALARY_HEADS.format(period=clause), [self.company_id, codes, *params])
        ]

        count = _count(totals.get("count"))
        return {
            "total": _money(totals.get("total")),
            "count": count,
            "rows": rows,
            "truncated": count > len(rows),
            "heads": heads,
            "span": self._span(SQL_SALARY_SPAN.format(period=clause), [self.company_id, codes, *params]),
        }
