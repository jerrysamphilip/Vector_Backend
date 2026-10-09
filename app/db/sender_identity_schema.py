"""
Schema additions for per-tenant sender identity and incremental IMAP sync.

create_all() never adds columns to an existing table, so these are applied
idempotently (information_schema check, then ALTER / CREATE), the same way
sending_schema.py does it. Safe to run on every start or from a migration.

  company_profiles.postal_address   The tenant's physical postal address, printed
                                     in every campaign email footer (CAN-SPAM).
                                     The default company profile is the tenant's
                                     sender identity (company name + address).
  imap_sync_state                    Highest IMAP UID seen per inbox + folder,
                                     with the folder's UIDVALIDITY (B11).
"""
import logging

from sqlalchemy import text

from app.db.contact_schema import _columns

logger = logging.getLogger(__name__)

_COLUMNS = {
    "company_profiles": {
        "postal_address": "TEXT NULL",
    },
}

_IMAP_SYNC_STATE_DDL = """
    CREATE TABLE IF NOT EXISTS imap_sync_state (
        inbox_id VARCHAR(36) NOT NULL,
        folder VARCHAR(255) NOT NULL,
        uidvalidity BIGINT NULL,
        last_uid BIGINT NOT NULL DEFAULT 0,
        updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (inbox_id, folder)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_sender_identity_schema(engine) -> None:
    """Add company_profiles.postal_address and the imap_sync_state table (idempotent)."""
    with engine.connect() as conn:
        added = []
        for table, columns in _COLUMNS.items():
            existing = _columns(conn, table)
            if not existing:
                continue  # table not created yet; create_all handles it
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}"))
                    added.append(f"{table}.{column}")
        if not _columns(conn, "imap_sync_state"):
            conn.execute(text(_IMAP_SYNC_STATE_DDL))
            added.append("imap_sync_state")
        conn.commit()
        if added:
            logger.info("Sender identity / IMAP sync schema: added %s", ", ".join(added))
