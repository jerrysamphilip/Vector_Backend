"""
Upload lists on the Prospects page (/prospect-lists): paged lists with member counts, a list's contacts,
per-list notes, enrolling lists into a campaign, list re-validation, deleting an upload list (and the
contacts that only existed for it), single-contact edit / delete and the prospect KPI cards.
Serves app/routers/prospect_list_router.py (ADR 0009, chunk A1); one unit of work per method.

Behaviour is the one the routes always had; only the SQL moved to app/repositories/lists.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Optional

from app.core.clock import utcnow
from app.core.errors import BadRequestError, NotFoundError
from app.domain.contacts import FilterError
from app.domain.lists import (
    ProspectListFilter,
    RevalidationOutcome,
    duplicate_emails,
    unsubscribed_issue,
)
from app.domain.paging import Paging
from app.models.campaign import CampaignProspect
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectList
from app.models.user import User
from app.repositories.lists import ListsUnitOfWork, Session, lists_uow
from app.schemas.contact_schema import ContactUpdate
from app.services import crm, enrollment_rules
from app.services.campaign_email_service import CampaignEmailService
from app.services.contact_service import delete_contacts
from app.services.contact_write_service import update_contact
from app.utils.business_calendar import get_timezone_for_state
from app.utils.email_utils import PERSONAL_EMAIL_DOMAINS, parse_email

__all__ = ["ProspectListService", "revalidate_prospect"]

ENROLLABLE_STATUSES = ("DRAFT", "PAUSED", "ACTIVE")


def _check_email(prospect: Prospect, outcome: RevalidationOutcome) -> bool:
    """Email format and type; fixes email_type / email_provider. True when the row changed."""
    info = parse_email(prospect.email)
    if info:
        if prospect.email_type != info.get("email_type") or prospect.email_provider != info.get("email_provider"):
            prospect.email_type = info.get("email_type")
            prospect.email_provider = info.get("email_provider")
            return True
        return False
    # parse_email returns None for invalid OR personal emails
    if prospect.email and "@" in prospect.email:
        domain = prospect.email.split("@")[-1].lower()
        if domain in PERSONAL_EMAIL_DOMAINS:
            outcome.warn("Personal email domain detected")
            if prospect.email_type != "PERSONAL":
                prospect.email_type = "PERSONAL"
                prospect.email_provider = domain
                return True
            return False
    outcome.fail("Invalid email format")
    return False


def _check_state(prospect: Prospect, outcome: RevalidationOutcome) -> bool:
    """POC State present; the timezone follows it. True when the row changed."""
    if not prospect.poc_state or not str(prospect.poc_state).strip():
        outcome.warn("Missing POC State")
        return False
    expected = get_timezone_for_state(prospect.poc_state)
    if prospect.timezone != expected:
        prospect.timezone = expected
        return True
    return False


def revalidate_prospect(prospect: Prospect, unsubscribed: dict[str, Optional[str]],
                        duplicates: set[str]) -> tuple[RevalidationOutcome, bool]:
    """Check one contact of an upload list; returns the outcome and whether its metadata was updated."""
    outcome = RevalidationOutcome()
    updated = _check_email(prospect, outcome)
    email = (prospect.email or "").lower()
    if email and email in unsubscribed:
        outcome.fail(unsubscribed_issue(unsubscribed.get(email)))
    if email and email in duplicates:
        outcome.warn("Duplicate email in list")
    updated = _check_state(prospect, outcome) or updated
    return outcome, updated


class ProspectListService:
    def __init__(self, uow: ListsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> ProspectListService:
        return cls(lists_uow(db))

    @staticmethod
    def _get(uow: ListsUnitOfWork, user: User, list_id: str) -> ProspectList:
        plist = uow.lists.get(user.tenant_id, list_id)
        if not plist:
            raise NotFoundError("List not found")
        return plist

    @staticmethod
    def _count(uow: ListsUnitOfWork, user: User, plist: ProspectList) -> int:
        if plist.list_type != "ACTIVE":
            return uow.lists.static_live_count(plist.list_id)
        try:
            return uow.lists.active_count(crm.list_scope(uow.session, plist, user))
        except FilterError:
            return 0

    # ── reads ────────────────────────────────────────────────────────
    def page(self, user: User, f: ProspectListFilter, paging: Paging) -> dict:
        with self.uow as uow:
            page = uow.lists.upload_page(user.tenant_id, f, paging)
            items = [{"list_id": lst.list_id, "list_name": lst.list_name, "source_type": lst.source_type,
                      "prospect_count": self._count(uow, user, lst), "uploaded_at": lst.uploaded_at}
                     for lst in page.items]
            return {"items": items, "total": page.total, "page": paging.page, "page_size": paging.page_size}

    def prospects(self, user: User, list_id: str, search: Optional[str], paging: Paging) -> dict:
        with self.uow as uow:
            plist = self._get(uow, user, list_id)
            page = uow.lists.list_prospects(crm.list_scope(uow.session, plist, user), search, paging)
            return {
                "list_id": list_id, "list_name": plist.list_name, "total": page.total,
                "page": paging.page, "page_size": paging.page_size,
                "prospects": [{
                    "prospect_id": p.prospect_id, "first_name": p.first_name, "last_name": p.last_name,
                    "email": p.email, "company_name": p.company_name, "designation": p.designation,
                    "poc_city": p.poc_city, "poc_state": p.poc_state, "poc_country": p.poc_country,
                    "consent_status": p.consent_status, "notes": notes, "row_version": p.row_version,
                } for p, notes in page.items],
            }

    def stats(self, user: User) -> dict:
        with self.uow as uow:
            total, available, active, opted_out = uow.lists.prospect_stats(user.tenant_id)
            return {"total": total, "available": available, "active": active, "opted_out": opted_out}

    # ── enrollment ───────────────────────────────────────────────────
    def enroll(self, user: User, campaign_id: str, list_ids: Sequence[str]) -> dict:
        """Enroll the lists' contacts into a campaign after the safety checks (unsubscribed, consent,
        already enrolled, invalid email and the campaign's own rules)."""
        with self.uow as uow:
            repo, db = uow.lists, uow.session
            campaign = repo.campaign(user.tenant_id, campaign_id)
            if not campaign:
                raise NotFoundError("Campaign not found")
            valid = repo.tenant_list_ids(user.tenant_id, list_ids)
            invalid = [lid for lid in list_ids if lid not in valid]
            if invalid:
                raise NotFoundError(f"Prospect list(s) not found in your workspace: {', '.join(invalid)}")
            member_ids = enrollment_rules.list_member_ids(db, user.tenant_id, valid)
            prospects, rejections = enrollment_rules.load_tenant_prospects(db, user.tenant_id, member_ids)
            eligible, screened_out = enrollment_rules.screen(db, user.tenant_id, campaign_id, prospects)
            rejections.extend(screened_out)
            if campaign.status not in ENROLLABLE_STATUSES:
                raise BadRequestError("Cannot enroll prospects in current campaign status")
            step1 = repo.first_step(campaign_id)
            now = utcnow()
            first_at = now + timedelta(days=step1.wait_days if step1 and step1.wait_days else 0)
            for prospect in eligible:
                repo.stage(CampaignProspect(campaign_id=campaign_id, prospect_id=prospect.prospect_id, current_step=1,
                                            status="ACTIVE", enrolled_at=now, next_scheduled_at=first_at))
            repo.flush()
            if eligible and campaign.status in ("ACTIVE", "PAUSED"):
                # Already launched: create the new contacts' emails now, as single enrollment does;
                # the scheduler only sends existing EmailMessage rows (launch made them for the rest)
                CampaignEmailService(db)._preschedule_all_emails(campaign_id)
            uow.commit()
        counts = enrollment_rules.summarize(rejections)
        return {
            "total_checked": len(prospects) + counts.get("not_found", 0),
            "enrolled": len(eligible),
            "rejected": {
                "global_unsubscribe": counts.get("unsubscribed", 0),
                "no_consent": counts.get("opted_out", 0),
                "already_enrolled": counts.get("already_enrolled", 0),
                "invalid_email": counts.get("invalid_email", 0),
            },
            "rejections": rejections[:enrollment_rules.MAX_REJECTIONS_RETURNED],
        }

    # ── writes ───────────────────────────────────────────────────────
    def delete(self, user: User, list_id: str) -> dict:
        """Delete an upload list and its memberships. Contacts in no other list and with no logged
        calls / meetings / notes only existed for the upload: they are deleted with it."""
        with self.uow as uow:
            repo = uow.lists
            plist = self._get(uow, user, list_id)
            in_list = repo.all_member_ids(list_id)
            orphaned: list[str] = []
            if in_list:
                elsewhere = repo.ids_in_other_lists(in_list, list_id)
                orphaned = [pid for pid in in_list if pid not in elsewhere]
                if orphaned:  # contacts with logged history are CRM records now: keep them
                    with_history = repo.ids_with_activity(orphaned)
                    orphaned = [pid for pid in orphaned if pid not in with_history]
            repo.delete_members(list_id)
            if orphaned:
                repo.hard_delete_prospects(orphaned)
            repo.remove(plist)
            uow.commit()
        return {"status": "deleted", "list_id": list_id, "deleted_prospects": len(orphaned)}

    def update_note(self, user: User, list_id: str, prospect_id: str, notes: Optional[str]) -> dict:
        with self.uow as uow:
            self._get(uow, user, list_id)
            membership = uow.lists.membership(list_id, prospect_id)
            if not membership:
                raise NotFoundError("Prospect not found in this list")
            membership.notes = notes
            uow.commit()
        return {"status": "updated", "prospect_id": prospect_id, "list_id": list_id}

    def update_prospect(self, user: User, prospect_id: str, data: dict[str, Any], if_match: Optional[int]) -> dict:
        """Same rules as PATCH /contacts/{id} (ContactWriteService commits)."""
        with self.uow as uow:
            prospect = update_contact(uow.session, user, prospect_id, ContactUpdate(**data), if_match=if_match)
            return {"status": "updated", "prospect_id": prospect_id, "row_version": prospect.row_version}

    def delete_prospect(self, user: User, prospect_id: str) -> dict:
        """Erase one contact and everything that references it."""
        with self.uow as uow:
            if not uow.lists.prospect(user.tenant_id, prospect_id):
                raise NotFoundError("Prospect not found")
            delete_contacts(uow.session, [prospect_id], actor_id=user.user_id)
            uow.commit()
        return {"status": "deleted", "prospect_id": prospect_id}

    def revalidate(self, user: User, list_id: str) -> dict:
        """Re-check every contact of a list: email format and type, global unsubscribes, duplicates in
        the list and POC State (with its timezone). Fixed metadata is saved."""
        with self.uow as uow:
            repo = uow.lists
            plist = self._get(uow, user, list_id)
            prospects = repo.revalidation_rows(user.tenant_id, list_id)
            unsubscribed = repo.unsubscribe_reasons(user.tenant_id)
            dupes = duplicate_emails(p.email for p in prospects)
            results, counts, updated_count = [], {"valid": 0, "invalid": 0, "warning": 0}, 0
            for prospect in prospects:
                outcome, updated = revalidate_prospect(prospect, unsubscribed, dupes)
                updated_count += int(updated)
                counts[outcome.status] += 1
                results.append({"prospect_id": prospect.prospect_id, "email": prospect.email or "",
                                "first_name": prospect.first_name, "last_name": prospect.last_name,
                                "company_name": prospect.company_name, "status": outcome.status,
                                "issues": outcome.issues})
            if updated_count > 0:
                uow.commit()
            return {"list_id": list_id, "list_name": plist.list_name, "total_checked": len(prospects),
                    "valid_count": counts["valid"], "invalid_count": counts["invalid"],
                    "warning_count": counts["warning"], "updated_count": updated_count, "results": results}
