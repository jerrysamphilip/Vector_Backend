"""
Named MySQL locks (GET_LOCK) for leader election: one process runs a job iteration (app/jobs.py) or a
per-workspace batch (contact rescoring) at a time. Each lock lives exactly as long as its own
connection, on an engine separate from the app pool.
"""

import logging
from typing import Any, Optional

from sqlalchemy import create_engine, pool, text

from app.core.config import settings

logger = logging.getLogger("app.jobs")

_lock_engine: Optional[Any] = None


def _get_lock_engine() -> Any:
    # Own connections (not the app pool): a lock lives exactly as long as its connection
    global _lock_engine
    if _lock_engine is None:
        _lock_engine = create_engine(settings.DATABASE_URL, poolclass=pool.NullPool,
                                     connect_args={"connect_timeout": 10})
    return _lock_engine


def lock_name(job_name: str) -> str:
    # Scoped to the database so environments sharing a MySQL server do not block each other
    database = _get_lock_engine().url.database or ""
    return f"vector:job:{job_name}:{database}"[:64]


def try_acquire(job_name: str, wait_seconds: float = 0) -> Any:
    """Return an open connection holding the job's lock, or None if another process holds it
    (after waiting up to wait_seconds for it to be released; 0 = do not wait)."""
    conn = _get_lock_engine().connect()
    try:
        got = conn.execute(text("SELECT GET_LOCK(:n, :w)"),
                           {"n": lock_name(job_name), "w": max(0, int(wait_seconds))}).scalar()
    except Exception:
        conn.close()
        raise
    if got == 1:
        return conn
    conn.close()
    return None


def release(conn: Any, job_name: str) -> None:
    try:
        conn.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": lock_name(job_name)})
    except Exception as e:
        # Closing the connection below releases the lock anyway
        logger.warning("Job %s: RELEASE_LOCK failed (%s)", job_name, type(e).__name__)
    finally:
        conn.close()
