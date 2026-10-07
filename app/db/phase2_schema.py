"""
Phase 2 schema additions (BRD v2.0 sections 5.4 - 5.10).

The new tables (leads, sales_stages, opportunities, proposals) come from
create_all(); this adds the hierarchy columns to the existing users table.
Idempotent; the Alembic revision c1d2e3f4a5b6 applies the same changes.
"""
from sqlalchemy import text

from app.db.contact_schema import _columns, _index_exists

_COLUMNS = {
    "users": {
        "sales_level": "INT NULL",
        "manager_id": "VARCHAR(36) NULL",
    },
}


def ensure_phase2_schema(engine) -> None:
    with engine.connect() as conn:
        for table, columns in _COLUMNS.items():
            existing = _columns(conn, table)
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}"))
        if not _index_exists(conn, "users", "ix_users_manager_id"):
            conn.execute(text("CREATE INDEX ix_users_manager_id ON users (manager_id)"))
        if not _index_exists(conn, "email_messages", "ix_email_messages_sent_at"):
            # Daily new-contact counts and the funnel read messages by send time
            conn.execute(text("CREATE INDEX ix_email_messages_sent_at ON email_messages (sent_at)"))
        conn.commit()
