# app/db/migrate.py
"""
Bring the database schema up to date: `python -m app.db.migrate` (= alembic upgrade head).

Safe to run from several places at once (Kubernetes initContainers of each API replica,
the compose `migrate` service, AUTO_MIGRATE): a MySQL named lock serialises the runs, and
whoever waits finds the schema already at head and does nothing. After the upgrade the
persona blueprints are synchronised (idempotent data step, so every deploy gets the latest
definitions).
"""
import logging
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, pool, text

from app.core.config import settings

logger = logging.getLogger("app.db.migrate")

LOCK_NAME = "vector:migrate"
LOCK_TIMEOUT_SECONDS = 600
_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _alembic_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_INI.parent / "alembic"))
    cfg.attributes["configure_logging"] = False  # keep the caller's logging setup
    return cfg


def _wait_for_database(engine, attempts: int = 30, delay: float = 2.0) -> None:
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as e:
            if attempt == attempts:
                raise
            logger.info("Database not reachable yet (%s); retrying in %.0fs", type(e).__name__, delay)
            time.sleep(delay)


def _seed_reference_data() -> None:
    from app.core.database import SessionLocal
    from app.db.seed_blueprints import seed_blueprints

    with SessionLocal() as db:
        total = seed_blueprints(db)
    logger.info("Persona blueprints synchronized (%s total)", total)


def run_migrations(seed: bool = True) -> None:
    """alembic upgrade head under a MySQL named lock, then idempotent reference data."""
    engine = create_engine(settings.DATABASE_URL, poolclass=pool.NullPool)
    _wait_for_database(engine)
    with engine.connect() as lock_conn:
        got = lock_conn.execute(text("SELECT GET_LOCK(:n, :t)"),
                                {"n": LOCK_NAME, "t": LOCK_TIMEOUT_SECONDS}).scalar()
        if got != 1:
            raise RuntimeError(f"Could not acquire migration lock {LOCK_NAME!r} within {LOCK_TIMEOUT_SECONDS}s")
        try:
            cfg = _alembic_config()
            logger.info("Running alembic upgrade head")
            command.upgrade(cfg, "head")
            command.current(cfg)
            if seed:
                _seed_reference_data()
        finally:
            lock_conn.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": LOCK_NAME})
    engine.dispose()
    logger.info("Database schema is up to date")


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, (settings.LOG_LEVEL or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run_migrations()
    except Exception:
        logger.exception("Migration failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
