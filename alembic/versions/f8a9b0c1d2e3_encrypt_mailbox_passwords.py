"""Encrypt mailbox SMTP/IMAP passwords at rest (BR-DF-09)

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-10-08 00:00:00.000000

Widens sending_inboxes.smtp_password / imap_password to VARCHAR(1024) and encrypts any
plain-text values with CREDENTIALS_ENCRYPTION_KEY. The app also does this on start-up
(app/db/security_schema.py); already-encrypted values are left alone.
"""
from alembic import op
from sqlalchemy import text

revision = 'f8a9b0c1d2e3'
down_revision = 'e7f8a9b0c1d2'
branch_labels = None
depends_on = None


def upgrade():
    from app.db.security_schema import encrypt_mailbox_passwords
    encrypt_mailbox_passwords(op.get_bind().engine)


def downgrade():
    """Back to plain text (the column width is kept)."""
    from app.core.encrypted_type import PREFIX, decrypt_value
    bind = op.get_bind()
    for column in ("smtp_password", "imap_password"):
        rows = bind.execute(text(f"SELECT inbox_id, {column} FROM sending_inboxes WHERE {column} LIKE :p"),
                            {"p": PREFIX + "%"}).fetchall()
        for inbox_id, value in rows:
            bind.execute(text(f"UPDATE sending_inboxes SET {column} = :v WHERE inbox_id = :id"),
                         {"v": decrypt_value(value), "id": inbox_id})
