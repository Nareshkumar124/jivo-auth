"""
Assembling a board out of sections that can each fail on their own.

The factory's boards keep one rule and these boards keep it too: a section
that cannot be read is reported INSIDE a 200, named in ``meta.degraded``
with its key set to null, and the screen renders the rest. A wall board has
nobody standing at it; an error page on a factory TV stays up until someone
notices.

Two latches, so one outage costs one timeout rather than one per section: a
SAP connection failure marks SAP down for the rest of the build, and a
factory database failure does the same for the sections that read it.

``meta.reasons`` names why each degraded section is missing. The factory's
boards only list the names; here the reason is kept because the most likely
cause on this service is a setting that isn't filled in yet, and a tile
that says "SAP is not configured" sends somebody to the right file.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from .sources import (
    FactoryNotConfigured,
    FactoryUnavailable,
    SAPConnectionError,
    SAPDataError,
    SAPNotConfigured,
)

logger = logging.getLogger(__name__)


class SectionBuilder:
    """Mixin for a board service: run sections, record why each is absent.

    Subclasses call ``self._init_sections()`` first, then ``self.section()``
    per band, and put ``self.section_meta()`` into the payload's ``meta``.
    """

    def _init_sections(self) -> None:
        self._degraded: List[str] = []
        self._reasons: Dict[str, str] = {}
        self._warnings: List[str] = []
        self._sap_down: Optional[str] = None
        self._factory_down: Optional[str] = None
        # A latch tripped by a missing setting rather than an outage: the
        # chip then says so, instead of "did not answer".
        self._sap_unconfigured = False
        self._factory_unconfigured = False

    def section(
        self,
        name: str,
        build: Callable[[], Any],
        *,
        needs_sap: bool = True,
        needs_factory: bool = True,
    ) -> Any:
        """Build one section, or record why it is missing and return None.

        A missing section is always ``None`` in the payload: never omitted,
        never ``{}``, never zero. A zero nobody can explain gets believed for
        a week and then ignored forever.
        """
        if needs_sap and self._sap_down:
            self._degrade(name, self._sap_down)
            return None

        if needs_factory and self._factory_down:
            self._degrade(name, self._factory_down)
            return None

        try:
            return build()
        except SAPConnectionError as exc:
            # Latch: one outage costs one timeout, not one per section.
            self._sap_down = str(exc)
            self._sap_unconfigured = isinstance(exc, SAPNotConfigured)
            logger.warning("Board section %r: %s", name, exc)
            self._degrade(name, str(exc))
        except SAPDataError as exc:
            logger.warning("Board section %r: %s", name, exc)
            self._degrade(name, str(exc))
        except FactoryUnavailable as exc:
            self._factory_down = str(exc)
            self._factory_unconfigured = isinstance(exc, FactoryNotConfigured)
            logger.warning("Board section %r: %s", name, exc)
            self._degrade(name, str(exc))
        except Exception as exc:  # noqa: BLE001 - a board must not 500 over one tile
            logger.exception("Board section %r failed", name)
            self._degrade(name, f"{type(exc).__name__}: {exc}")

        return None

    def _degrade(self, name: str, reason: str) -> None:
        if name not in self._degraded:
            self._degraded.append(name)
        self._reasons[name] = reason

    def warn(self, message: str) -> None:
        """A prose caveat for a section that DID render."""
        if message not in self._warnings:
            self._warnings.append(message)

    def section_meta(self) -> Dict[str, Any]:
        """The lists, for splicing into the payload's ``meta``."""
        if self._sap_down:
            self.warn(
                "SAP is not configured on this service. The tiles that read it show nothing."
                if self._sap_unconfigured
                else "SAP did not answer. The tiles that read it show nothing "
                "rather than a stale figure."
            )
        if self._factory_down:
            self.warn(
                "The factory database is not configured on this service. The tiles "
                "that read it show nothing."
                if self._factory_unconfigured
                else "The factory database did not answer. The tiles that read it "
                "show nothing rather than a stale figure."
            )
        return {
            "degraded": list(self._degraded),
            "reasons": dict(self._reasons),
            "warnings": list(self._warnings),
            # Kept for the templates ported from the factory, which read it.
            # Nothing is withheld here: holding the carousel permission shows
            # every tile.
            "withheld": [],
        }
