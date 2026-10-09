# alembic/env.py
"""
Alembic migration environment configuration.
Configured to use app settings for database connection.

Run migrations with `python -m app.db.migrate` (serialises concurrent runs with a MySQL
named lock). The revisions in alembic/versions/ start at 0001_baseline; the pre-baseline
chain is kept, unloaded, in alembic/versions_legacy/.
"""

import logging
import os
import sys
from logging.config import fileConfig

from sqlalchemy import bindparam, create_engine, pool, text

from alembic import context

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.models import Base

# this is the Alembic Config object
config = context.config

# Interpret the config file for Python logging, without silencing the app's own loggers
# (migrations can run inside the API process when AUTO_MIGRATE is on)
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

logger = logging.getLogger("alembic.env")

# Metadata for `alembic revision --autogenerate` (review its output; never run it blindly)
target_metadata = Base.metadata


def _drop_legacy_version(connection) -> None:
    """
    A database stamped by the legacy chain (alembic/versions_legacy/) holds a revision this
    chain does not know, which would make `upgrade` fail. Such a database predates the
    baseline, so its stamp is removed and 0001_baseline (idempotent) brings it up to date.
    """
    exists = connection.execute(text(
        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'alembic_version'"
    )).first()
    if not exists:
        return
    known = {rev.revision for rev in context.script.walk_revisions()}
    stamped = [row[0] for row in connection.execute(text("SELECT version_num FROM alembic_version"))]
    legacy = [rev for rev in stamped if rev not in known]
    if legacy:
        logger.warning("Database is stamped with pre-baseline revision(s) %s; re-applying from 0001_baseline", legacy)
        connection.execute(text("DELETE FROM alembic_version WHERE version_num IN :revs").bindparams(
            bindparam("revs", expanding=True)), {"revs": legacy})


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`)."""
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the database in settings.DATABASE_URL."""
    connectable = create_engine(settings.DATABASE_URL, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        _drop_legacy_version(connection)
        # End the transaction the checks autobegan, so Alembic owns (and commits) its own
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,  # Detect column type changes
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
