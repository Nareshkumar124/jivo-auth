"""
Fakes for board tests. No test may reach the factory database or SAP: the
services take their sources as arguments, and these answer SQL by matching
a substring (or compiled regex) of the statement.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterable, Optional, Sequence

from .sources import SAPConnectionError


class FakePostgres:
    """A factory database that answers from a list of (needle, rows).

    ``needle`` is a substring of the SQL, or a compiled regex searched in
    it. The first match answers; an unmatched statement returns no rows.
    Every call is recorded in ``calls``.
    """

    def __init__(self, answers: Optional[Iterable[tuple]] = None):
        self.answers = list(answers or [])
        self.calls: list[tuple[str, Any]] = []

    def add(self, needle, rows: Sequence[dict]) -> "FakePostgres":
        self.answers.append((needle, list(rows)))
        return self

    def rows(self, sql: str, params: Optional[Sequence[Any]] = None) -> list[dict]:
        self.calls.append((sql, params))
        for needle, rows in self.answers:
            if isinstance(needle, re.Pattern):
                if needle.search(sql):
                    return copy.deepcopy(rows)
            elif needle in sql:
                return copy.deepcopy(rows)
        return []

    def one(self, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[dict]:
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def value(self, sql: str, params: Optional[Sequence[Any]] = None, default: Any = None) -> Any:
        row = self.one(sql, params)
        if not row:
            return default
        value = next(iter(row.values()))
        return default if value is None else value


class FakeHana(FakePostgres):
    """A SAP schema that answers the same way. ``table()`` quotes like HANA."""

    def __init__(self, answers=None, company_code: str = "JIVO_OIL", schema: str = "TEST_SCHEMA"):
        super().__init__(answers)
        self.company_code = company_code
        self.schema = schema

    def table(self, name: str) -> str:
        return f'"{self.schema}"."{name}"'


class HanaDown:
    """SAP that never answers: every read raises ``SAPConnectionError``."""

    def __init__(self, company_code: str = "JIVO_OIL", reason: str = "SAP HANA could not be reached."):
        self.company_code = company_code
        self.schema = None
        self.reason = reason

    def table(self, name: str) -> str:
        raise SAPConnectionError(self.reason)

    def rows(self, sql: str, params=None):
        raise SAPConnectionError(self.reason)

    def one(self, sql: str, params=None):
        raise SAPConnectionError(self.reason)


def hana_factory(*readers):
    """``hana_for`` that hands out the given fakes by company code."""
    by_company = {reader.company_code: reader for reader in readers}

    def hana_for(company_code: str):
        try:
            return by_company[company_code]
        except KeyError:
            return HanaDown(company_code, f"No SAP schema is configured for {company_code}.")

    return hana_for
