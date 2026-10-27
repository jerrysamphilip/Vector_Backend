# app/models/crm.py
"""
CRM tables added for Phase 1 Contact Management (BRD v2.0, section 5.2):
property change history, tasks, saved views and import jobs.
"""

import uuid

from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime

MYSQL_OPTS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class PropertyChange(Base):
    """Every change to a contact or company property (BR-CM-11)."""
    __tablename__ = "property_changes"

    change_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    object_type = Column(String(10), nullable=False)  # CONTACT / COMPANY
    object_id = Column(String(36), nullable=False)
    field = Column(String(100), nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    source = Column(String(20), nullable=False, default="UI")  # UI / BULK / IMPORT / MERGE / API / SYSTEM
    changed_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    # Microsecond precision keeps changes made in the same second in order
    changed_at = Column(UtcDateTime, server_default=NOW6, nullable=False)

    __table_args__ = (Index("ix_property_changes_object", "object_type", "object_id", "changed_at"), MYSQL_OPTS)


class CrmTask(Base):
    """To-dos on contacts and companies (BR-CM-15)."""
    __tablename__ = "crm_tasks"

    task_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    prospect_id = Column(String(36), ForeignKey("prospects.prospect_id"), nullable=True, index=True)
    account_id = Column(String(36), ForeignKey("accounts.account_id"), nullable=True, index=True)
    opportunity_id = Column(String(36), ForeignKey("opportunities.opportunity_id"), nullable=True, index=True)
    title = Column(String(255), nullable=False)
    notes = Column(Text, nullable=True)
    task_type = Column(String(20), nullable=False, default="TODO")  # TODO / CALL / EMAIL / MEETING
    priority = Column(String(10), nullable=False, default="MEDIUM")  # LOW / MEDIUM / HIGH
    status = Column(String(10), nullable=False, default="OPEN")  # OPEN / DONE
    due_at = Column(UtcDateTime, nullable=True)
    reminder_at = Column(UtcDateTime, nullable=True)
    owner_id = Column(String(36), ForeignKey("users.user_id"), nullable=True, index=True)
    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    completed_at = Column(UtcDateTime, nullable=True)
    created_at = Column(UtcDateTime, server_default=NOW6)
    updated_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)

    __table_args__ = (MYSQL_OPTS,)


class SavedView(Base):
    """A saved table view: filters, columns and sort, personal or shared (BR-CM-19)."""
    __tablename__ = "saved_views"

    view_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    object_type = Column(String(10), nullable=False, default="CONTACT")  # CONTACT / COMPANY
    name = Column(String(100), nullable=False)
    owner_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    shared = Column(Boolean, nullable=False, default=False)
    filters = Column(JSON, nullable=True)
    columns = Column(JSON, nullable=True)
    sort = Column(JSON, nullable=True)
    created_at = Column(UtcDateTime, server_default=NOW6)
    updated_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)

    __table_args__ = (MYSQL_OPTS,)


class ImportJob(Base):
    """One CSV/XLSX import with its outcome and rejected rows (BR-CM-25..30)."""
    __tablename__ = "import_jobs"

    import_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    file_name = Column(String(255), nullable=False)
    status = Column(String(15), nullable=False, default="RUNNING")  # RUNNING / COMPLETED / FAILED
    mapping = Column(JSON, nullable=True)
    options = Column(JSON, nullable=True)
    total_rows = Column(Integer, default=0)
    contacts_created = Column(Integer, default=0)
    contacts_updated = Column(Integer, default=0)
    companies_created = Column(Integer, default=0)
    companies_updated = Column(Integer, default=0)
    error_count = Column(Integer, default=0)
    errors = Column(JSON, nullable=True)  # [{row, reason, values}]
    message = Column(Text, nullable=True)
    started_at = Column(UtcDateTime, server_default=NOW6)
    finished_at = Column(UtcDateTime, nullable=True)

    __table_args__ = (MYSQL_OPTS,)


class ContactCompany(Base):
    """A contact's association with a company, with a label (BR-CM, CM-014).

    prospects.account_id stays the contact's primary company; its link row has is_primary set
    and mirrors it. Other rows are secondary associations ("Billing", "Decision maker", ...).
    """
    __tablename__ = "contact_companies"

    link_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    prospect_id = Column(String(36), ForeignKey("prospects.prospect_id", ondelete="CASCADE"), nullable=False)
    account_id = Column(String(36), ForeignKey("accounts.account_id", ondelete="CASCADE"), nullable=False)
    label = Column(String(100), nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False)
    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at = Column(UtcDateTime, server_default=NOW6)
    updated_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)

    __table_args__ = (
        UniqueConstraint("prospect_id", "account_id", name="uq_contact_companies_contact_account"),
        Index("ix_contact_companies_account", "account_id"),
        Index("ix_contact_companies_tenant", "tenant_id"),
        MYSQL_OPTS,
    )


class NoteMention(Base):
    """A colleague @mentioned in a note (BR-CM-16, CM-033); they get an in-app notification."""
    __tablename__ = "note_mentions"

    mention_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    activity_id = Column(String(36), ForeignKey("contact_activities.activity_id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at = Column(UtcDateTime, server_default=NOW6)

    __table_args__ = (
        UniqueConstraint("activity_id", "user_id", name="uq_note_mentions_activity_user"),
        Index("ix_note_mentions_user", "user_id"),
        Index("ix_note_mentions_tenant", "tenant_id"),
        MYSQL_OPTS,
    )
