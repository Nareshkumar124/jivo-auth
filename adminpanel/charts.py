"""
Chart geometry for the dashboard, computed here so the templates only lay
out plain HTML (which stays legible at any width, unlike a scaled SVG).
"""

import math
from dataclasses import dataclass, field


@dataclass
class Series:
    key: str
    label: str
    counts: dict = field(default_factory=dict)   # date -> value


def nice_step(value, intervals=4):
    """A 1/2/5 x 10^n step splitting 0..value into about `intervals` parts."""

    raw = max(value, 1) / intervals

    if raw <= 1:
        return 1

    magnitude = 10 ** math.floor(math.log10(raw))

    for multiple in (1, 2, 5, 10):
        if multiple * magnitude >= raw:
            return multiple * magnitude

    return 10 * magnitude


def _axis(value, intervals):
    step = nice_step(value, intervals)
    top = max(step * math.ceil(max(value, 1) / step), step)

    return top, list(range(0, top + 1, step))


def axis(value):
    """
    Whole-number ticks from 0 to a clean top at or above value: the lowest
    top among 3-5 intervals, then the tick count closest to five.
    """

    return min(
        (_axis(value, intervals) for intervals in (3, 4, 5)),
        key=lambda result: (result[0], abs(len(result[1]) - 5)),
    )


def nice_ceiling(value):
    return axis(value)[0]


def column_chart(days, series, today):
    """Stacked columns, one per day; series stack bottom to top in order."""

    totals = [
        sum(s.counts.get(day, 0) for s in series)
        for day in days
    ]

    top, ticks = axis(max(totals, default=0))
    last = len(days) - 1
    columns = []

    for index, (day, total) in enumerate(zip(days, totals)):
        columns.append(
            {
                "date": day,
                "label": f"{day:%a} {day:%b} {day.day}",
                "short": f"{day.day}",
                "is_today": day == today,
                "total": total,
                "height": total / top * 100,
                "segments": [
                    {
                        "key": s.key,
                        "label": s.label,
                        "value": s.counts.get(day, 0),
                    }
                    for s in series
                    if s.counts.get(day, 0)
                ],
                "values": [
                    {"key": s.key, "label": s.label, "value": s.counts.get(day, 0)}
                    for s in series
                ],
                # Keeps edge tooltips inside the card.
                "align": "start" if index < 3 else "end" if index > last - 3 else "",
            }
        )

    return {
        "columns": columns,
        "ticks": [
            {"value": tick, "position": tick / top * 100}
            for tick in ticks
        ],
        "series": [{"key": s.key, "label": s.label} for s in series],
        "total": sum(totals),
    }


def bar_list(rows, limit=6):
    """Horizontal bars for (label, value, url) rows, largest first."""

    rows = sorted(rows, key=lambda row: row[1], reverse=True)[:limit]
    top = nice_ceiling(max((value for _, value, _ in rows), default=0))

    return [
        {
            "label": label,
            "value": value,
            "url": url,
            "width": value / top * 100,
        }
        for label, value, url in rows
    ]
