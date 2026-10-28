"""
Contact identity records (migration 0025, gap list C-08 / C-10 / C-11 / C-19).

* ``ContactIdentifier`` -- an e-mail address a contact has had that is not its current one: RETIRED
  (the contact changed address) or MERGED (the address belonged to a duplicate merged into this
  contact); ACTIVE when an address was retired and later given back. ``current_prospect_id`` points at
  the contact that answers to the address today, so imports, contact create and reply matching find
  the surviving record. The current address itself lives on ``prospects.email``.
* ``DuplicatePair`` -- a stored "these two contacts look like the same person" suggestion with score,
  confidence (HIGH / MEDIUM) and readable reasons. Status OPEN / DISMISSED ("not the same person";
  never suggested again) / MERGED. No foreign keys: a pair outlives the merge that resolved it.
* ``MergeLog`` -- one row per merge: a snapshot of every record before the merge, the per-property
  choices and what moved. Goes with the surviving contact when it is erased (ON DELETE CASCADE).
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, ForeignKey, Index, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_charset import MYSQL_CHARSET, MYSQL_COLLATION, MYSQL_ENGINE
from app.models.base import Base
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id

_TABLE_OPTIONS = {"mysql_engine": MYSQL_ENGINE, "mysql_charset": MYSQL_CHARSET, "mysql_collate": MYSQL_COLLATION}


def _uuid() -> str:
    return new_id()


class ContactIdentifier(Base):
    __tablename__ = "contact_identifiers"

    identifier_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)  # lower case
    status: Mapped[str] = mapped_column(String(10), nullable=False)  # ACTIVE / RETIRED / MERGED
    # The contact the address belonged to when it was recorded (may since have been merged away)
    original_prospect_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    current_prospect_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("prospects.prospect_id", ondelete="CASCADE", name="fk_contact_identifiers_current"), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # EMAIL_CHANGE / MERGE
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6,
                                                           nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_contact_identifiers_tenant_email"),
        Index("ix_contact_identifiers_current", "current_prospect_id"),
        _TABLE_OPTIONS,
    )


class DuplicatePair(Base):
    __tablename__ = "duplicate_pairs"

    pair_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    contact_a_id: Mapped[str] = mapped_column(String(36), nullable=False)  # contact_a_id < contact_b_id
    contact_b_id: Mapped[str] = mapped_column(String(36), nullable=False)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False)  # HIGH / MEDIUM
    reasons: Mapped[Any] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="OPEN", server_default="OPEN")
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "contact_a_id", "contact_b_id", name="uq_duplicate_pairs_tenant_pair"),
        Index("ix_duplicate_pairs_tenant_status", "tenant_id", "status", "score"),
        Index("ix_duplicate_pairs_contact_b", "contact_b_id"),
        _TABLE_OPTIONS,
    )


class MergeLog(Base):
    __tablename__ = "merge_logs"

    merge_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    survivor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("prospects.prospect_id", ondelete="CASCADE", name="fk_merge_logs_survivor"), nullable=False)
    merged_ids: Mapped[Any] = mapped_column(JSON, nullable=False)
    snapshot: Mapped[Any] = mapped_column(JSON, nullable=False)  # {"survivor": {...}, "merged": [{...}]}
    choices: Mapped[Any] = mapped_column(JSON, nullable=True)  # {field: prospect_id}
    moved: Mapped[Any] = mapped_column(JSON, nullable=True)  # {"lists": n, "campaigns": n, ...}
    merged_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    merged_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)

    __table_args__ = (
        Index("ix_merge_logs_survivor", "survivor_id", "merged_at"),
        Index("ix_merge_logs_tenant", "tenant_id", "merged_at"),
        _TABLE_OPTIONS,
    )
