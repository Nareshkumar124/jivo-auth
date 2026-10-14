"""
Cards that carry no figure: the furniture an author arranges around the ones
that do. A port of the factory's ``board_builder/cards/chrome.py``.

WHY FURNITURE IS A CARD
------------------------
The three wall boards get their structure from a coloured rail down the left
of each band, naming the domain. A board composed on a free grid has no bands
to hang one off, and inventing a second concept ("sections", with their own
model, editor and rules) would double the size of the builder to reproduce a
stripe. A label is a card instead: it drags, drops, snaps and takes a hue
exactly like every other card.
"""

from __future__ import annotations

from . import viz
from .catalogue import CardContext, CardSpec, register

SECTION_LABEL_FALLBACK = "Untitled section"


def _section_label(context: CardContext) -> dict:
    """A hue and a word. The text is the placement's own title.

    No option of its own: the placement already carries a ``title`` that
    overrides a card's catalogue name, and a second text field meaning almost
    the same thing is how two labels end up disagreeing on one tile.
    """
    return viz.figure(
        value=" ",
        sub="",
        viz=viz.nothing(),
    )


register(
    CardSpec(
        key="section_label",
        title=SECTION_LABEL_FALLBACK,
        summary="A coloured rail with a name on it. Rename it after dropping it.",
        category="Layout",
        # Discloses nothing: it renders text the author typed and reads no
        # table. The ONE card in this catalogue that holds no feed.
        feed=None,
        columns=1,
        rows=1,
        accent="warehouse",
        needs_sap=False,
        needs_factory=False,
        build=_section_label,
        note=(
            "Structure only. Place one at the start of a row and give the "
            "cards beside it the same hue, and the row reads as a band of the "
            "wall boards."
        ),
    )
)
