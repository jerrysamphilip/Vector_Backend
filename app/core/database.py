# app/core/database.py
"""
Database connection and session management.
"""

import os
from contextlib import contextmanager
from typing import Any, Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool

from app.core.config import settings
from app.core.db_charset import MYSQL_CHARSET, MYSQL_COLLATION

# Every connection runs in UTC (D2): times are stored as naive UTC in DATETIME(6), and CURRENT_TIMESTAMP(6)
# defaults, NOW() and TIMESTAMP conversions use the session time zone, so the server's own must not matter.
MYSQL_TIME_ZONE = "+00:00"
MYSQL_INIT_COMMAND = f"SET time_zone = '{MYSQL_TIME_ZONE}'"
# The full session init (app engine and Alembic). It used to also turn the InnoDB FULLTEXT stopword list off
# for the contact search index (D7); 0034_drop_contact_fulltext dropped that index, so it is the time zone only.
MYSQL_SESSION_INIT_COMMAND = MYSQL_INIT_COMMAND

# MySQL TLS (D8, off until DEC-01). MYSQL_SSL_MODE mirrors the mysql client's --ssl-mode values.
MYSQL_SSL_MODES = ("disabled", "required", "verify_ca", "verify_identity")


def mysql_ssl_connect_args(mode: Optional[str], ca_path: Optional[str] = None) -> dict[str, Any]:
    """The PyMySQL ``connect_args`` entries for a TLS mode ({} = plain TCP, today's behaviour).

    - ``disabled``: no TLS.
    - ``required``: encrypted, the server certificate is not checked (protects against passive sniffing only).
    - ``verify_ca``: encrypted and the certificate must chain to ``ca_path`` (e.g. the RDS bundle).
    - ``verify_identity``: as verify_ca, and the certificate must name the host connected to.

    Raises ValueError for an unknown mode, or a verify mode without a readable CA bundle."""
    normalised = (mode or "disabled").strip().lower().replace("-", "_")
    if normalised not in MYSQL_SSL_MODES:
        raise ValueError(f"MYSQL_SSL_MODE must be one of {', '.join(MYSQL_SSL_MODES)}; got {mode!r}")
    if normalised == "disabled":
        return {}
    if normalised == "required":
        return {"ssl": {"check_hostname": False, "verify_mode": "none"}}
    ca = (ca_path or "").strip()
    if not ca:
        raise ValueError(f"MYSQL_SSL_MODE={normalised} needs MYSQL_SSL_CA (path to the CA bundle)")
    if not os.path.isfile(ca):
        raise ValueError(f"MYSQL_SSL_CA does not name a readable file: {ca}")
    return {"ssl": {"ca": ca, "check_hostname": normalised == "verify_identity", "verify_mode": "required"}}


def mysql_connect_args(**extra: Any) -> dict[str, Any]:
    """Session settings shared by the app engine and Alembic: charset / collation (D1), UTC (D2)
    and TLS (D8); ``extra`` adds driver timeouts."""
    return {
        **extra,
        "charset": MYSQL_CHARSET,  # Session charset / collation match the tables (D1)
        "collation": MYSQL_COLLATION,
        "init_command": MYSQL_SESSION_INIT_COMMAND,  # Session time zone UTC (D2)
        **mysql_ssl_connect_args(settings.MYSQL_SSL_MODE, settings.MYSQL_SSL_CA),
    }


# Create SQLAlchemy engine with connection pooling
engine = create_engine(
    settings.DATABASE_URL,
    poolclass=QueuePool,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=300,          # Recycle every 5 min — well under MySQL's wait_timeout
    pool_pre_ping=True,        # Verify connections before use
    pool_timeout=30,           # Max wait for a connection from pool
    echo=settings.DEBUG,       # Log SQL queries in debug mode
    connect_args=mysql_connect_args(
        connect_timeout=10,    # Fail fast if DB is unreachable
        read_timeout=30,       # Drop reads that hang
        write_timeout=30,      # Drop writes that hang
    ),
)

# Session factory
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


def get_db() -> Generator[Session, None, None]:
    """
    Dependency for FastAPI routes.
    Yields a database session and ensures cleanup.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """
    Context manager for database sessions.
    Use in non-FastAPI contexts (services, scripts).
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
