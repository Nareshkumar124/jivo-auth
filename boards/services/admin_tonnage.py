"""
How the Admin board turns stock into tonnes: the factory's
``admin_board/tonnage.py``, which is itself the Logistics board's rule.

WHY THIS EXISTS
---------------
The first cut of the factory's board weighed finished goods as
``litres / 1000``, borrowing the Plant board's rule, and put 444.5 T on BH-BT
while the Logistics board two screens away said 439 T. Two things were
wrong, and both are fixed here the way the factory fixed them:

1. **Basis.** ``litres / 1000`` is a density of 1.0 and ignores the
   packaging. Finished goods are weighed by ``OITM.U_Gross_Weight``, the
   GROSS kg of one sales case, which is what ties to a weighbridge.
2. **Scope.** Item group 102 (finished goods) only. BH-BT also holds group
   105 and 106 stock that does not belong in a tile labelled "FG storage".

THE RULES, ALL THREE LOAD-BEARING
---------------------------------
* **Only piece units.** On a row stocked in KG or LTR the on-hand figure is
  already a mass or a volume; dividing it by a pack factor means nothing.
  Those rows are counted and disclosed, never weighed.
* **Gross case weight over pieces-per-case.** ``U_Gross_Weight`` is per CASE
  and ``on_hand`` is a piece count, so the case count is on-hand over
  ``SalFactor2``. Multiplying a case weight by a piece count overstates by
  the pack factor.
* **An unweighable row is disclosed, not zeroed.** No recorded weight returns
  None and counts in ``unweighed_items``; "unknown" added into a total as
  "nothing" is how a half-weighed warehouse renders as a confident figure.

The oil tanks are the exception: stocked in litres, so ``litres()`` takes the
volume as it stands and the board's 1,000 L = 1 T rule applies directly.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

#: Inventory units that are a mass or a volume rather than a count of things.
#: A deny-list, because the countable units are open-ended: SAP holds ``PCS``
#: for almost everything and ``DRM`` for the one drum SKU, and a drum is
#: still a thing you count.
MASS_OR_VOLUME_UOMS = frozenset(
    {
        "KG", "KGS", "KGM", "GM", "GMS", "GRM", "MT", "TON", "TONNE",
        "L", "LT", "LTR", "LTRS", "LITRE", "LITRES", "ML", "CC", "M3",
    }
)

#: Units that ARE a volume, as opposed to a mass. "Not a piece" is not the
#: same question as "is a volume": the tank farm holds a few kilograms of
#: rosemary and walnut beside the oil, and a kilogram is not a litre.
VOLUME_UOMS = frozenset({"L", "LT", "LTR", "LTRS", "LITRE", "LITRES", "ML", "CC", "M3"})

#: Finished goods. The only group a finished-goods tile may count.
FINISHED_ITEM_GROUP = 102


def is_piece_uom(uom: Optional[str]) -> bool:
    """True where an on-hand quantity in ``uom`` is a count of pieces.

    Blank answers False: SAP leaves ``InvntryUom`` empty often enough that
    guessing "piece" would fold unweighable rows into a total silently.
    """
    trimmed = (uom or "").strip()
    return bool(trimmed) and trimmed.upper() not in MASS_OR_VOLUME_UOMS


def row_kilograms(row: Dict[str, Any]) -> Optional[float]:
    """Kilograms held by one occupancy row, or None where it cannot be weighed.

    A pack factor of exactly 1 is not an error: the SKU is sold by the piece,
    so one piece is one case and the weight applies directly.
    """
    if not is_piece_uom(row.get("uom")):
        return None

    per_case = row.get("gross_weight_per_case")
    if per_case is None or per_case <= 0:
        return None

    pieces_per_case = row.get("pieces_per_box")
    if pieces_per_case is None or pieces_per_case <= 0:
        return None

    return (float(row.get("on_hand") or 0) * float(per_case)) / float(pieces_per_case)


def roll_up(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """A warehouse in tonnes, carrying its own incompleteness with it.

    Negative on-hand rows are included as they are: SAP does carry
    negatives, and clamping them would make this board disagree with the
    stock screen for a reason nobody could see.
    """
    kg = 0.0
    weighed = 0
    unweighed = 0
    non_piece = 0
    total = 0

    for row in rows:
        total += 1
        if not is_piece_uom(row.get("uom")):
            non_piece += 1
            continue

        row_kg = row_kilograms(row)
        if row_kg is None:
            unweighed += 1
            continue

        kg += row_kg
        weighed += 1

    return {
        "tonnes": round(kg / 1000, 2),
        "weighed_items": weighed,
        "unweighed_items": unweighed,
        "non_piece_items": non_piece,
        "item_count": total,
        "coverage": round(weighed / total, 3) if total else None,
    }


def litres(rows: Iterable[Dict[str, Any]]) -> float:
    """Total litres across rows, for stock that IS a volume.

    * A volume unit: the on-hand figure is already litres.
    * A piece unit: through ``litres_per_piece`` (a 200 L drum against a
      15 L tin); a piece row with no volume recorded contributes nothing.
    * A mass unit: skipped. Kilograms are not litres.
    """
    total = 0.0
    for row in rows:
        on_hand = float(row.get("on_hand") or 0)
        uom = (row.get("uom") or "").strip().upper()

        if is_piece_uom(uom):
            per_piece = row.get("litres_per_piece")
            total += on_hand * float(per_piece) if per_piece else 0.0
        elif uom in VOLUME_UOMS:
            total += on_hand
        # else: a mass, deliberately not counted.
    return total
