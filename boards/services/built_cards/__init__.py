"""
Every card in the palette, one subject per module. A port of the factory's
``board_builder/cards/``, reading the factory's databases through the
board's own sources instead of the ORM.

HOW TO ADD ONE
--------------
Put it in the module its subject belongs to (or start a new one and name it
in :func:`load`), write a build function taking a
:class:`~.catalogue.CardContext` and returning a shape from :mod:`.viz`, and
register it. The template in ``boards/fragments/built.html`` already knows
every shape, so there is no markup to write.

Keep the key identical to the factory's: it is the stored contract on every
placement the factory's builder saves.

WHY THE IMPORTS ARE IN A FUNCTION
----------------------------------
The card modules import :mod:`.catalogue` to register themselves, and the
catalogue imports this package to load them. :func:`load` is called on the
catalogue's first use, by which point both modules exist.
"""

from __future__ import annotations


def load() -> None:
    """Import every card module once. Idempotent by Python's module cache."""
    from . import chrome, dispatch, gate, production  # noqa: F401
