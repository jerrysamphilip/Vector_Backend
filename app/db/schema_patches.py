# app/db/schema_patches.py
"""
Idempotent schema patches for databases created before Alembic became the source of truth.

Base.metadata.create_all() creates missing tables but never adds columns, indexes or
constraints to tables that already exist, so the column/index additions that used to run in
app/main.py on every start live here. Every patch checks information_schema first, so
running apply_all() on an empty database (after create_all) or on a database that is already
up to date is a no-op.

Called by the baseline Alembic revision (alembic/versions/0001_baseline.py); do not call it
from application start-up. New schema changes go into new Alembic revisions.
"""
import logging

from sqlalchemy import text

from app.db.contact_schema import _columns, _index_exists

logger = logging.getLogger(__name__)


def _engine(bind):
    """Accept an Engine or a Connection (Alembic hands the migration a Connection)."""
    return getattr(bind, "engine", bind)


def _constraint_exists(conn, table, name, kind=None):
    sql = ("SELECT 1 FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA = DATABASE() "
           "AND TABLE_NAME = :t AND CONSTRAINT_NAME = :n")
    if kind:
        sql += " AND CONSTRAINT_TYPE = :k"
    return bool(conn.execute(text(sql), {"t": table, "n": name, "k": kind}).first())


def _is_nullable(conn, table, column):
    return conn.execute(text(
        "SELECT IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :t AND COLUMN_NAME = :c"), {"t": table, "c": column}).scalar()


def _add_missing_columns(conn, table, columns):
    """columns: {name: ddl}. Returns the names that were added."""
    existing = _columns(conn, table)
    if not existing:
        return []  # table missing; create_all owns it
    added = []
    for column, ddl in columns.items():
        if column not in existing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}"))
            added.append(column)
    return added


_CAMPAIGN_COLUMNS = {
    "sender_name": "VARCHAR(100) NULL",
    "campaign_description": "TEXT NULL",
    "cta_link": "VARCHAR(500) NULL",
    "daily_batch_size": "INT NULL",
}

_TEMPLATE_COLUMNS = {
    "cta_link": "TEXT NULL",
    "personalization_tokens": "JSON NULL",
}

_INBOX_COLUMNS = {
    "imap_host": "VARCHAR(255) NULL",
    "imap_port": "INT DEFAULT 993",
    "imap_username": "VARCHAR(255) NULL",
    "imap_password": "VARCHAR(255) NULL",
    "last_sync_at": "TIMESTAMP NULL",
    "warmup_status": "VARCHAR(50) DEFAULT 'ACTIVE'",
    "warmup_pool": "VARCHAR(50) DEFAULT 'FOUNDATION'",
    "warmup_reputation": "FLOAT DEFAULT 65",
    "warmup_auto_adjust": "BOOLEAN DEFAULT TRUE",
    "warmup_randomize": "BOOLEAN DEFAULT TRUE",
    "warmup_reply_rate_target": "INT DEFAULT 35",
    "warmup_max_target": "INT NULL",
    "warmup_daily_target": "INT DEFAULT 0",
    "warmup_identifier": "VARCHAR(120) NULL",
    "warmup_issue_code": "VARCHAR(100) NULL",
    "warmup_issue_message": "TEXT NULL",
    "warmup_last_activity_at": "TIMESTAMP NULL",
    "warmup_today_sent": "INT DEFAULT 0",
    "warmup_today_opened": "INT DEFAULT 0",
    "warmup_today_replied": "INT DEFAULT 0",
    "warmup_today_saved": "INT DEFAULT 0",
}

_USER_COLUMNS = {
    "password_hash": "VARCHAR(255) NULL",
    "auth_provider": "VARCHAR(50) NULL DEFAULT 'local'",
    "google_id": "VARCHAR(255) NULL",
    "email_verified": "BOOLEAN NOT NULL DEFAULT FALSE",
    "avatar_url": "VARCHAR(500) NULL",
    "invited_by": "VARCHAR(36) NULL",
    "custom_permissions": "JSON NULL",
}


def patch_core_columns(bind) -> None:
    """Columns, keys and nullability that app/main.py used to add on every start."""
    with _engine(bind).connect() as conn:
        added = {}
        for table, columns in (("campaigns", _CAMPAIGN_COLUMNS),
                               ("email_templates", _TEMPLATE_COLUMNS),
                               ("sending_inboxes", _INBOX_COLUMNS),
                               ("global_unsubscribes", {"suppression_expires_at": "TIMESTAMP NULL"}),
                               ("company_profiles", {"tenant_id": "VARCHAR(36) NULL"}),
                               ("users", _USER_COLUMNS)):
            cols = _add_missing_columns(conn, table, columns)
            if cols:
                added[table] = cols

        # email_messages: inbound messages belong to a conversation and may have no campaign
        msg_added = _add_missing_columns(conn, "email_messages", {
            "conversation_id": "VARCHAR(36) NULL",
            "direction": "VARCHAR(20) DEFAULT 'OUTBOUND'",
        })
        if msg_added:
            added["email_messages"] = msg_added
        if "conversation_id" in msg_added and not _constraint_exists(conn, "email_messages", "fk_em_conversation"):
            conn.execute(text("ALTER TABLE email_messages ADD CONSTRAINT fk_em_conversation "
                              "FOREIGN KEY (conversation_id) REFERENCES conversations(id)"))
        if _is_nullable(conn, "email_messages", "campaign_id") == "NO":
            conn.execute(text("ALTER TABLE email_messages MODIFY COLUMN campaign_id VARCHAR(36) NULL"))

        # Tenant isolation for company profiles: legacy rows keep NULL and are invisible to every tenant
        if _columns(conn, "company_profiles") and not _index_exists(conn, "company_profiles", "ix_company_profiles_tenant_id"):
            conn.execute(text("CREATE INDEX ix_company_profiles_tenant_id ON company_profiles (tenant_id)"))

        # Auth keys on users
        if _columns(conn, "users"):
            if not _index_exists(conn, "users", "uq_user_tenant_email"):
                conn.execute(text("ALTER TABLE users ADD CONSTRAINT uq_user_tenant_email UNIQUE (tenant_id, email)"))
            if not _index_exists(conn, "users", "uq_users_google_id"):
                conn.execute(text("ALTER TABLE users ADD CONSTRAINT uq_users_google_id UNIQUE (google_id)"))
            # create_all names this FK itself; only add ours when no FK covers invited_by yet
            has_fk = conn.execute(text("""
                SELECT 1 FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users'
                  AND COLUMN_NAME = 'invited_by' AND REFERENCED_TABLE_NAME IS NOT NULL
            """)).first()
            if not has_fk:
                conn.execute(text("ALTER TABLE users ADD CONSTRAINT fk_users_invited_by "
                                  "FOREIGN KEY (invited_by) REFERENCES users(user_id)"))
        conn.commit()
        for table, cols in added.items():
            logger.info("Schema patch: added %s columns %s", table, cols)


def apply_all(bind) -> None:
    """Bring any database (empty after create_all, or legacy) to the baseline schema. Idempotent."""
    from app.db.contact_schema import ensure_contact_schema
    from app.db.phase2_schema import ensure_phase2_schema
    from app.db.security_schema import allow_anonymised_audit_logs, encrypt_mailbox_passwords
    from app.db.sender_identity_schema import ensure_sender_identity_schema
    from app.db.sending_schema import ensure_sending_schema

    engine = _engine(bind)
    patch_core_columns(engine)
    ensure_contact_schema(engine)
    ensure_sending_schema(engine)
    ensure_phase2_schema(engine)
    ensure_sender_identity_schema(engine)
    encrypt_mailbox_passwords(engine)
    allow_anonymised_audit_logs(engine)
