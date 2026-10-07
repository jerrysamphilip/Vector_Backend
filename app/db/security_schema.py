# app/db/security_schema.py
"""
Start-up hardening of existing data (BR-DF-09): widen the mailbox password columns for
encrypted values and encrypt any that are still stored as plain text. Safe to run every
start; already-encrypted values are left alone. Alembic revision f8a9b0c1d2e3 runs the same.
"""

from sqlalchemy import text

from app.core.encrypted_type import PREFIX, encrypt_value

PASSWORD_COLUMNS = ("smtp_password", "imap_password")


def encrypt_mailbox_passwords(engine) -> int:
    with engine.connect() as conn:
        lengths = dict(conn.execute(text("""
            SELECT COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sending_inboxes'
              AND COLUMN_NAME IN ('smtp_password', 'imap_password')
        """)).fetchall())
        for column in PASSWORD_COLUMNS:
            if column in lengths and (lengths[column] or 0) < 1024:
                conn.execute(text(f"ALTER TABLE sending_inboxes MODIFY {column} VARCHAR(1024) NULL"))

        changed = 0
        for column in PASSWORD_COLUMNS:
            if column not in lengths:
                continue
            rows = conn.execute(text(
                f"SELECT inbox_id, {column} FROM sending_inboxes "
                f"WHERE {column} IS NOT NULL AND {column} <> '' AND {column} NOT LIKE :prefix"
            ), {"prefix": PREFIX + "%"}).fetchall()
            for inbox_id, value in rows:
                conn.execute(text(f"UPDATE sending_inboxes SET {column} = :value WHERE inbox_id = :id"),
                             {"value": encrypt_value(value), "id": inbox_id})
                changed += 1
        conn.commit()
    if changed:
        print(f"Security: encrypted {changed} stored mailbox password(s)")
    return changed
