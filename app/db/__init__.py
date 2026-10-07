# app/db/init_db.py
"""
Database initialization script.
Creates all tables and optionally seeds initial data.
"""

from sqlalchemy import text
from app.core.database import engine
from app.models import Base

# Import all models to register them with Base.metadata
from app.models.tenant import Tenant
from app.models.user import User
from app.models.prospect import Prospect, GlobalUnsubscribe
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.campaign import Campaign, CampaignStateEvent, CampaignProspect
from app.models.email_sequence import EmailSequence
from app.models.email_template import AIPrompt, EmailTemplate, EmailTemplateVersion
from app.models.sending_inbox import SendingInbox
from app.models.email_message import EmailMessage, EmailEvent
from app.models.campaign_group import CampaignGroup, CampaignGroupMember
from app.models.metrics import CampaignMetricsRealtime
from app.models.audit import AuditLog


def create_database():
    """Create database if it doesn't exist."""
    from app.core.config import settings
    
    # Connect without database to create it
    base_url = (
        f"mysql+pymysql://{settings.MYSQL_USER}:{settings.MYSQL_PASSWORD}"
        f"@{settings.MYSQL_HOST}:{settings.MYSQL_PORT}"
    )
    from sqlalchemy import create_engine
    temp_engine = create_engine(base_url)
    
    with temp_engine.connect() as conn:
        conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {settings.MYSQL_DATABASE}"))
        conn.execute(text(f"USE {settings.MYSQL_DATABASE}"))
        conn.commit()
    
    temp_engine.dispose()
    print(f"[OK] Database '{settings.MYSQL_DATABASE}' ready")


def create_tables():
    """Create all tables from SQLAlchemy models."""
    Base.metadata.create_all(bind=engine)
    print("[OK] All tables created successfully")


def drop_tables():
    """Drop all tables (use with caution!)."""
    Base.metadata.drop_all(bind=engine)
    print("[OK] All tables dropped")


def init_db(drop_existing: bool = False):
    """
    Initialize the database.
    
    Args:
        drop_existing: If True, drops all tables before creating.
    """
    print("Initializing Outreach AI database...")
    
    create_database()
    
    if drop_existing:
        print("[WARN] Dropping existing tables...")
        drop_tables()
    
    create_tables()
    print("[OK] Database initialization complete!")


if __name__ == "__main__":
    import sys
    
    drop = "--drop" in sys.argv or "-d" in sys.argv
    init_db(drop_existing=drop)
