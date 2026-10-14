"""
The dispatch clock, split into the legs nobody currently compares.
A port of the factory's ``board_builder/cards/dispatch.py``.

WHAT THIS MEASURES AND WHY IT IS WORTH A CARD
----------------------------------------------
``SalesDispatchGateOut`` (``gate_core_salesdispatchgateout``) stamps three
moments on every docking: ``docked_at`` when the truck is put on a bay,
``printed_at`` when its gate pass is printed, and ``dispatched_at`` when it
leaves. All three are written and none of them are ever subtracted from each
other.

Doing it splits the wait into two very different problems. Dock-to-print is
LOADING: boxes onto a truck and a bill reconciled. Print-to-out is the GATE:
paperwork, security, the barrier. Measured over 120 days of production data
the first leg is about 1h 15m and the second about 39m, so roughly two thirds
of the clock sits in the half nobody watches, and a single "turnaround"
figure hides which of the two moved.

THE READ
--------
The factory iterated the three stamps and subtracted in Python. Here the
database does the subtraction (``EXTRACT(EPOCH FROM ...)``) and hands back
two second-counts per docking, so the wide SAP payload columns never cross
the wire. The negatives are still dropped in Python, where the reason can be
written down.
"""

from __future__ import annotations

from datetime import timedelta
from typing import List

from ... import formatting
from . import viz
from .catalogue import CardContext, CardOption, CardSpec, register

WINDOW = CardOption(
    key="window_days",
    label="Window",
    kind="choice",
    default="30",
    choices=(("7", "Last 7 days"), ("30", "Last 30 days"), ("90", "Last 90 days")),
)

#: Companies are joined by code because the service only knows the code: the
#: factory's ``company_company`` table has ``id, code, name, is_active``.
STAGE_LEGS_SQL = """
    SELECT
        EXTRACT(EPOCH FROM (d.printed_at - d.docked_at))     AS load_seconds,
        EXTRACT(EPOCH FROM (d.dispatched_at - d.printed_at)) AS gate_seconds
    FROM gate_core_salesdispatchgateout AS d
    JOIN company_company AS c ON c.id = d.company_id
    WHERE d.is_active = TRUE
      AND c.code = %s
      AND d.docked_at >= %s
      AND d.printed_at IS NOT NULL
      AND d.dispatched_at IS NOT NULL
"""


def _mean_hours(seconds: List[float]) -> float:
    """Arithmetic mean of a list of second-counts, in hours. Empty reads zero."""
    return (sum(seconds) / len(seconds) / 3600) if seconds else 0.0


def _stage_clock(context: CardContext) -> dict:
    days = int(context.option("window_days", "30"))
    # The factory subtracts from the instant now; so does this, from the
    # board's own clock, so a board composed for a test day reads the same
    # window the factory would have.
    since = context.now - timedelta(days=days)

    rows = context.postgres.rows(STAGE_LEGS_SQL, [context.company_code, since])

    load_legs: List[float] = []
    gate_legs: List[float] = []
    for row in rows:
        load = formatting.f(row.get("load_seconds"))
        gate = formatting.f(row.get("gate_seconds"))
        # Out-of-order stamps happen on a record corrected by hand. Dropped
        # rather than clamped: a negative leg averaged in is a wrong figure,
        # and a zero leg is a claim that a stage took no time.
        if load >= 0 and gate >= 0:
            load_legs.append(load)
            gate_legs.append(gate)

    if not load_legs:
        return viz.missing(
            f"No docking completed both stages in the last {days} days.",
            sub="Docked to printed to gone",
        )

    load_hours = _mean_hours(load_legs)
    gate_hours = _mean_hours(gate_legs)
    total = load_hours + gate_hours
    loading_share = round(load_hours / total * 100) if total else 0

    return viz.figure(
        value=f"{total:.2f}",
        unit="h average",
        sub=f"{len(load_legs):,} dockings · last {days} days",
        tag=viz.tag(
            f"{loading_share}% loading",
            "warn" if loading_share >= 60 else "neut",
        ),
        # One scale for the two legs, so the visible gap between the bars is
        # the difference in minutes and not a choice of axis.
        viz=viz.pair(
            [
                ("Dock → print", load_hours, "main"),
                ("Print → out", gate_hours, "light"),
            ],
            note="Average hours per leg, one scale",
        ),
    )


register(
    CardSpec(
        key="dispatch_stage_clock",
        title="Dispatch stage clock",
        summary="Docked → gate pass printed → gone, as two legs on one scale.",
        category="Dispatch",
        feed="sales_dispatch_out",
        columns=2,
        rows=1,
        accent="dispatch",
        options=(WINDOW,),
        build=_stage_clock,
        note=(
            "Only dockings that completed BOTH stages inside the window are "
            "counted, so a truck still on a bay never shortens the average. "
            "Dock-to-print is loading; print-to-out is the gate."
        ),
    )
)
