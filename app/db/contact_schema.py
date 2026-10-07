# app/db/contact_schema.py
"""
Brings an existing database up to the contact-management schema on startup.

Base.metadata.create_all() creates the new tables (accounts, contact_activities,
contact_field_definitions) but never adds columns to an existing prospects table,
so the new prospect columns are added here. The one-off backfills run only when
the column is first added, so later edits (e.g. unlinking an account) stick.
The same changes are in alembic revision e7f8a9b0c1d2 for environments that migrate.
"""

from sqlalchemy import text

_PROSPECT_COLUMNS = {
    "phone": "VARCHAR(50) NULL",
    "mobile_phone": "VARCHAR(50) NULL",
    "owner_id": "VARCHAR(36) NULL",
    "account_id": "VARCHAR(36) NULL",
    "tags": "JSON NULL",
    "custom_fields": "JSON NULL",
}

# Accounts are keyed by (tenant_id, name); company names longer than the column are truncated.
BACKFILL_ACCOUNTS_SQL = """
    INSERT IGNORE INTO accounts (account_id, tenant_id, name, industry, emp_band, created_at, updated_at)
    SELECT UUID(), p.tenant_id, LEFT(TRIM(p.company_name), 255), MAX(p.industry), MAX(p.emp_band), NOW(), NOW()
    FROM prospects p
    WHERE p.account_id IS NULL AND p.company_name IS NOT NULL AND TRIM(p.company_name) <> ''
    GROUP BY p.tenant_id, LEFT(TRIM(p.company_name), 255)
"""

LINK_ACCOUNTS_SQL = """
    UPDATE prospects p
    JOIN accounts a ON a.tenant_id = p.tenant_id AND a.name = LEFT(TRIM(p.company_name), 255)
    SET p.account_id = a.account_id
    WHERE p.account_id IS NULL AND p.company_name IS NOT NULL AND TRIM(p.company_name) <> ''
"""

# Existing contacts are owned by whoever uploaded the first list they appear in.
BACKFILL_OWNERS_SQL = """
    UPDATE prospects p
    JOIN (
        SELECT m.prospect_id, SUBSTRING_INDEX(GROUP_CONCAT(l.uploaded_by ORDER BY m.added_at), ',', 1) AS owner_id
        FROM prospect_list_members m
        JOIN prospect_lists l ON l.list_id = m.list_id
        GROUP BY m.prospect_id
    ) first_list ON first_list.prospect_id = p.prospect_id
    SET p.owner_id = first_list.owner_id
    WHERE p.owner_id IS NULL
"""


def ensure_contact_schema(engine) -> None:
    with engine.connect() as conn:
        existing = {
            row[0]
            for row in conn.execute(text("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'prospects'
            """))
        }
        added = []
        for column, ddl in _PROSPECT_COLUMNS.items():
            if column not in existing:
                conn.execute(text(f"ALTER TABLE prospects ADD COLUMN {column} {ddl}"))
                added.append(column)
        conn.commit()

        if "owner_id" in added:
            conn.execute(text("CREATE INDEX ix_prospects_owner_id ON prospects (owner_id)"))
            conn.execute(text(
                "ALTER TABLE prospects ADD CONSTRAINT fk_prospects_owner "
                "FOREIGN KEY (owner_id) REFERENCES users (user_id)"
            ))
            conn.execute(text(BACKFILL_OWNERS_SQL))
        if "account_id" in added:
            conn.execute(text("CREATE INDEX ix_prospects_account_id ON prospects (account_id)"))
            conn.execute(text(
                "ALTER TABLE prospects ADD CONSTRAINT fk_prospects_account "
                "FOREIGN KEY (account_id) REFERENCES accounts (account_id)"
            ))
            conn.execute(text(BACKFILL_ACCOUNTS_SQL))
            conn.execute(text(LINK_ACCOUNTS_SQL))
        conn.commit()

        if added:
            print(f"Contact management: added prospects columns {added}")
