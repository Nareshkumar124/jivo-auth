"""
SAP HANA reads for the boards' SAP tiles (output, storage, dispatch, plans).

Optional: without ``FACTORY_HANA_HOST`` every SAP read raises
``SAPConnectionError`` with a reason that names the setting, so the tiles
say "SAP is not configured" and the rest of the board shows.

One connection per statement, closed always, exactly as the factory's own
readers do. Queries use hdbcli's ``?`` placeholders and must qualify every
table with the company's schema: ``hana.table("OINM")``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from django.conf import settings

logger = logging.getLogger(__name__)


class SAPConnectionError(Exception):
    """SAP is not configured, not reachable, or didn't answer in time."""


class SAPNotConfigured(SAPConnectionError):
    """SAP, or the company's schema, isn't set up on this service."""


class SAPDataError(Exception):
    """SAP answered and rejected the statement."""


#: Litres in one transacted piece, gated on the item being a litre item at
#: all. Expects ``M`` = OITM. The same expression the factory's boards use:
#: ``SalPackUn`` is populated for the whole item master, so without the
#: ``U_IsLitre`` test a movement of preforms would count as litres of oil.
LITRES_PER_UNIT = """
    CASE
        WHEN UPPER(IFNULL(M."U_IsLitre", 'N')) = 'Y' THEN IFNULL(M."SalPackUn", 0)
        ELSE 0
    END
"""


def configured() -> bool:
    hana = settings.FACTORY_HANA
    return bool(hana["HOST"] and hana["USER"])


def schema_for(company_code: str) -> Optional[str]:
    return settings.FACTORY_HANA["SCHEMAS"].get(company_code)


class Hana:
    """The SAP reads for one company (one HANA schema)."""

    def __init__(self, company_code: str):
        self.company_code = company_code
        self.schema = schema_for(company_code)

    def table(self, name: str) -> str:
        """``"SCHEMA"."NAME"``, quoted the way HANA wants it."""
        self._check()
        return f'"{self.schema}"."{name}"'

    def _check(self) -> None:
        """Raise the reason SAP can't be read, naming the setting to fill."""
        if not configured():
            raise SAPNotConfigured(
                "SAP HANA is not configured on this service "
                "(FACTORY_HANA_HOST, FACTORY_HANA_USER, FACTORY_HANA_PASSWORD)."
            )
        if not self.schema:
            raise SAPNotConfigured(self._no_schema())

    def rows(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        """Run one statement and return dicts keyed by column name."""
        self._check()

        from hdbcli import dbapi

        hana = settings.FACTORY_HANA
        connection = None
        cursor = None

        try:
            connection = dbapi.connect(
                address=hana["HOST"],
                port=int(hana["PORT"]),
                user=hana["USER"],
                password=hana["PASSWORD"],
                connectTimeout=int(hana["CONNECT_TIMEOUT"]) * 1000,
                communicationTimeout=int(hana["COMMUNICATION_TIMEOUT"]) * 1000,
            )
        except dbapi.Error as exc:
            logger.warning("SAP HANA connection failed: %s", exc)
            raise SAPConnectionError("SAP HANA could not be reached.") from exc

        try:
            cursor = connection.cursor()
            cursor.execute(sql, list(params or []))
            columns = [column[0] for column in cursor.description or []]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except dbapi.Error as exc:
            logger.warning("SAP HANA query failed: %s", exc)
            raise SAPDataError(f"SAP rejected this query: {exc}") from exc
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except dbapi.Error:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except dbapi.Error:
                    pass

    def one(self, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[Dict[str, Any]]:
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def _no_schema(self) -> str:
        return (
            f"No SAP schema is configured for {self.company_code} "
            "(FACTORY_HANA_SCHEMAS)."
        )


def hana_for(company_code: str) -> Hana:
    """The SAP reader for a company. Boards that add two companies' figures
    (dispatch across Oil and Mart) call this once per company."""
    return Hana(company_code)
