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


# Columns added by Phase 1 Contact Management (BRD v2.0), per existing table
_PHASE1_COLUMNS = {
    "prospects": {
        "lifecycle_stage": "VARCHAR(30) NULL",
        "lead_status": "VARCHAR(30) NULL",
        "lead_source": "VARCHAR(100) NULL",
        "legal_basis": "VARCHAR(40) NULL",
        "deleted_at": "TIMESTAMP NULL",
        "deleted_by": "VARCHAR(36) NULL",
        "merged_into_id": "VARCHAR(36) NULL",
    },
    "accounts": {
        "street": "VARCHAR(255) NULL",
        "postal_code": "VARCHAR(20) NULL",
        "annual_revenue": "DECIMAL(18,2) NULL",
        "lifecycle_stage": "VARCHAR(30) NULL",
        "deleted_at": "TIMESTAMP NULL",
        "deleted_by": "VARCHAR(36) NULL",
    },
    "prospect_lists": {
        "list_type": "VARCHAR(10) NOT NULL DEFAULT 'STATIC'",
        "filters": "JSON NULL",
        "description": "TEXT NULL",
    },
    "contact_field_definitions": {
        "group_name": "VARCHAR(100) NULL",
        "required": "TINYINT(1) NOT NULL DEFAULT 0",
    },
}

# Starting lifecycle values for contacts that predate the fields: everyone is a Lead;
# lead status follows their email history.
BACKFILL_LIFECYCLE_SQL = [
    "UPDATE prospects SET lifecycle_stage = 'LEAD' WHERE lifecycle_stage IS NULL",
    """UPDATE prospects p SET lead_status = CASE
         WHEN EXISTS (SELECT 1 FROM email_messages m WHERE m.prospect_id = p.prospect_id AND m.direction = 'INBOUND') THEN 'CONNECTED'
         WHEN EXISTS (SELECT 1 FROM email_messages m WHERE m.prospect_id = p.prospect_id AND m.sent_at IS NOT NULL) THEN 'ATTEMPTED_TO_CONTACT'
         ELSE 'NEW' END
       WHERE lead_status IS NULL""",
    "UPDATE accounts SET lifecycle_stage = 'LEAD' WHERE lifecycle_stage IS NULL",
]


def _columns(conn, table):
    return {row[0] for row in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
    ), {"t": table})}


def _index_exists(conn, table, name):
    return bool(conn.execute(text(
        "SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND INDEX_NAME = :n"
    ), {"t": table, "n": name}).first())


def ensure_phase1_schema(engine) -> None:
    """Add the Phase 1 columns and indexes to an existing database (idempotent)."""
    with engine.connect() as conn:
        added = []
        for table, columns in _PHASE1_COLUMNS.items():
            existing = _columns(conn, table)
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}"))
                    added.append(f"{table}.{column}")
        if "search_text" not in _columns(conn, "prospects"):
            from app.models.prospect import SEARCH_TEXT_SQL
            conn.execute(text(f"ALTER TABLE prospects ADD COLUMN search_text VARCHAR(1100) "
                              f"GENERATED ALWAYS AS ({SEARCH_TEXT_SQL}) STORED"))
            added.append("prospects.search_text")
        for table, name, cols in (("prospects", "ix_prospects_deleted_at", "deleted_at"),
                                  ("prospects", "ix_prospects_lifecycle_stage", "lifecycle_stage"),
                                  ("prospects", "ix_prospects_tenant_live_created", "tenant_id, deleted_at, created_at"),
                                  ("prospects", "ix_prospects_tenant_live_updated", "tenant_id, deleted_at, updated_at")):
            if not _index_exists(conn, table, name):
                conn.execute(text(f"CREATE INDEX {name} ON {table} ({cols})"))
        if not _index_exists(conn, "accounts", "uq_account_tenant_domain"):
            # Domains become unique per workspace (BR-CM-03): keep the oldest account's domain
            conn.execute(text("""
                UPDATE accounts a JOIN (
                    SELECT tenant_id, domain, MIN(created_at) AS first_created FROM accounts
                    WHERE domain IS NOT NULL GROUP BY tenant_id, domain HAVING COUNT(*) > 1
                ) d ON d.tenant_id = a.tenant_id AND d.domain = a.domain AND a.created_at > d.first_created
                SET a.domain = NULL
            """))
            conn.execute(text("CREATE UNIQUE INDEX uq_account_tenant_domain ON accounts (tenant_id, domain)"))
        precision = conn.execute(text(
            "SELECT DATETIME_PRECISION FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'property_changes' AND COLUMN_NAME = 'changed_at'")).scalar()
        if precision is not None and precision < 6:
            conn.execute(text("ALTER TABLE property_changes MODIFY changed_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)"))
        if "prospects.lifecycle_stage" in added:
            for sql in BACKFILL_LIFECYCLE_SQL:
                conn.execute(text(sql))
        conn.commit()
        if added:
            print(f"Contact management (Phase 1): added columns {added}")


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

    ensure_phase1_schema(engine)
