"""Platform-wide switches kept in the database so every process (API replicas, worker) sees them."""
from typing import Optional

from sqlalchemy import text

from app.core.database import SessionLocal

SCHEDULER_PAUSED = "scheduler_paused"


def get(name: str) -> Optional[str]:
    db = SessionLocal()
    try:
        return db.execute(text("SELECT value FROM platform_flags WHERE name = :n"), {"n": name}).scalar()
    finally:
        db.close()


def set_flag(name: str, value: Optional[str]) -> None:
    db = SessionLocal()
    try:
        if value is None:
            db.execute(text("DELETE FROM platform_flags WHERE name = :n"), {"n": name})
        else:
            db.execute(text("INSERT INTO platform_flags (name, value) VALUES (:n, :v) "
                            "ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = CURRENT_TIMESTAMP"),
                       {"n": name, "v": value})
        db.commit()
    finally:
        db.close()


def scheduler_paused() -> bool:
    return get(SCHEDULER_PAUSED) is not None
