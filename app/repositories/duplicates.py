"""
Contact identity storage (ADR 0009; gap list C-08 / C-09 / C-10 / C-11 / C-19): stored duplicate pairs,
merge logs, e-mail identifiers, the row locks a merge takes, and e-mail lookups that follow retired and
merged addresses to the contact that answers to them today.

Reached through ``ContactRepository.duplicates`` (app/repositories/contacts.py). Nothing here commits.

E-mail history is kept automatically: a ``before_flush`` listener (``track_email_changes``) records the
old address of every contact whose ``email`` changes through the ORM as RETIRED, pointing at that contact,
so every code path that edits an address (contact edit, prospects API, merge) keeps the history without
calling anything. Set-based ``UPDATE`` statements bypass it; none changes ``prospects.email``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy import event, func, or_
from sqlalchemy import inspect as orm_inspect
from sqlalchemy.orm import Query, Session, aliased

from app.domain.contacts import VisibilityScope
from app.domain.duplicates import Candidate
from app.domain.paging import Page, Paging
from app.models.campaign import CampaignProspect
from app.models.contact_activity import ContactActivity
from app.models.contact_identity import ContactIdentifier, DuplicatePair, MergeLog
from app.models.crm import CrmTask
from app.models.email_message import EmailMessage
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectListMember
from app.models.sales import Lead, Opportunity
from app.repositories.base import TenantId

__all__ = ["DuplicateRepository", "SqlAlchemyDuplicateRepository", "resolve_contact_by_email",
           "track_email_changes"]

IDENTIFIER_STATUSES = ("ACTIVE", "RETIRED", "MERGED")


def _norm(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def _visible(column: Any, scope: VisibilityScope) -> Any:
    """Owner condition for ``scope`` (same rule as app.repositories.contacts.owner_condition)."""
    if scope.owner_ids is None:
        return None
    clause = column.in_(scope.owner_ids)
    return or_(clause, column.is_(None)) if scope.include_unassigned else clause


class DuplicateRepository(Protocol):
    # duplicate pairs
    def candidates(self, tenant_id: TenantId) -> list[Candidate]: ...
    def pairs(self, tenant_id: TenantId) -> list[DuplicatePair]: ...
    def has_pairs(self, tenant_id: TenantId) -> bool: ...
    def add_pair(self, pair: DuplicatePair) -> None: ...
    def delete_pairs(self, pair_ids: Collection[str]) -> int: ...
    def pair(self, tenant_id: TenantId, pair_id: str) -> Optional[DuplicatePair]: ...
    def pair_page(self, tenant_id: TenantId, visibility: VisibilityScope, status: str,
                  confidence: Optional[str], paging: Paging) -> Page[DuplicatePair]: ...
    def resolve_pairs(self, tenant_id: TenantId, contact_ids: Collection[str], user_id: Optional[str],
                      when: datetime) -> int: ...
    def tenants_with_contacts(self) -> list[str]: ...

    # merge
    def lock_contacts(self, tenant_id: TenantId, ids: Collection[str]) -> list[Prospect]: ...
    def contacts_any_state(self, tenant_id: TenantId, ids: Collection[str]) -> list[Prospect]: ...
    def move_counts(self, ids: Collection[str]) -> dict[str, int]: ...
    def add_merge_log(self, log: MergeLog) -> None: ...
    def merge_logs(self, tenant_id: TenantId, survivor_id: str, limit: int) -> list[MergeLog]: ...

    # e-mail identity
    def identifiers_of(self, prospect_id: str) -> list[ContactIdentifier]: ...
    def carry_identities(self, tenant_id: TenantId, survivor_id: str, duplicates: Sequence[tuple[str, str]],
                         keep_email: Optional[str]) -> None: ...
    def resolve_contact_by_email(self, tenant_id: TenantId, email: str) -> Optional[Prospect]: ...


class SqlAlchemyDuplicateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # duplicate pairs -------------------------------------------------------------
    def candidates(self, tenant_id: TenantId) -> list[Candidate]:
        rows = self._session.query(
            Prospect.prospect_id, Prospect.first_name, Prospect.last_name, Prospect.email, Prospect.company_name,
            Prospect.account_id, Prospect.phone, Prospect.mobile_phone, Prospect.linkedin_url,
        ).filter(Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None)).all()
        return [Candidate(*row) for row in rows]

    def pairs(self, tenant_id: TenantId) -> list[DuplicatePair]:
        return self._session.query(DuplicatePair).filter(DuplicatePair.tenant_id == tenant_id).all()

    def has_pairs(self, tenant_id: TenantId) -> bool:
        return self._session.query(DuplicatePair.pair_id).filter(
            DuplicatePair.tenant_id == tenant_id).first() is not None

    def add_pair(self, pair: DuplicatePair) -> None:
        self._session.add(pair)

    def delete_pairs(self, pair_ids: Collection[str]) -> int:
        ids = list(pair_ids)
        deleted = 0
        for start in range(0, len(ids), 1000):
            deleted += self._session.query(DuplicatePair).filter(
                DuplicatePair.pair_id.in_(ids[start:start + 1000])).delete(synchronize_session=False)
        return deleted

    def pair(self, tenant_id: TenantId, pair_id: str) -> Optional[DuplicatePair]:
        return self._session.query(DuplicatePair).filter(DuplicatePair.pair_id == pair_id,
                                                         DuplicatePair.tenant_id == tenant_id).first()

    def pair_page(self, tenant_id: TenantId, visibility: VisibilityScope, status: str,
                  confidence: Optional[str], paging: Paging) -> Page[DuplicatePair]:
        """Pairs whose two contacts are live and visible to the caller, strongest first."""
        a, b = aliased(Prospect), aliased(Prospect)
        query: Query[Any] = self._session.query(DuplicatePair).join(
            a, a.prospect_id == DuplicatePair.contact_a_id).join(b, b.prospect_id == DuplicatePair.contact_b_id).filter(
            DuplicatePair.tenant_id == tenant_id, DuplicatePair.status == status,
            a.tenant_id == tenant_id, b.tenant_id == tenant_id, a.deleted_at.is_(None), b.deleted_at.is_(None))
        for alias in (a, b):
            clause = _visible(alias.owner_id, visibility)
            if clause is not None:
                query = query.filter(clause)
        if confidence:
            query = query.filter(DuplicatePair.confidence == confidence)
        total = query.with_entities(func.count(DuplicatePair.pair_id)).scalar() or 0
        rows = query.order_by(DuplicatePair.score.desc(), DuplicatePair.created_at.desc(),
                              DuplicatePair.pair_id).offset(paging.offset).limit(paging.limit).all()
        return Page(items=rows, total=total, paging=paging)

    def resolve_pairs(self, tenant_id: TenantId, contact_ids: Collection[str], user_id: Optional[str],
                      when: datetime) -> int:
        """Pairs touching merged-away contacts are done: status MERGED."""
        ids = list(contact_ids)
        return self._session.query(DuplicatePair).filter(
            DuplicatePair.tenant_id == tenant_id, DuplicatePair.status != "MERGED",
            or_(DuplicatePair.contact_a_id.in_(ids), DuplicatePair.contact_b_id.in_(ids)),
        ).update({DuplicatePair.status: "MERGED", DuplicatePair.resolved_at: when,
                  DuplicatePair.resolved_by: user_id}, synchronize_session=False)

    def tenants_with_contacts(self) -> list[str]:
        return [t for (t,) in self._session.query(Prospect.tenant_id).filter(
            Prospect.deleted_at.is_(None)).distinct().all()]

    # merge -----------------------------------------------------------------------
    def lock_contacts(self, tenant_id: TenantId, ids: Collection[str]) -> list[Prospect]:
        """SELECT ... FOR UPDATE on every contact of a merge (in id order), refreshing loaded objects, so
        two merges or a merge and an edit of the same contacts run one after the other."""
        return self._session.query(Prospect).filter(
            Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(list(ids)),
        ).order_by(Prospect.prospect_id).with_for_update().populate_existing().all()

    def contacts_any_state(self, tenant_id: TenantId, ids: Collection[str]) -> list[Prospect]:
        return self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id,
                                                    Prospect.prospect_id.in_(list(ids))).all()

    def move_counts(self, ids: Collection[str]) -> dict[str, int]:
        """What moves to the survivor when these contacts are merged into it."""
        db, ids = self._session, list(ids)

        def count(column: Any) -> int:
            return db.query(func.count()).filter(column.in_(ids)).scalar() or 0

        return {
            "lists": count(ProspectListMember.prospect_id),
            "campaigns": count(CampaignProspect.prospect_id),
            "emails": count(EmailMessage.prospect_id),
            "activities": count(ContactActivity.prospect_id),
            "tasks": count(CrmTask.prospect_id),
            "leads": count(Lead.prospect_id),
            "deals": count(Opportunity.prospect_id),
        }

    def add_merge_log(self, log: MergeLog) -> None:
        self._session.add(log)

    def merge_logs(self, tenant_id: TenantId, survivor_id: str, limit: int) -> list[MergeLog]:
        return self._session.query(MergeLog).filter(MergeLog.tenant_id == tenant_id,
                                                    MergeLog.survivor_id == survivor_id).order_by(
            MergeLog.merged_at.desc()).limit(limit).all()

    # e-mail identity --------------------------------------------------------------
    def identifiers_of(self, prospect_id: str) -> list[ContactIdentifier]:
        return self._session.query(ContactIdentifier).filter(
            ContactIdentifier.current_prospect_id == prospect_id).order_by(
            ContactIdentifier.updated_at.desc()).all()

    def carry_identities(self, tenant_id: TenantId, survivor_id: str, duplicates: Sequence[tuple[str, str]],
                         keep_email: Optional[str]) -> None:
        """Before the duplicates are deleted: their past addresses now point at the survivor, and each
        duplicate's own address is recorded as MERGED into it (except ``keep_email``, which the survivor
        takes as its current address)."""
        dup_ids = [pid for pid, _ in duplicates]
        self._session.query(ContactIdentifier).filter(
            ContactIdentifier.current_prospect_id.in_(dup_ids)).update(
            {ContactIdentifier.current_prospect_id: survivor_id}, synchronize_session=False)
        keep = _norm(keep_email)
        for pid, email in duplicates:
            if _norm(email) and _norm(email) != keep:
                upsert_identifier(self._session, tenant_id, email, "MERGED", survivor_id, pid, "MERGE")

    def resolve_contact_by_email(self, tenant_id: TenantId, email: str) -> Optional[Prospect]:
        return resolve_contact_by_email(self._session, tenant_id, email)


# ── E-mail identity helpers (module level: also used by the flush listener) ───────

def resolve_contact_by_email(session: Session, tenant_id: TenantId, email: str) -> Optional[Prospect]:
    """The contact (live or in the recycle bin) whose current address is ``email``; otherwise the contact a
    retired or merged address now belongs to (C-11). None when nobody has had it."""
    address = _norm(email)
    if not address:
        return None
    with session.no_autoflush:
        prospect = session.query(Prospect).filter(Prospect.tenant_id == tenant_id, Prospect.email == address).first()
        if prospect is not None:
            return prospect
        ident = session.query(ContactIdentifier).filter(ContactIdentifier.tenant_id == tenant_id,
                                                        ContactIdentifier.email == address).first()
        if ident is None:
            return None
        return session.query(Prospect).filter(Prospect.prospect_id == ident.current_prospect_id,
                                              Prospect.tenant_id == tenant_id).first()


def _pending_identifier(session: Session, tenant_id: str, address: str) -> Optional[ContactIdentifier]:
    for obj in session.new:
        if isinstance(obj, ContactIdentifier) and obj.tenant_id == tenant_id and obj.email == address:
            return obj
    return None


def upsert_identifier(session: Session, tenant_id: str, email: str, status: str, current_id: str,
                      original_id: Optional[str], source: str) -> None:
    """Record ``email`` for ``current_id`` (one row per workspace and address)."""
    address = _norm(email)[:255]
    with session.no_autoflush:
        row = _pending_identifier(session, tenant_id, address) or session.query(ContactIdentifier).filter(
            ContactIdentifier.tenant_id == tenant_id, ContactIdentifier.email == address).first()
    if row is None:
        session.add(ContactIdentifier(tenant_id=tenant_id, email=address, status=status,
                                      current_prospect_id=current_id, original_prospect_id=original_id,
                                      source=source))
        return
    row.status, row.current_prospect_id, row.source = status, current_id, source
    if original_id and not row.original_prospect_id:
        row.original_prospect_id = original_id


def _email_change(prospect: Prospect) -> Optional[tuple[str, str]]:
    history = orm_inspect(prospect).attrs.email.history
    if not history.deleted or not history.added:
        return None
    old, new = _norm(history.deleted[0]), _norm(history.added[0])
    if not old or old == new:
        return None
    return old, new


def _changed_emails(session: Session) -> Iterable[tuple[Prospect, str, str]]:
    for obj in list(session.dirty):
        if isinstance(obj, Prospect) and obj.tenant_id:
            change = _email_change(obj)
            if change:
                yield obj, change[0], change[1]


@event.listens_for(Session, "before_flush")
def track_email_changes(session: Session, flush_context: Any, instances: Any) -> None:
    """A contact's old address is RETIRED (still pointing at it) when its e-mail changes; an address it
    gets back is ACTIVE again (C-11)."""
    for prospect, old, new in _changed_emails(session):
        upsert_identifier(session, prospect.tenant_id, old, "RETIRED", prospect.prospect_id,
                          prospect.prospect_id, "EMAIL_CHANGE")
        with session.no_autoflush:
            had_new = _pending_identifier(session, prospect.tenant_id, new) or session.query(
                ContactIdentifier).filter(ContactIdentifier.tenant_id == prospect.tenant_id,
                                          ContactIdentifier.email == new).first()
        if had_new is not None:
            had_new.status, had_new.current_prospect_id, had_new.source = "ACTIVE", prospect.prospect_id, "EMAIL_CHANGE"
