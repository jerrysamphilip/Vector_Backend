"""
Duplicate review, merge preview and contact identity history (BR-CM-32/33; gap list C-08, C-09, C-10,
C-11, C-19, F-21). Serves app/routers/duplicates_router.py, one unit of work per method.

* review list: stored pairs (OPEN / DISMISSED / MERGED) whose two contacts the caller can see, with
  score, confidence and reasons; dismiss ("not the same person", never suggested again) and reopen;
* on-demand scan (the worker also rescans: ``duplicate_scan.run_scan_job``);
* merge preview: every mergeable property per record, whether they differ, the default choice, what moves
  and warnings -- refused (409) when a contact is in the recycle bin or already merged;
* merge history (``merge_logs``) and past e-mail addresses (``contact_identifiers``) of one contact.

The merge itself is ContactWriteService.merge -> contact_service.merge_into (locks, merge log, identities).
Managing duplicates needs the manage_prospects permission, like merging.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from app.core.clock import utcnow
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.domain.duplicates import default_choice, values_differ
from app.domain.paging import Paging
from app.models.contact_identity import ContactIdentifier, DuplicatePair, MergeLog
from app.models.prospect import Prospect
from app.models.user import User
from app.repositories.contacts import ContactsUnitOfWork, Session, contacts_uow
from app.services import duplicate_scan
from app.services.contact_query_service import contact_summaries, find_contact, user_name
from app.services.contact_service import (
    MERGE_CHOOSABLE,
    MERGE_REFUSED,
    can_access_contact,
    merge_refusal,
    require_manager,
    visibility_scope,
)

__all__ = ["MERGE_LABELS", "DuplicateService", "merge_warnings"]

PAIR_STATUSES = ("OPEN", "DISMISSED", "MERGED")
MERGE_LABELS = {
    "email": "Email", "first_name": "First name", "last_name": "Last name", "phone": "Phone",
    "mobile_phone": "Mobile", "designation": "Job title", "company_name": "Company", "account_id": "Primary company",
    "owner_id": "Owner", "industry": "Industry", "emp_band": "Employees", "linkedin_url": "LinkedIn",
    "poc_city": "City", "poc_state": "State", "poc_country": "Country", "timezone": "Time zone",
    "lifecycle_stage": "Lifecycle stage", "lead_status": "Lead status", "lead_source": "Lead source",
    "legal_basis": "Legal basis",
}
_DISPLAY = {"owner_id": "owner_name", "account_id": "account_name"}


def _distinct(values: Sequence[Any]) -> int:
    return len({str(v).strip().lower() for v in values if v not in (None, "")})


def merge_warnings(records: Sequence[Prospect]) -> list[str]:
    """What a person should know before merging (survivor first)."""
    warnings = []
    if any(p.consent_status == "UNSUBSCRIBED" for p in records):
        warnings.append("One of the records is unsubscribed: the merged contact stays unsubscribed.")
    if _distinct([p.owner_id for p in records]) > 1:
        warnings.append("The records have different owners: the owner you keep owns everything that moves.")
    if _distinct([p.account_id for p in records]) > 1:
        warnings.append("The records belong to different companies: the other companies stay as associated "
                        "companies.")
    if _distinct([p.email for p in records]) > 1:
        warnings.append("The other e-mail addresses are kept in the contact's e-mail history, so replies and "
                        "imports from them still find the merged contact.")
    warnings.append("The other records are removed. The merge is logged with a copy of every record.")
    return warnings


def _property_rows(records: Sequence[Prospect], summaries: Sequence[dict]) -> list[dict]:
    rows = []
    for field in MERGE_CHOOSABLE:
        values = [(p.prospect_id, getattr(p, field)) for p in records]
        display_key = _DISPLAY.get(field, field)
        rows.append({
            "field": field, "label": MERGE_LABELS.get(field, field),
            "values": [{"prospect_id": pid, "value": value, "display": s.get(display_key) or value}
                       for (pid, value), s in zip(values, summaries, strict=True)],
            "differs": values_differ(v for _, v in values),
            "default": default_choice(values),
        })
    return rows


def identifier_dict(row: ContactIdentifier) -> dict:
    return {"email": row.email, "status": row.status, "source": row.source,
            "original_prospect_id": row.original_prospect_id, "since": row.created_at, "updated_at": row.updated_at}


def merge_log_dict(log: MergeLog, names: dict[str, Optional[str]]) -> dict:
    merged = (log.snapshot or {}).get("merged") or []
    return {
        "merge_id": log.merge_id, "merged_at": log.merged_at, "merged_by": log.merged_by,
        "merged_by_name": names.get(log.merged_by or ""), "merged_ids": log.merged_ids,
        "merged": [{"prospect_id": m.get("prospect_id"), "email": m.get("email"),
                    "name": " ".join(x for x in (m.get("first_name"), m.get("last_name")) if x) or None}
                   for m in merged],
        "choices": log.choices or {}, "moved": log.moved or {}, "snapshot": log.snapshot,
    }


class DuplicateService:
    def __init__(self, uow: ContactsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> DuplicateService:
        return cls(contacts_uow(db))

    # ── review list ────────────────────────────────────────────────
    def pairs(self, user: User, status: str, confidence: Optional[str], paging: Paging) -> dict:
        require_manager(user)
        if status not in PAIR_STATUSES:
            raise BadRequestError("status must be OPEN, DISMISSED or MERGED")
        with self.uow as uow:
            repo, store = uow.contacts, uow.contacts.duplicates
            page = store.pair_page(user.tenant_id, visibility_scope(uow.session, user), status, confidence, paging)
            ids = {i for p in page.items for i in (p.contact_a_id, p.contact_b_id)}
            by_id = {s["prospect_id"]: s for s in contact_summaries(repo, repo.prospects_by_ids(user.tenant_id, ids))}
            names = {u: user_name(v) for u, v in repo.users_by_ids(
                {p.resolved_by for p in page.items if p.resolved_by}).items()}
            items = []
            for pair in page.items:
                group = duplicate_scan.pair_group(pair, by_id)
                group.update(created_at=pair.created_at, resolved_at=pair.resolved_at,
                             resolved_by_name=names.get(pair.resolved_by or ""))
                items.append(group)
            return {"items": items, "total": page.total, "page": paging.page, "page_size": paging.page_size}

    def scan(self, user: User) -> dict:
        """Rescan the workspace now (managers); returns what changed."""
        require_manager(user)
        with self.uow as uow:
            stats = duplicate_scan.scan_tenant(uow.contacts.duplicates, user.tenant_id, utcnow())
            uow.commit()
            return stats

    def _pair_for(self, uow: ContactsUnitOfWork, user: User, pair_id: str) -> DuplicatePair:
        pair = uow.contacts.duplicates.pair(user.tenant_id, pair_id)
        if pair is None:
            raise NotFoundError("Duplicate suggestion not found")
        for pid in (pair.contact_a_id, pair.contact_b_id):
            find_contact(uow, user, pid)  # 404 unless the caller sees both contacts
        return pair

    def _set_status(self, user: User, pair_id: str, status: str, allowed_from: str) -> dict:
        require_manager(user)
        with self.uow as uow:
            pair = self._pair_for(uow, user, pair_id)
            if pair.status != allowed_from:
                raise ConflictError(f"This suggestion is {pair.status.lower()}, not {allowed_from.lower()}.")
            pair.status = status
            pair.resolved_at = utcnow() if status == "DISMISSED" else None
            pair.resolved_by = user.user_id if status == "DISMISSED" else None
            uow.commit()
            return {"pair_id": pair.pair_id, "status": pair.status}

    def dismiss(self, user: User, pair_id: str) -> dict:
        """ "Not the same person": the pair is never suggested again (until reopened)."""
        return self._set_status(user, pair_id, "DISMISSED", "OPEN")

    def reopen(self, user: User, pair_id: str) -> dict:
        return self._set_status(user, pair_id, "OPEN", "DISMISSED")

    # ── merge preview ───────────────────────────────────────────────
    def merge_preview(self, user: User, primary_id: str, duplicate_ids: Sequence[str]) -> dict:
        require_manager(user)
        dup_ids = [i for i in dict.fromkeys(duplicate_ids) if i != primary_id]
        if not dup_ids:
            raise BadRequestError("Choose at least one other contact to merge")
        ids = [primary_id] + dup_ids
        with self.uow as uow:
            repo, store = uow.contacts, uow.contacts.duplicates
            found = {p.prospect_id: p for p in store.contacts_any_state(user.tenant_id, ids)}
            if len(found) != len(ids) or not all(can_access_contact(user, p) for p in found.values()):
                raise NotFoundError("One or more contacts to merge were not found")
            refusal = merge_refusal(found.values(), ids, MERGE_REFUSED)
            if refusal:
                raise ConflictError(refusal)
            records = [found[i] for i in ids]
            summaries = contact_summaries(repo, records)
            tags = sorted({t for p in records for t in (p.tags or [])}, key=str.lower)
            return {
                "survivor": summaries[0], "merging": summaries[1:],
                "properties": _property_rows(records, summaries),
                "moves": store.move_counts(dup_ids), "warnings": merge_warnings(records), "tags": tags,
            }

    # ── history ──────────────────────────────────────────────────────
    def merge_history(self, user: User, prospect_id: str, limit: int) -> dict:
        with self.uow as uow:
            prospect = find_contact(uow, user, prospect_id)
            logs = uow.contacts.duplicates.merge_logs(user.tenant_id, prospect.prospect_id, limit)
            users = uow.contacts.users_by_ids({log.merged_by for log in logs if log.merged_by})
            names = {uid: user_name(u) for uid, u in users.items()}
            return {"items": [merge_log_dict(log, names) for log in logs]}

    def email_history(self, user: User, prospect_id: str) -> dict:
        """The contact's current address and every address it had (retired) or took over (merged)."""
        with self.uow as uow:
            prospect = find_contact(uow, user, prospect_id)
            rows = uow.contacts.duplicates.identifiers_of(prospect.prospect_id)
            current = (prospect.email or "").lower()
            return {"current": prospect.email,
                    "items": [identifier_dict(r) for r in rows if r.email != current or r.status != "ACTIVE"]}

