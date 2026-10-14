"""
The shapes a card is allowed to be, as data.

A port of the factory's ``board_builder/viz.py``, plus the one thing this
service has to do that the factory's frontend did: turn the shapes into
geometry. ``CardViz.tsx`` computed every bar's share of its set in the
browser; here the template is plain Django with no arithmetic, so
:func:`present` precomputes the widths, heights and figures and the template
only prints them.

WHY A CARD RETURNS A SHAPE AND NOT MARKUP
-----------------------------------------
The set is closed on purpose and it is small. Every entry below exists as a
component on the factory's wall boards (``OpsMeter``, ``OpsBars``,
``OpsPair``) or in the builder's own stylesheet (table, matrix, split), so a
card built here and a band on the Plant board draw the same bar the same
way. A card that needs a shape nobody has drawn yet adds it HERE, once, with
its markup in ``built.html``.

WHAT A CARD ALWAYS RETURNS
--------------------------
:func:`figure` builds the envelope, and every card goes through it. A card
that cannot compute its number returns :func:`missing` instead, never zero.
The boards' one hard rule is that an empty warehouse and an unreadable one
must not look the same.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from ...templatetags.boards import indian

#: What a tag's colour means. ``nil`` is "there is no source", which is not
#: a condition and never amber.
Tone = Literal["neut", "ok", "warn", "bad", "nil"]

#: Which tint of the card's own hue a bar segment wears. Never a literal
#: colour: the card names a weight and the board supplies the hue.
Fill = Literal["main", "light", "mute"]


def tag(label: str, tone: Tone = "neut") -> dict:
    """The pill in the card's top-right: a share, a comparison, a p90."""
    return {"label": label, "tone": tone}


def figure(
    value: str,
    *,
    unit: str = "",
    sub: str = "",
    tag: Optional[dict] = None,  # noqa: A002 - matches the payload key
    viz: Optional[dict] = None,
    note: str = "",
) -> dict:
    """A card that has a number.

    ``value`` is a STRING and already formatted. Formatting happens on the
    server because that is where the context is: whether this figure is
    rupees, tonnes or hours, whether a lakh should compact to "L", how many
    decimals still carry meaning.
    """
    return {
        "value": value,
        "unit": unit,
        "sub": sub,
        "tag": tag,
        "viz": viz,
        "note": note,
        "missing": None,
    }


def missing(reason: str, *, sub: str = "") -> dict:
    """A card with nothing behind it, and the reason on its face.

    Not an error and not a zero. The tile renders a rule where the figure
    goes and prints ``reason`` underneath.
    """
    return {
        "value": "",
        "unit": "",
        "sub": sub,
        "tag": None,
        "viz": None,
        "note": "",
        "missing": reason,
    }


# ---------------------------------------------------------------------------
# The shapes
# ---------------------------------------------------------------------------


def meter(segments: Iterable[Tuple[str, float, Fill]]) -> dict:
    """One quantity split into its parts, as a single composition bar.

    Segments share one scale and are laid in order, so the bar reads as the
    WHOLE of something. Never for unrelated quantities side by side.
    """
    return {
        "kind": "meter",
        "segments": [
            {"label": label, "value": float(value), "fill": fill}
            for label, value, fill in segments
        ],
    }


def bars(days: Iterable[Tuple[str, float, bool]]) -> dict:
    """A short column chart: the recent window, with the current one picked out.

    Heights are a share of the TALLEST column in the window, not of a target.
    That is the only question a chart this size can answer honestly: "is
    today normal for this week".
    """
    return {
        "kind": "bars",
        "days": [
            {"label": label, "value": float(value), "current": bool(current)}
            for label, value, current in days
        ],
    }


def pair(rows: Iterable[Tuple[str, float, Fill]], *, note: str = "") -> dict:
    """Two or three figures as stacked bars on ONE shared scale.

    Scaled against the largest of the set, so the visible gap between them is
    the difference and nothing else.
    """
    return {
        "kind": "pair",
        "rows": [
            {"label": label, "value": float(value), "fill": fill}
            for label, value, fill in rows
        ],
        "note": note,
    }


def matrix(
    *,
    columns: List[str],
    rows: List[Tuple[str, List[Optional[int]]]],
    caption: str = "",
) -> dict:
    """Counts across two dimensions: age bands down, stages across.

    Bands must be EXCLUSIVE windows, so the rows sum to the total.
    """
    return {
        "kind": "matrix",
        "columns": list(columns),
        "rows": [{"label": label, "cells": list(cells)} for label, cells in rows],
        "caption": caption,
    }


def table(
    *,
    columns: List[str],
    rows: List[List[str]],
    aligns: Optional[List[Literal["left", "right"]]] = None,
) -> dict:
    """A short list: the worst five, the oldest four.

    Every cell is a formatted string. Keep it to what fits without
    scrolling: a card is a headline with its evidence under it.
    """
    return {
        "kind": "table",
        "columns": list(columns),
        "rows": [[str(cell) for cell in row] for row in rows],
        "aligns": list(aligns) if aligns else ["left"] * len(columns),
    }


def split(parts: Iterable[Tuple[str, str]]) -> dict:
    """Two to four small labelled figures, for a card answering one question
    in several parts: the three legs of a stage clock, say."""
    return {
        "kind": "split",
        "parts": [{"label": label, "value": value} for label, value in parts],
    }


def nothing() -> dict:
    """No visualisation: a card that is one big number and its subtitle.

    Returned explicitly rather than by passing ``viz=None``, so a card author
    saying "this is deliberately just a figure" reads differently from one
    who forgot.
    """
    return {"kind": "none"}


#: Every kind the template knows. Asserted against in the tests so a shape
#: added here without markup fails the build rather than a wall screen.
VIZ_KINDS: frozenset = frozenset({"meter", "bars", "pair", "matrix", "table", "split", "none"})


def validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Check a card's own output before it reaches a screen.

    Cheap, and worth it: a card is somebody's query written six weeks after
    this file, and the failure mode of a malformed payload is a blank tile
    on a wall rather than a stack trace anybody sees.
    """
    viz = payload.get("viz")
    if viz is not None and viz.get("kind") not in VIZ_KINDS:
        raise ValueError(
            f"Card returned an unknown visualisation {viz.get('kind')!r}. "
            f"Known kinds: {', '.join(sorted(VIZ_KINDS))}."
        )
    if payload.get("missing") is None and not payload.get("value"):
        raise ValueError(
            "A card must return either a value or a reason it has none. "
            "Use viz.missing(reason) rather than an empty figure."
        )
    return payload


# ---------------------------------------------------------------------------
# Geometry, precomputed for the template
# ---------------------------------------------------------------------------


def compact(value: Any) -> str:
    """One decimal where the fraction carries meaning, none where it does
    not. The frontend's ``compact()``: whole numbers in Indian grouping."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    if abs(number) >= 100:
        return indian(round(number))
    if abs(number) >= 10:
        return f"{number:.1f}"
    return f"{number:.2f}"


def share_of(value: float, largest: float) -> float:
    """A value as a share of the largest in its set, 0-100, one decimal."""
    if largest <= 0:
        return 0.0
    return round(max(0.0, min(100.0, value / largest * 100)), 1)


def present(viz: Optional[dict]) -> Optional[dict]:
    """The shape with its geometry attached, as the template prints it.

    Additive: every key the factory's payload carries stays, so the JSON
    read of a built board still matches the factory's, and the template's
    widths, heights and figures sit beside them.

    One scale per visualisation, decided here, is what stops two bars on a
    wall being comparable-looking and not comparable.
    """
    if viz is None:
        return None
    kind = viz.get("kind")
    shown = dict(viz)

    if kind == "meter":
        segments = [dict(segment) for segment in viz.get("segments", [])]
        total = sum(segment["value"] for segment in segments)
        for index, segment in enumerate(segments):
            segment["pct"] = share_of(segment["value"], total)
            segment["figure"] = compact(segment["value"])
            # A sliver still has to be seen: the template gives any non-zero
            # segment the bar's own height as a minimum width.
            segment["sliver"] = segment["pct"] > 0
            segment["delay"] = f"{index * 0.1:.1f}" if index > 0 else ""
        shown["segments"] = segments

    elif kind == "pair":
        rows = [dict(row) for row in viz.get("rows", [])]
        largest = max([row["value"] for row in rows] + [0.0])
        for row in rows:
            row["pct"] = share_of(row["value"], largest)
            row["figure"] = compact(row["value"])
        shown["rows"] = rows

    elif kind == "bars":
        # The payload carries `current` per column rather than treating the
        # last as today: a weekday median has a "today" in the middle, and
        # labelling Sunday as today on a wall is the kind of quiet error
        # nobody reports.
        days = [dict(day) for day in viz.get("days", [])]
        largest = max([day["value"] for day in days] + [0.0])
        for index, day in enumerate(days):
            # The 2% floor keeps a small-but-real column visible, and must not
            # apply to a column of nothing: a stub with no figure over it reads
            # as a tiny amount rather than none.
            if day["value"] > 0:
                day["height"] = max(2.0, share_of(day["value"], largest))
                day["figure"] = compact(day["value"])
            else:
                day["height"] = 0.0
                day["figure"] = ""
            day["delay"] = f"{0.02 + index * 0.06:.2f}"
        shown["days"] = days

    elif kind == "table":
        aligns = list(viz.get("aligns") or [])
        columns = list(viz.get("columns", []))

        def align(index: int) -> str:
            return aligns[index] if index < len(aligns) and aligns[index] in ("left", "right") else "left"

        shown["header"] = [{"label": label, "align": align(index)} for index, label in enumerate(columns)]
        shown["body"] = [
            [{"text": cell, "align": align(index)} for index, cell in enumerate(row)]
            for row in viz.get("rows", [])
        ]

    return shown


def present_card(payload: Dict[str, Any]) -> Dict[str, Any]:
    """A validated card payload with its visualisation presented and the tag
    tone settled: a tile with nothing behind it never wears a condition
    colour, because there is no condition to report."""
    shown = dict(payload)
    shown["viz"] = present(payload.get("viz"))
    if payload.get("missing") and payload.get("tag"):
        shown["tag"] = {**payload["tag"], "tone": "nil"}
    return shown
