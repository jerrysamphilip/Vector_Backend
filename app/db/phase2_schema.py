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
        "notify_email": "TINYINT(1) NOT NULL DEFAULT 1",
    },
    # Phase 2 "Should" features (BR-SF-02, 06, 08, 11, 16)
    "leads": {"recycle_at": "TIMESTAMP NULL"},
    "opportunities": {
        "forecast_category": "VARCHAR(12) NULL",
        "forecast_category_manual": "TINYINT(1) NOT NULL DEFAULT 0",
        "campaign_id": "VARCHAR(36) NULL",
    },
    "crm_tasks": {"opportunity_id": "VARCHAR(36) NULL"},
    "contact_activities": {
        "opportunity_id": "VARCHAR(36) NULL",
        "source": "VARCHAR(20) NULL",
        "external_id": "VARCHAR(255) NULL",
    },
}


def _existing_tables(conn):
    return {r[0] for r in conn.execute(text(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE()"))}


def ensure_phase2_schema(engine) -> None:
    with engine.connect() as conn:
        for table, columns in _COLUMNS.items():
            existing = _columns(conn, table)
            if not existing:
                continue
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}"))
        # Activities can belong to a deal with no contact (BR-SF-06)
        nullable = conn.execute(text(
            "SELECT IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'contact_activities' AND COLUMN_NAME = 'prospect_id'")).scalar()
        if nullable == "NO":
            conn.execute(text("ALTER TABLE contact_activities MODIFY prospect_id VARCHAR(36) NULL"))
        for table, name, cols in (("crm_tasks", "ix_crm_tasks_opportunity_id", "opportunity_id"),
                                  ("contact_activities", "ix_contact_activities_opportunity_id", "opportunity_id"),
                                  ("contact_activities", "ix_contact_activities_external_id", "external_id"),
                                  ("opportunities", "ix_opps_campaign", "campaign_id")):
            if table in _existing_tables(conn) and not _index_exists(conn, table, name):
                conn.execute(text(f"CREATE INDEX {name} ON {table} ({cols})"))
        if not _index_exists(conn, "users", "ix_users_manager_id"):
            conn.execute(text("CREATE INDEX ix_users_manager_id ON users (manager_id)"))
        tables = _existing_tables(conn)
        if {"opportunities", "sales_stages"} <= tables:
            # Forecast category from the stage for deals created before BR-SF-08
            conn.execute(text("""
                UPDATE opportunities o JOIN sales_stages s ON s.stage_id = o.stage_id
                SET o.forecast_category = CASE WHEN s.is_won THEN 'CLOSED' WHEN s.is_lost THEN 'OMITTED'
                    WHEN s.probability >= 70 THEN 'COMMIT' WHEN s.probability >= 40 THEN 'BEST_CASE' ELSE 'PIPELINE' END
                WHERE o.forecast_category IS NULL
            """))
            # Source campaign for campaign ROI (BR-SF-11): the lead's, else the last campaign that emailed the contact
            conn.execute(text("""
                UPDATE opportunities o JOIN leads l ON l.lead_id = o.lead_id
                SET o.campaign_id = l.campaign_id WHERE o.campaign_id IS NULL AND l.campaign_id IS NOT NULL
            """))
            conn.execute(text("""
                UPDATE opportunities o SET o.campaign_id = (
                    SELECT m.campaign_id FROM email_messages m
                    WHERE m.prospect_id = o.prospect_id AND m.direction = 'OUTBOUND' AND m.sent_at IS NOT NULL
                      AND m.campaign_id IS NOT NULL AND m.sent_at <= o.created_at
                    ORDER BY m.sent_at DESC LIMIT 1)
                WHERE o.campaign_id IS NULL AND o.prospect_id IS NOT NULL
            """))
        if not _index_exists(conn, "email_messages", "ix_email_messages_sent_at"):
            # Daily new-contact counts and the funnel read messages by send time
            conn.execute(text("CREATE INDEX ix_email_messages_sent_at ON email_messages (sent_at)"))
        conn.commit()
