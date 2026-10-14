"""
The limits a composed board is built inside, and why each one is where it is.

A port of the factory's ``board_builder/constants.py``. The numbers are the
factory's: a board is read from across a room, and a grid past a certain
density stops being a board and becomes a spreadsheet nobody can read at four
metres. They are checked here on READ (the geometry check in
``boards.services.built``), never changed: a board is saved by the factory's
builder, and this service only draws what it was given.

EVERY NUMBER HERE IS A LEGIBILITY LIMIT, NOT A STORAGE ONE.
"""

from __future__ import annotations

#: The widest a board may be. Six is the point at which a tile on a
#: 1920-wide wall falls under about 300px, where the four-row tile starts
#: eliding its own figure.
GRID_MAX_COLUMNS = 6

#: A one-column board is a legitimate thing: a single big number on a
#: screen by a machine.
GRID_MIN_COLUMNS = 1

#: How tall a WALL board may be. A wall board does not scroll, and 1080
#: minus the topbar divided by five is about 190px a row, the measured floor
#: for a tile that still shows a visualisation under its figure.
WALL_MAX_ROWS = 5

#: A page board scrolls inside the app shell, so height costs nothing but
#: patience. Only reachable here by a board's own address, never on the
#: rotation.
PAGE_MAX_ROWS = 12

GRID_MIN_ROWS = 1

#: What a new board starts as: the shape of the boards that already exist.
DEFAULT_COLUMNS = 4
DEFAULT_ROWS = 3

#: The board modes, as the factory stores them.
MODE_WALL = "WALL"
MODE_PAGE = "PAGE"

#: The visibility a board must hold to be drawn here at all.
VISIBILITY_PUBLISHED = "PUBLISHED"


def max_rows_for(mode: str) -> int:
    """The row ceiling for one board mode."""
    return PAGE_MAX_ROWS if mode == MODE_PAGE else WALL_MAX_ROWS


#: The hues a board or a card may wear.
#:
#: A CLOSED LIST, AND THAT IS THE POINT. The wall boards' rule is that colour
#: carries meaning and nothing else: each band owns one hue, every bar inside
#: it is a tint of that same hue, and green/amber/red are reserved for
#: CONDITION. The author picks an identity, not a colour, and the stylesheet
#: (``ops-board.css`` plus ``built.css``) resolves each to the same custom
#: properties the fixed boards paint with, which is what keeps a card built
#: here and the same card on the Plant board from being two teals.
CARD_ACCENTS: tuple[str, ...] = (
    "warehouse",
    "dispatch",
    "transport",
    "purchase",
    "store",
    "production",
    "shifting",
)

DEFAULT_ACCENT = "warehouse"

#: Light is the default because the three fixed wall boards are light, and a
#: built board beside them in one rotation should not be the odd one out
#: unless somebody chose that. Dark is for the screens by the lines.
BOARD_SURFACES: tuple[str, ...] = ("light", "dark")

DEFAULT_SURFACE = "light"

#: How much air a tile gets.
BOARD_DENSITIES: tuple[str, ...] = ("compact", "normal", "roomy")

DEFAULT_DENSITY = "normal"
