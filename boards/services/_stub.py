"""
A board that has not been ported yet. Each real service replaces one of
these; until then the carousel still rotates and the tile says why it is
empty rather than showing a zero.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .. import formatting
from ..sections import SectionBuilder


class StubBoardService(SectionBuilder):
    #: The reason every section gives.
    reason = "This board has not been ported to Jivo Auth yet."
    #: The sections the template expects, all null.
    sections = ("board",)

    def __init__(
        self,
        company_code: str,
        *,
        postgres=None,
        hana_for: Optional[Callable[[str], Any]] = None,
        today=None,
        **extra,
    ):
        self.company_code = company_code
        self.postgres = postgres
        self.hana_for = hana_for
        self.today = today or formatting.today()
        self.extra = extra
        self._init_sections()

    def build(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for name in self.sections:
            payload[name] = self.section(name, self._not_ported, needs_sap=False, needs_factory=False)
        payload["meta"] = self.section_meta()
        return payload

    def _not_ported(self):
        raise NotImplementedError(self.reason)
