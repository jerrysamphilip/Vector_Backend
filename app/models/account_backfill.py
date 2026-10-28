# app/models/account_backfill.py
"""
Company backfill run history (C-14, migration 0024_company_properties).

``AccountBackfillRun`` is one "link contacts to companies" run; ``AccountBackfillItem`` records each thing the
run did so it can be undone:

    ACCOUNT_CREATED     account_id      a company the run created
    ACCOUNT_DOMAIN_SET  account_id      a domain-less company that got its domain (old_value / new_value)
    CONTACT_LINKED      prospect_id     a contact linked to account_id (it had no company before)
    CONTACT_FIELD       prospect_id     a contact field the run filled (field, old_value, new_value)

No foreign keys on purpose: the ledger never blocks deleting or purging a company or a contact; undo checks that
each row still holds what the run wrote before changing it back.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_charset import MYSQL_CHARSET, MYSQL_COLLATION, MYSQL_ENGINE
from app.models.base import Base
from app.models.types import NOW6, UtcDateTime

# Explicit (as apply_table_defaults sets them) so the tables are right even before app.models imports this module
MYSQL_OPTS = {"mysql_engine": MYSQL_ENGINE, "mysql_charset": MYSQL_CHARSET, "mysql_collate": MYSQL_COLLATION}

RUN_STATUSES = ("RUNNING", "COMPLETED", "FAILED", "UNDONE")
ITEM_KINDS = ("ACCOUNT_CREATED", "ACCOUNT_DOMAIN_SET", "CONTACT_LINKED", "CONTACT_FIELD")


class AccountBackfillRun(Base):
    __tablename__ = "account_backfill_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    started_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="RUN", server_default="RUN")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RUNNING", server_default="RUNNING")
    stats: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=NOW6, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    undone_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    undone_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    __table_args__ = (Index("ix_account_backfill_runs_tenant_started", "tenant_id", "started_at"), MYSQL_OPTS)


class AccountBackfillItem(Base):
    __tablename__ = "account_backfill_items"

    item_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    prospect_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    field: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)

    __table_args__ = (Index("ix_account_backfill_items_run_kind", "run_id", "kind"), MYSQL_OPTS)
