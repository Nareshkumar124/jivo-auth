"""
The register of cards, and the contract a card signs.

A port of the factory's ``board_builder/catalogue.py``. The keys are the
stored contract: every placement the factory saves holds one, so a key here
must match the factory's exactly or a board arrives as a grid of "retired"
cards. Retire a card by leaving it registered and hidden, never by renaming.

WHAT A CARD IS GIVEN HERE
-------------------------
In the factory a card runs inside Django with the ORM. Here it is handed the
board's own sources instead (:class:`CardContext`): the factory's PostgreSQL
through ``context.postgres.rows(sql, params)`` with ``%s`` placeholders, and
SAP through ``context.hana.rows(sql, params)`` with ``?`` placeholders and
every table qualified with ``context.hana.table("OINM")``. The factory's
``user`` is gone: this service has no per-reader audience check (the carousel
permission shows every tile), so ``feed`` is kept on the spec as a record of
what the factory gates the card on, and nothing reads it.

WHAT A CARD MUST NOT DO
-----------------------
Raise. A card that throws is caught by ``SectionBuilder.section`` and
reported as degraded, which is right for a SAP timeout and wrong for a typo.
Cards should return :func:`viz.missing` for every condition they can
foresee, and keep the exception path for the ones they cannot.

Be slow. A board is a dozen cards behind one request, and the factory's
Postgres box serves fourteen databases. A card doing anything heavier than an
indexed aggregate over a few thousand rows should read a rollup.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterator, Literal, Optional, Tuple

from .constants import CARD_ACCENTS, DEFAULT_ACCENT, GRID_MAX_COLUMNS, WALL_MAX_ROWS


@dataclass(frozen=True)
class CardOption:
    """One setting an author may change per placement.

    Kept deliberately thin: an integer, a choice or a switch. An option exists
    so the same card can be dropped twice on one board reading two windows;
    it is not a query builder.
    """

    key: str
    label: str
    kind: Literal["int", "choice", "bool"]
    default: Any
    #: ``int``: inclusive bounds. Ignored for the other kinds.
    minimum: int = 0
    maximum: int = 0
    #: ``choice``: the allowed ``(value, label)`` pairs.
    choices: Tuple[Tuple[str, str], ...] = ()
    help: str = ""

    def coerce(self, raw: Any) -> Any:
        """This option's value from whatever the author's browser saved.

        Falls back to the default rather than raising. An option is chrome: a
        board that will not open because somebody's saved dropdown value was
        retired in a later release is a worse outcome than a board that
        quietly reverts to the default.
        """
        if raw is None:
            return self.default
        if self.kind == "int":
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return self.default
            if self.minimum or self.maximum:
                value = max(self.minimum, min(self.maximum, value))
            return value
        if self.kind == "bool":
            return bool(raw)
        allowed = {value for value, _ in self.choices}
        return raw if raw in allowed else self.default


@dataclass(frozen=True)
class CardContext:
    """Everything a card is given, and the whole of it.

    A card receives no request and no view. That is a boundary rather than
    an oversight: permissions were decided once, at the door of the carousel,
    and a card that could re-decide them would be a second place for the
    answer to live.
    """

    company_code: str
    #: The placement's options, already coerced against the card's declarations.
    options: Dict[str, Any]
    #: The factory database: ``.rows(sql, params)`` / ``.one()`` / ``.value()``.
    postgres: Any
    #: The SAP reader for ``company_code``: ``.rows(sql, params)`` / ``.table()``.
    hana: Any
    #: The factory's local date and time the board is being composed for.
    today: date
    now: datetime

    def option(self, key: str, fallback: Any = None) -> Any:
        return self.options.get(key, fallback)


#: What a card's build function looks like.
CardBuilder = Callable[[CardContext], dict]


@dataclass(frozen=True)
class CardSpec:
    """One entry in the palette."""

    key: str
    title: str
    #: One line, in the vocabulary of the person who would drag it.
    summary: str
    category: str
    build: CardBuilder

    #: The feed right the FACTORY gates this card on. Recorded, not enforced:
    #: see the module docstring. ``None`` means the card discloses nothing.
    feed: Optional[str] = None

    #: The fixed footprint, in grid cells. A property of the card, never of
    #: the placement, so the catalogue and a saved board cannot disagree.
    columns: int = 1
    rows: int = 1

    accent: str = DEFAULT_ACCENT
    options: Tuple[CardOption, ...] = ()

    #: Whether this card talks to SAP. Cards that do are skipped wholesale
    #: once one of them reports the connection is gone (the latch in
    #: ``boards.sections``). Defaults to False because everything in this
    #: catalogue today is PostgreSQL, and a card wrongly marked as needing
    #: SAP disappears during an outage it was immune to.
    needs_sap: bool = False
    #: Whether this card reads the factory database. Off for furniture.
    needs_factory: bool = True

    #: Longer prose for the tile's caption: what the figure means, which
    #: definition it follows.
    note: str = ""

    def __post_init__(self) -> None:
        # Validated at import, so a mistake fails the process rather than
        # one tile.
        if self.accent not in CARD_ACCENTS:
            raise ValueError(
                f"Card {self.key!r} has accent {self.accent!r}; "
                f"choose one of {', '.join(CARD_ACCENTS)}."
            )
        if not 1 <= self.columns <= GRID_MAX_COLUMNS:
            raise ValueError(
                f"Card {self.key!r} is {self.columns} columns wide; "
                f"the widest board is {GRID_MAX_COLUMNS}."
            )
        if not 1 <= self.rows <= WALL_MAX_ROWS:
            raise ValueError(
                f"Card {self.key!r} is {self.rows} rows tall; a wall board is "
                f"at most {WALL_MAX_ROWS}, and a card that cannot fit on a wall "
                "cannot be placed on one."
            )

    def coerce_options(self, raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The options this card will actually run with.

        Every declared option gets a value and nothing undeclared survives: a
        stale key left behind by an earlier version of the card is dropped
        rather than passed through to confuse it.
        """
        raw = raw or {}
        return {option.key: option.coerce(raw.get(option.key)) for option in self.options}


#: The register. Keys are part of the stored contract.
_CARDS: Dict[str, CardSpec] = {}


def register(spec: CardSpec) -> CardSpec:
    """Add one card to the palette. Returns it, so it can decorate a module."""
    if spec.key in _CARDS:
        raise ValueError(
            f"Two cards are registered as {spec.key!r}. A card key is stored on "
            "every placement that uses it, so it must be unique and stable."
        )
    _CARDS[spec.key] = spec
    return spec


def get(key: str) -> Optional[CardSpec]:
    """One card, or ``None`` if a board holds a key no longer in the code."""
    _load()
    return _CARDS.get(key)


def require(key: str) -> CardSpec:
    """One card, loudly. For paths where an unknown key is a bug, not history."""
    spec = get(key)
    if spec is None:
        raise KeyError(
            f"Unknown card {key!r}. Registered: {', '.join(sorted(_CARDS)) or 'none'}."
        )
    return spec


def all_cards() -> Tuple[CardSpec, ...]:
    """Every registered card, in palette order: category, then title."""
    _load()
    return tuple(sorted(_CARDS.values(), key=lambda spec: (spec.category, spec.title)))


def categories() -> Tuple[str, ...]:
    return tuple(dict.fromkeys(spec.category for spec in all_cards()))


@contextmanager
def temporarily(spec: CardSpec) -> Iterator[CardSpec]:
    """Register a card for the length of a ``with`` block. For tests that
    need a card the catalogue does not have: one that raises, one that reads
    SAP."""
    _load()
    register(spec)
    try:
        yield spec
    finally:
        _CARDS.pop(spec.key, None)


_loaded = False


def _load() -> None:
    """Import the card modules once, on first use.

    Lazy, as in the factory: the card modules import this module to
    register themselves, so importing them from here at module scope would
    be a cycle.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import load

    load()
