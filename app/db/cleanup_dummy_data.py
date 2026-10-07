# app/db/cleanup_dummy_data.py
"""
Cleanup Script - Remove dummy data before importing real prospects.
CAUTION: This will delete all data except tenants, users, and persona_blueprints.

Usage:
    python -m app.db.cleanup_dummy_data
"""

from sqlalchemy.orm import Session
from sqlalchemy import text
from app.core.database import SessionLocal
from app.models import (
    Campaign,
    CampaignProspect,
    CampaignStateEvent,
    EmailMessage,
    EmailEvent,
    EmailTemplate,
    EmailTemplateVersion,
    EmailSequence,
    Prospect,
    ProspectList,
    ProspectListMember,
    ProspectPersona,
    GlobalUnsubscribe,
    CampaignGroup,
    CampaignGroupMember,
    CampaignMetricsRealtime,
    AuditLog,
    ComplianceCheck,
)


def cleanup_all_dummy_data(db: Session, confirm: bool = False) -> dict:
    """
    Delete all dummy data from the database.
    
    Keeps:
    - tenants
    - users
    - persona_blueprints
    - ai_prompts
    
    Deletes (in order to respect foreign keys):
    - email_events
    - email_messages
    - compliance_checks
    - email_template_versions
    - email_templates
    - email_sequences
    - campaign_metrics_realtime
    - campaign_state_events
    - campaign_prospects
    - campaign_group_members
    - campaigns
    - campaign_groups
    - prospect_list_members
    - prospect_personas
    - prospects
    - prospect_lists
    - global_unsubscribes
    - audit_logs
    
    Returns:
        dict with counts of deleted records
    """
    if not confirm:
        print("⚠️  DRY RUN - No data will be deleted. Use confirm=True to delete.")
        return _count_records(db)
    
    print("🗑️  Starting cleanup...")
    deleted = {}
    
    # Delete in order (child tables first)
    tables_in_order = [
        ("email_events", EmailEvent),
        ("email_messages", EmailMessage),
        ("compliance_checks", ComplianceCheck),
        ("email_template_versions", EmailTemplateVersion),
        ("email_templates", EmailTemplate),
        ("email_sequences", EmailSequence),
        ("campaign_metrics_realtime", CampaignMetricsRealtime),
        ("campaign_state_events", CampaignStateEvent),
        ("campaign_prospects", CampaignProspect),
        ("campaign_group_members", CampaignGroupMember),
        ("campaigns", Campaign),
        ("campaign_groups", CampaignGroup),
        ("prospect_list_members", ProspectListMember),
        ("prospect_personas", ProspectPersona),
        ("prospects", Prospect),
        ("prospect_lists", ProspectList),
        ("global_unsubscribes", GlobalUnsubscribe),
        ("audit_logs", AuditLog),
    ]
    
    for table_name, model in tables_in_order:
        try:
            count = db.query(model).count()
            if count > 0:
                db.query(model).delete()
                deleted[table_name] = count
                print(f"  ✓ Deleted {count} rows from {table_name}")
        except Exception as e:
            print(f"  ✗ Error deleting from {table_name}: {e}")
            deleted[table_name] = f"ERROR: {e}"
    
    db.commit()
    print("\n✅ Cleanup complete!")
    return deleted


def _count_records(db: Session) -> dict:
    """Count records in each table (for dry run)."""
    tables = [
        ("campaigns", Campaign),
        ("campaign_prospects", CampaignProspect),
        ("email_messages", EmailMessage),
        ("email_templates", EmailTemplate),
        ("prospects", Prospect),
        ("prospect_lists", ProspectList),
    ]
    
    counts = {}
    for name, model in tables:
        count = db.query(model).count()
        counts[name] = count
        print(f"  {name}: {count} records")
    
    return counts


def show_uuid_query():
    """Print the query to view binary UUIDs as readable format."""
    print("""
To view binary UUIDs in MySQL, use:

SELECT 
    BIN_TO_UUID(prospect_id) as prospect_id,
    first_name,
    email,
    company_name,
    designation
FROM prospects;

SELECT 
    BIN_TO_UUID(campaign_id) as campaign_id,
    campaign_name,
    status
FROM campaigns;
""")


if __name__ == "__main__":
    import sys
    
    db = SessionLocal()
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--confirm":
            cleanup_all_dummy_data(db, confirm=True)
        else:
            print("=" * 50)
            print("DRY RUN - Current record counts:")
            print("=" * 50)
            cleanup_all_dummy_data(db, confirm=False)
            print("\n" + "=" * 50)
            print("To actually delete, run:")
            print("  python -m app.db.cleanup_dummy_data --confirm")
            print("=" * 50)
            show_uuid_query()
    finally:
        db.close()
