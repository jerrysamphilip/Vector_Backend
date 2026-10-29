"""The one column type for points in time (chunk D2): MySQL ``DATETIME(6)`` holding naive UTC.

``TIMESTAMP`` stops at 2038-01-19 and converts through the session time zone; ``DATETIME(6)`` reaches
year 9999, stores what it is given and keeps microseconds. Values are naive UTC (``app.core.clock.utcnow``)
and every connection runs with ``time_zone = '+00:00'`` (app/core/database.py, alembic/env.py), so
``CURRENT_TIMESTAMP(6)`` defaults are UTC too.

    created_at = Column(UtcDateTime, server_default=NOW6)
    updated_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)

``NOW6`` carries the ``(6)``: MySQL rejects ``DATETIME(6) DEFAULT CURRENT_TIMESTAMP`` (fsp mismatch),
and as an ``onupdate`` it keeps the stamp at the same precision as the default. Calendar dates stay ``Date``.
"""
from sqlalchemy import DateTime, text
from sqlalchemy.dialects import mysql

# Generic DateTime elsewhere (DDL compiled for another dialect); DATETIME(6) on MySQL
UtcDateTime = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")

# Server default / on-update value matching UtcDateTime's precision
NOW6 = text("CURRENT_TIMESTAMP(6)")

# updated_at that the server also refreshes on every UPDATE (raw-SQL fixes included). SQLAlchemy has no DDL for
# MySQL's ON UPDATE clause, so it rides on the DEFAULT; pair it with server_onupdate=FetchedValue() (0035).
NOW6_ON_UPDATE = text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)")
