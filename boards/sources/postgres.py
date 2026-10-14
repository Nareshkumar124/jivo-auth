"""
Read-only access to the factory application's PostgreSQL database.

Deliberately NOT a Django database alias. The factory's tables have no models
in this project, Django's test runner would try to create ``test_<name>`` on
the factory server for every alias in ``DATABASES``, and nothing here may
ever write there. So a board build opens one psycopg connection of its own,
read-only at the session level (``default_transaction_read_only=on``), with a
statement timeout, and closes it when the board is composed.

Queries use psycopg's ``%s`` placeholders and return rows as dicts keyed by
the column names the statement aliases.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence

import psycopg
from django.conf import settings
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)


class FactoryError(Exception):
    """The factory database could not serve a query."""


class FactoryUnavailable(FactoryError):
    """The database is not configured, not reachable, or the connection died.

    A section builder latches on this: once the database is gone, the other
    sections are degraded without each waiting for its own timeout.
    """


class FactoryNotConfigured(FactoryUnavailable):
    """The factory database isn't set up on this service."""


class FactoryQueryError(FactoryError):
    """The database answered, and rejected this statement (or timed it out)."""


def configured() -> bool:
    db = settings.FACTORY_DB
    return bool(db["NAME"] and db["USER"] and db["HOST"])


def _first_line(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or ["unknown error"])[0]


class Postgres:
    """One read-only connection, for one board build."""

    def __init__(self, connection: psycopg.Connection):
        self._connection = connection

    def rows(self, sql: str, params: Optional[Sequence[Any]] = None) -> list[dict]:
        """Run one statement and return its rows as dicts."""
        try:
            with self._connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql, params)
                if cursor.description is None:
                    return []
                return cursor.fetchall()
        except psycopg.errors.QueryCanceled as exc:
            raise FactoryQueryError(
                "The query took longer than FACTORY_DB_STATEMENT_TIMEOUT."
            ) from exc
        except psycopg.OperationalError as exc:
            raise FactoryUnavailable(
                f"The factory database stopped answering: {_first_line(exc)}"
            ) from exc
        except psycopg.Error as exc:
            logger.warning("Factory query failed: %s", _first_line(exc))
            raise FactoryQueryError(_first_line(exc)) from exc

    def one(self, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[dict]:
        """The first row, or None."""
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def value(self, sql: str, params: Optional[Sequence[Any]] = None, default: Any = None) -> Any:
        """The first column of the first row, or ``default``."""
        row = self.one(sql, params)
        if not row:
            return default
        value = next(iter(row.values()))
        return default if value is None else value


class Unavailable:
    """Stands in for ``Postgres`` when the database could not be reached.

    Every query raises the original error, so each section degrades with
    the same reason instead of the whole board failing.
    """

    def __init__(self, error: FactoryUnavailable):
        self.error = error

    def rows(self, sql: str, params: Optional[Sequence[Any]] = None) -> list[dict]:
        raise self.error

    def one(self, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[dict]:
        raise self.error

    def value(self, sql: str, params: Optional[Sequence[Any]] = None, default: Any = None) -> Any:
        raise self.error


@contextmanager
def connect() -> Iterator[Postgres]:
    """A read-only connection to the factory database, closed on exit.

    Raises ``FactoryUnavailable`` when the database isn't configured or
    can't be reached within ``FACTORY_DB_CONNECT_TIMEOUT``.
    """
    if not configured():
        raise FactoryNotConfigured(
            "The factory database is not configured on this service "
            "(FACTORY_DB_NAME, FACTORY_DB_USER, FACTORY_DB_HOST)."
        )

    db = settings.FACTORY_DB
    statement_timeout_ms = int(db["STATEMENT_TIMEOUT"]) * 1000

    try:
        connection = psycopg.connect(
            dbname=db["NAME"],
            user=db["USER"],
            password=db["PASSWORD"],
            host=db["HOST"],
            port=db["PORT"],
            connect_timeout=db["CONNECT_TIMEOUT"],
            application_name="jivo_auth boards",
            # Read-only at the session level, whatever a statement says, and
            # a ceiling on how long any one statement may run.
            options=(
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={statement_timeout_ms}"
            ),
            autocommit=True,
        )
    except psycopg.Error as exc:
        logger.warning("Factory database connection failed: %s", _first_line(exc))
        raise FactoryUnavailable(
            f"The factory database could not be reached: {_first_line(exc)}"
        ) from exc

    try:
        yield Postgres(connection)
    finally:
        try:
            connection.close()
        except psycopg.Error:
            pass
