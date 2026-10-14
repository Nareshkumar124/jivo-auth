"""
Boards somebody composed in the factory's dashboard builder and flagged onto
the wall rotation, composed here from the factory's own database.

A port of the factory's ``board_builder/services.py`` (the read) and the
rotation half of its ``views.CarouselBoardsAPI`` (the list). The cards
themselves are the package beside this module, ``built_cards``.

WHAT IS READ
------------
``board_builder_customboard`` is the board: its grid, its palette and its
sharing state. ``board_builder_boardplacement`` holds the cards on it, each
as a catalogue key and an ORIGIN. The card's width and height are not in
the row on purpose: they are a property of the card and are looked up here,
so the catalogue and a saved board cannot disagree and render as two cards
on one square with no error anywhere.

FOUR WAYS A CARD CAN BE ABSENT
------------------------------
The factory keeps four lists and so does this payload, because they send a
reader to four different people:

    meta.degraded   the source did not answer (named with its reason)
    meta.withheld   always empty here: the carousel permission shows every tile
    meta.retired    the card was removed from the catalogue
    meta.misplaced  the card no longer fits where it was put

The last two only ever happen across a deploy. Both are reported and the
card is dropped, because the alternative, rendering it anyway, is two cards
on one square with nothing anywhere saying so. The board still composes and
the rest of it draws.

WHO MAY SEE WHAT
----------------
The factory decides per reader: a published board opens for its audience,
and each card is withheld unless the reader holds its feed. There is no
reader here. A screen holding the carousel permission shows every published
wall board flagged onto the rotation, with every tile on it; private boards
are never listed and never composed, because a half-built wall is not a
fact about the factory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .. import formatting
from ..sections import SectionBuilder
from .built_cards import catalogue, viz
from .built_cards.catalogue import CardContext, CardSpec
from .built_cards.constants import VISIBILITY_PUBLISHED

#: How often a screen re-reads a built board, in seconds. Sixty, matching
#: the factory's ``BUILT_BOARD_REFRESH_MS`` and the other wall boards. Not
#: configurable per board on purpose: an author choosing a five-second
#: refresh for a card that scans a large table is a decision whose cost
#: lands on a shared Postgres box, and they have no way to see that cost.
REFRESH_SECONDS = 60

#: The rotation: published, active, WALL-mode boards flagged onto it, by
#: name. The factory lists these per reader and drops any the screen cannot
#: read a single card of; here the carousel permission reads everything, so
#: the list is the whole set.
CAROUSEL_SQL = """
    SELECT b.slug, b.name
    FROM board_builder_customboard AS b
    JOIN company_company AS c ON c.id = b.company_id
    WHERE c.code = %s
      AND b.is_active = TRUE
      AND b.visibility = %s
      AND b.mode = 'WALL'
      AND b.in_carousel = TRUE
    ORDER BY b.name, b.id
"""

#: One board by slug. Only a PUBLISHED one: a private draft does not exist
#: for anybody but its author, and the author is not here.
BOARD_SQL = """
    SELECT
        b.id, b.slug, b.name, b.description, b.mode,
        b."columns", b."rows", b.surface, b.density, b.accent,
        b.show_heading, b.visibility, b.in_carousel
    FROM board_builder_customboard AS b
    JOIN company_company AS c ON c.id = b.company_id
    WHERE c.code = %s
      AND b.slug = %s
      AND b.is_active = TRUE
      AND b.visibility = %s
"""

#: The cards on a board, in reading order: row then column, so the first
#: card placed wins a collision. Arbitrary, but deterministic, which is what
#: matters: the alternative is a board that drops a different card depending
#: on row ordering in the database. ``column`` and ``row`` are quoted because
#: PostgreSQL reserves both words.
PLACEMENTS_SQL = """
    SELECT p.id, p.card_key, p."column", p."row", p.title, p.accent, p.options
    FROM board_builder_boardplacement AS p
    WHERE p.board_id = %s
    ORDER BY p."row", p."column", p.id
"""


def list_carousel_boards(postgres, company_code: str) -> List[Dict[str, Any]]:
    """``[{"slug", "name"}]`` for the published wall boards on the rotation."""
    rows = postgres.rows(CAROUSEL_SQL, [company_code, VISIBILITY_PUBLISHED])
    return [{"slug": str(row["slug"]), "name": str(row["name"])} for row in rows]


@dataclass
class _Placed:
    """A placement that survived the geometry check, with its spec attached."""

    placement: Dict[str, Any]
    spec: CardSpec


def _options(raw: Any) -> Dict[str, Any]:
    """The placement's JSON options as a dict, whatever the driver returned.

    psycopg hands ``jsonb`` back decoded; a fake, or an older driver, may
    hand back the text. Anything that is not an object is treated as no
    options rather than refused, because an option is chrome and a board
    must open.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


class BuiltBoardService(SectionBuilder):
    """Build one built board's payload: the board row, every card on it, and
    a named reason for each one that is not there."""

    REFRESH_SECONDS = REFRESH_SECONDS

    def __init__(
        self,
        company_code: str,
        *,
        slug: str,
        postgres,
        hana_for: Callable[[str], Any],
        today=None,
    ):
        self.company_code = company_code
        self.slug = slug
        self.postgres = postgres
        self.hana = hana_for(company_code)
        # "Now" is the factory's clock. A board composed for a given day
        # (tests, replays) keeps the time of day, so a card that subtracts
        # from the instant now reads the same window it would have then.
        self.now: datetime = formatting.now()
        if today is not None and today != self.now.date():
            self.now = datetime.combine(today, self.now.timetz())
        self.today = self.now.date()
        self._init_sections()
        self._retired: List[str] = []
        self._misplaced: List[str] = []

    # -- the board ----------------------------------------------------------

    def _board_row(self) -> Optional[Dict[str, Any]]:
        """The board, or ``None`` when no published board carries this slug."""
        row = self.postgres.one(BOARD_SQL, [self.company_code, self.slug, VISIBILITY_PUBLISHED])
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "slug": str(row["slug"]),
            "name": str(row["name"]),
            "description": str(row.get("description") or ""),
            "mode": str(row["mode"]),
            "columns": int(row["columns"]),
            "rows": int(row["rows"]),
            "surface": str(row["surface"]),
            "density": str(row["density"]),
            "accent": str(row["accent"]),
            "show_heading": bool(row["show_heading"]),
            "visibility": str(row["visibility"]),
            "in_carousel": bool(row["in_carousel"]),
        }

    def _placements(self, board_id: int) -> List[Dict[str, Any]]:
        rows = self.postgres.rows(PLACEMENTS_SQL, [board_id])
        return [
            {
                "id": int(row["id"]),
                "card_key": str(row["card_key"]),
                "column": int(row["column"]),
                "row": int(row["row"]),
                "title": str(row.get("title") or ""),
                "accent": str(row.get("accent") or ""),
                "options": _options(row.get("options")),
            }
            for row in rows
        ]

    # -- geometry -----------------------------------------------------------

    def _fits(self, board: Dict[str, Any], placement: Dict[str, Any], spec: CardSpec, taken: Set[Tuple[int, int]]) -> bool:
        """Whether this card's footprint is inside the grid and unoccupied.

        Checked on READ and not only on write, because a card's footprint is
        catalogue data: it can change under a saved board without anything
        touching the board's own rows. The check is cheap, a board holds a
        couple of dozen cells, and it is the difference between an author
        being told to move a tile and a reader seeing two stacked on one.
        """
        if placement["column"] + spec.columns > board["columns"]:
            return False
        if placement["row"] + spec.rows > board["rows"]:
            return False
        cells = {
            (placement["column"] + dx, placement["row"] + dy)
            for dx in range(spec.columns)
            for dy in range(spec.rows)
        }
        if cells & taken:
            return False
        taken |= cells
        return True

    def _resolve(self, board: Dict[str, Any], placements: List[Dict[str, Any]]) -> List[_Placed]:
        """The placements that can actually be drawn, in reading order."""
        placed: List[_Placed] = []
        taken: Set[Tuple[int, int]] = set()

        for placement in placements:
            spec = catalogue.get(placement["card_key"])
            if spec is None:
                self._retired.append(placement["card_key"])
                continue
            if not self._fits(board, placement, spec, taken):
                self._misplaced.append(placement["card_key"])
                continue
            placed.append(_Placed(placement=placement, spec=spec))

        return placed

    # -- the read -----------------------------------------------------------

    def _card(self, board: Dict[str, Any], entry: _Placed) -> Dict[str, Any]:
        """One card's payload, or the reason it has none.

        The build is wrapped by ``SectionBuilder.section``, so a card that
        raises costs its own tile and never the board: a wall with eleven
        good tiles and one that says why it is dark is worth far more than
        a 503 nobody is standing there to read.
        """
        placement, spec = entry.placement, entry.spec
        context = CardContext(
            company_code=self.company_code,
            options=spec.coerce_options(placement["options"]),
            postgres=self.postgres,
            hana=self.hana,
            today=self.today,
            now=self.now,
        )

        payload = self.section(
            spec.key,
            lambda: viz.present_card(viz.validate(spec.build(context))),
            needs_sap=spec.needs_sap,
            needs_factory=spec.needs_factory,
        )

        return {
            "id": placement["id"],
            "card_key": spec.key,
            "title": placement["title"] or spec.title,
            "accent": placement["accent"] or board["accent"],
            "column": placement["column"],
            "row": placement["row"],
            "columns": spec.columns,
            "rows": spec.rows,
            "category": spec.category,
            "note": spec.note,
            # None means the section builder did not run it, or it failed.
            # The reason is in meta, named by `spec.key`.
            "payload": payload,
        }

    def build(self) -> Dict[str, Any]:
        board = self.section("board", self._board_row, needs_sap=False)
        cards: List[Dict[str, Any]] = []

        if board is None:
            if "board" not in self._degraded:
                # Not a read failure: the database answered and there is no
                # such published board. The page decides what to show; a
                # slug that was unpublished or retired is not a 404 here,
                # because the rotation was listed a minute ago.
                self.warn(
                    f'No published board is slugged "{self.slug}" for '
                    f"{self.company_code}: it may have been unpublished or "
                    "taken off the rotation."
                )
        else:
            placements = self.section("placements", lambda: self._placements(board["id"]), needs_sap=False)
            if placements is not None:
                cards = [self._card(board, entry) for entry in self._resolve(board, placements)]

        if self._retired:
            self.warn(
                "This board holds "
                f"{len(self._retired)} card(s) that no longer exist. Open it in "
                "the builder to clear them."
            )
        if self._misplaced:
            self.warn(
                f"{len(self._misplaced)} card(s) no longer fit where they were "
                "placed and are not shown. Open it in the builder to move them."
            )

        meta = self.section_meta()
        meta["retired"] = list(self._retired)
        meta["misplaced"] = list(self._misplaced)
        meta["generated_at"] = formatting.now().isoformat()
        meta["refresh_seconds"] = self.REFRESH_SECONDS
        meta["slug"] = self.slug

        payload_board = None
        if board is not None:
            payload_board = {key: value for key, value in board.items() if key != "id"}

        return {
            "board": payload_board,
            "cards": cards,
            "meta": meta,
        }
