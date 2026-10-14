"""
Where the boards' figures come from: the factory application's PostgreSQL
database and SAP HANA. Both are read-only, both fail by raising the errors
below, and ``boards.sections.SectionBuilder`` turns each failure into one
degraded tile rather than a broken board.
"""

from .hana import Hana, SAPConnectionError, SAPDataError, SAPNotConfigured, hana_for
from .postgres import (
    FactoryError,
    FactoryNotConfigured,
    FactoryQueryError,
    FactoryUnavailable,
    Postgres,
    Unavailable,
    connect,
)

__all__ = [
    "FactoryError",
    "FactoryNotConfigured",
    "FactoryQueryError",
    "FactoryUnavailable",
    "Hana",
    "Postgres",
    "SAPConnectionError",
    "SAPDataError",
    "SAPNotConfigured",
    "Unavailable",
    "connect",
    "hana_for",
]
