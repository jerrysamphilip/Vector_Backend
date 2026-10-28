# app/models/crm.py
"""
CRM tables added for Phase 1 Contact Management (BRD v2.0, section 5.2):
property change history, tasks, saved views and import jobs.
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, Computed, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.status_checks import status_check
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id

MYSQL_OPTS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class PropertyChange(Base):
    """Every change to a contact or company property (BR-CM-11)."""
    __tablename__ = "property_changes"

    change_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    object_type: Mapped[str] = mapped_column(String(10), nullable=False)  # CONTACT / COMPANY
    object_id: Mapped[str] = mapped_column(String(36), nullable=False)
    field: Mapped[str] = mapped_column(String(100), nullable=False)
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="UI")  # UI / BULK / IMPORT / MERGE / API / SYSTEM
    changed_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    # Microsecond precision keeps changes made in the same second in order
    changed_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=NOW6, nullable=False)

    __table_args__ = (Index("ix_property_changes_object", "object_type", "object_id", "changed_at"), MYSQL_OPTS)


class CrmTask(Base):
    """To-dos on contacts and companies (BR-CM-15)."""
    __tablename__ = "crm_tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    prospect_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("prospects.prospect_id"), nullable=True, index=True)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("accounts.account_id"), nullable=True, index=True)
    opportunity_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("opportunities.opportunity_id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_type: Mapped[str] = mapped_column(String(20), nullable=False, default="TODO")  # TODO / CALL / EMAIL / MEETING
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="MEDIUM")  # LOW / MEDIUM / HIGH
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="OPEN")  # OPEN / DONE
    due_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    reminder_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True, index=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6, nullable=True)

    __table_args__ = (MYSQL_OPTS,)


class SavedView(Base):
    """A saved table view: filters, columns and sort, personal or shared (BR-CM-19)."""
    __tablename__ = "saved_views"

    view_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    object_type: Mapped[str] = mapped_column(String(10), nullable=False, default="CONTACT")  # CONTACT / COMPANY
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    filters: Mapped[Any] = mapped_column(JSON, nullable=True)
    columns: Mapped[Any] = mapped_column(JSON, nullable=True)
    sort: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6, nullable=True)

    __table_args__ = (MYSQL_OPTS,)


class ImportJob(Base):
    """One CSV/XLSX import with its outcome and rejected rows (BR-CM-25..30)."""
    __tablename__ = "import_jobs"

    import_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="RUNNING")  # RUNNING / COMPLETED / FAILED
    mapping: Mapped[Any] = mapped_column(JSON, nullable=True)
    options: Mapped[Any] = mapped_column(JSON, nullable=True)
    total_rows: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    contacts_created: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    contacts_updated: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    companies_created: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    companies_updated: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    error_count: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    errors: Mapped[Any] = mapped_column(JSON, nullable=True)  # [{row, reason, values}]
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    # Background imports (C-03, F-16; migration 0026): the stored file, checkpoint and worker heartbeat.
    # status also takes UPLOADED / QUEUED / CANCELLED (app/domain/imports.py JobStatus)
    mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # CREATE / UPDATE_ONLY / CREATE_AND_UPDATE
    processed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_count: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    file_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # relative to IMPORT_STORAGE_DIR
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sheet_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    queued_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    claimed_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        status_check("import_jobs", "status"),  # D5, 0029_status_checks
        Index("ix_import_jobs_status_heartbeat", "status", "heartbeat_at"),
        MYSQL_OPTS,
    )


# The contact on its primary link row, NULL otherwise (C-22). Literal copy in
# alembic/versions/0028_child_tenant_ids.py: keep the two identical.
PRIMARY_KEY_SQL = "CASE WHEN is_primary THEN prospect_id END"


class ContactCompany(Base):
    """A contact's association with a company, with a label (BR-CM, CM-014).

    prospects.account_id stays the contact's primary company; its link row has is_primary set
    and mirrors it. Other rows are secondary associations ("Billing", "Decision maker", ...).
    """
    __tablename__ = "contact_companies"

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    prospect_id: Mapped[str] = mapped_column(String(36), ForeignKey("prospects.prospect_id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.account_id", ondelete="CASCADE"), nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6, nullable=True)
    # Generated (VIRTUAL) key behind ux_contact_companies_one_primary: the contact on its primary row, NULL on
    # the others, so a contact has at most one primary company (C-22). Never written by the application.
    primary_prospect_id: Mapped[Optional[str]] = mapped_column(
        String(36), Computed(PRIMARY_KEY_SQL, persisted=False), nullable=True)

    __table_args__ = (
        UniqueConstraint("prospect_id", "account_id", name="uq_contact_companies_contact_account"),
        Index("ix_contact_companies_account", "account_id"),
        Index("ix_contact_companies_tenant", "tenant_id"),
        # Contact and company of the same tenant as the row (C-22, 0028_child_tenant_ids)
        Index("ix_contact_companies_tenant_prospect", "tenant_id", "prospect_id"),
        Index("ix_contact_companies_tenant_account", "tenant_id", "account_id"),
        ForeignKeyConstraint(["tenant_id", "prospect_id"], ["prospects.tenant_id", "prospects.prospect_id"],
                             name="fk_contact_companies_tenant_prospect", ondelete="CASCADE"),
        ForeignKeyConstraint(["tenant_id", "account_id"], ["accounts.tenant_id", "accounts.account_id"],
                             name="fk_contact_companies_tenant_account", ondelete="CASCADE"),
        Index("ux_contact_companies_one_primary", "primary_prospect_id", unique=True),
        MYSQL_OPTS,
    )


class NoteMention(Base):
    """A colleague @mentioned in a note (BR-CM-16, CM-033); they get an in-app notification."""
    __tablename__ = "note_mentions"

    mention_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    activity_id: Mapped[str] = mapped_column(String(36), ForeignKey("contact_activities.activity_id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)

    __table_args__ = (
        UniqueConstraint("activity_id", "user_id", name="uq_note_mentions_activity_user"),
        Index("ix_note_mentions_user", "user_id"),
        Index("ix_note_mentions_tenant", "tenant_id"),
        MYSQL_OPTS,
    )
