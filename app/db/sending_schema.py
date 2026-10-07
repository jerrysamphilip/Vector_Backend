"""
Schema additions for the sending defect fixes (BR-DF-01, 04, 05, 06, 07).

create_all() does not add columns to existing tables, so these are applied
idempotently at startup, the same way contact_schema does it. The Alembic
revision b0c1d2e3f4a5 makes the same changes for migration-managed databases.
"""
import logging

from sqlalchemy import text

from app.db.contact_schema import _columns, _index_exists

logger = logging.getLogger(__name__)

_COLUMNS = {
    "email_messages": {
        "send_key": "VARCHAR(330) NULL",
        "claimed_at": "TIMESTAMP NULL",
        "ses_message_id": "VARCHAR(100) NULL",
        "final_status": "VARCHAR(30) NULL",
        "final_status_at": "TIMESTAMP NULL",
    },
    "campaigns": {
        "paused_reason": "TEXT NULL",
        "paused_at": "TIMESTAMP NULL",
        "auto_paused": "TINYINT(1) NOT NULL DEFAULT 0",
        "health_baseline_at": "TIMESTAMP NULL",
    },
    "sending_inboxes": {
        "auth_type": "VARCHAR(30) NOT NULL DEFAULT 'PASSWORD'",
        "oauth_refresh_token": "TEXT NULL",
        "oauth_access_token": "TEXT NULL",
        "oauth_expires_at": "TIMESTAMP NULL",
        "oauth_error": "TEXT NULL",
        "imap_last_error": "TEXT NULL",
    },
    "sending_domains": {
        "sends_24h": "INT NOT NULL DEFAULT 0",
        "bounce_rate_24h": "FLOAT NOT NULL DEFAULT 0",
        "complaint_rate_24h": "FLOAT NOT NULL DEFAULT 0",
        "health_checked_at": "TIMESTAMP NULL",
    },
}

_INDEXES = (
    ("email_messages", "ix_email_messages_status_scheduled", "status, scheduled_at", False),
    ("email_messages", "ix_email_messages_reconcile", "final_status, status, sent_at", False),
    ("email_messages", "ix_email_messages_ses_message_id", "ses_message_id", False),
    ("email_messages", "send_key", "send_key", True),
)


def ensure_sending_schema(engine) -> None:
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
        for table, name, cols, unique in _INDEXES:
            if not _index_exists(conn, table, name):
                conn.execute(text(f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON {table} ({cols})"))
                added.append(name)
        # Messages already sent before this change get a final status from what we know
        conn.execute(text("""
            UPDATE email_messages SET final_status = CASE
                WHEN status IN ('BOUNCED','COMPLAINED','REJECTED','FAILED') THEN status
                WHEN delivered_at IS NOT NULL THEN 'DELIVERED'
                ELSE 'UNCONFIRMED' END,
              final_status_at = COALESCE(delivered_at, sent_at)
            WHERE final_status IS NULL AND direction = 'OUTBOUND'
              AND sent_at IS NOT NULL AND sent_at < UTC_TIMESTAMP() - INTERVAL 1 DAY
        """))
        conn.commit()
        if added:
            logger.info("Sending schema: added %s", ", ".join(added))
