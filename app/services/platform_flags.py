"""Platform-wide switches kept in the database so every process (API replicas, worker) sees them.

SQL in app/repositories/platform_flags.py; each call uses its own short session.
"""
from typing import Optional

from app.core.database import SessionLocal
from app.repositories.platform_flags import PlatformFlagRepository

SCHEDULER_PAUSED = "scheduler_paused"


def get(name: str) -> Optional[str]:
    db = SessionLocal()
    try:
        return PlatformFlagRepository(db).get(name)
    finally:
        db.close()


def set_flag(name: str, value: Optional[str]) -> None:
    db = SessionLocal()
    try:
        flags = PlatformFlagRepository(db)
        if value is None:
            flags.remove(name)
        else:
            flags.put(name, value)
        db.commit()
    finally:
        db.close()


def scheduler_paused() -> bool:
    return get(SCHEDULER_PAUSED) is not None
