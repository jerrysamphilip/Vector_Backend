# app/services/enrollment_rules.py
"""
One set of campaign-enrollment checks, used by every enrollment path (campaign page,
campaign wizard, list bulk enrollment), returning a reason for each contact that is not
enrolled (BR-DF-02) and enforcing unsubscribes the same way everywhere (BR-DF-08).

Checks, in order (first failure wins):
  unsubscribed      on the workspace's global unsubscribe list (still within its suppression period)
  opted_out         the contact's own subscription status is not OPT_IN
  invalid_email     email marked invalid (e.g. after a hard bounce)
  already_enrolled  already in this campaign
  previously_removed  removed from this campaign by the user after being emailed (a removed contact
                    with no send history is re-enrolable: its enrolment is reactivated, S-21)
  in_source_campaign  already in the campaign this one is a copy of (duplicate with skip_source_prospects)
  replied_positive  already replied with interest (positive reply) in any campaign
  active_elsewhere  still mid-sequence in another ACTIVE campaign of the workspace
  recently_contacted  cool-off rule of the calling path
Emails are compared case-insensitively. check() is the dry run of exactly these rules (S-20).
"""

from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models.campaign import Campaign, CampaignProspect
from app.models.email_message import EmailMessage
from app.models.prospect import GlobalUnsubscribe, Prospect
from app.repositories.campaigns import CampaignsRepository

REASONS = {
    "unsubscribed": "On the global unsubscribe list",
    "opted_out": "Contact has unsubscribed",
    "invalid_email": "Email address is invalid or has bounced",
    "already_enrolled": "Already in this campaign",
    "previously_removed": "Removed from this campaign after being emailed",
    "in_source_campaign": "Already in the campaign this one was copied from",
    "replied_positive": "Already replied with interest",
    "active_elsewhere": "Still in another active campaign",
    "recently_contacted": "Contacted too recently (cool-off period)",
    "personal_email": "Personal email address (excluded by this campaign's rules)",
    "segment_mismatch": "Doesn't match this campaign's audience rules",
    "not_found": "Contact not found in your workspace",
}

MAX_REJECTIONS_RETURNED = 5000


def rejection(prospect: Optional[Prospect], code: str, prospect_id: Optional[str] = None) -> dict:
    return {
        "prospect_id": prospect.prospect_id if prospect else prospect_id,
        "email": prospect.email if prospect else None,
        "name": prospect.full_name if prospect else None,
        "reason_code": code,
        "reason": REASONS[code],
    }


def summarize(rejections: Iterable[dict]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for r in rejections:
        counts[r["reason_code"]] = counts.get(r["reason_code"], 0) + 1
    return counts


def sent_within(db: Session, prospect_ids: List[str], hours: int) -> set:
    """Cool-off used by the campaign page: an email was sent to them in the last N hours."""
    cutoff = utcnow() - timedelta(hours=hours)
    return {r[0] for r in db.query(EmailMessage.prospect_id).filter(
        EmailMessage.prospect_id.in_(prospect_ids), EmailMessage.sent_at > cutoff).distinct()}


def enrolled_elsewhere_within(db: Session, prospect_ids: List[str], days: int) -> set:
    """Cool-off used by the wizard: enrolled in an active or completed campaign in the last N days."""
    cutoff = utcnow() - timedelta(days=days)
    return {r[0] for r in db.query(CampaignProspect.prospect_id).join(Campaign).filter(
        CampaignProspect.prospect_id.in_(prospect_ids),
        Campaign.status.in_(["ACTIVE", "COMPLETED"]),
        CampaignProspect.enrolled_at >= cutoff,
    ).distinct()}


def screen(
    db: Session,
    tenant_id: str,
    campaign_id: Optional[str],
    prospects: List[Prospect],
    recently_contacted: Optional[set] = None,
    skip_ids: Optional[Set[str]] = None,
    allow_reenrol: bool = False,
) -> Tuple[List[Prospect], List[dict]]:
    """
    Split prospects into (eligible, rejections). Never mutates anything. With ``allow_reenrol`` a
    contact the user removed from this campaign before it was emailed is eligible again; the caller
    must then reactivate its kept row with reactivate_removed() instead of inserting one. Without it
    every removed contact counts as already_enrolled (for callers that only insert rows).
    """
    if not prospects:
        return [], []
    ids = [p.prospect_id for p in prospects]
    emails = {(p.email or "").strip().lower() for p in prospects}
    repo = CampaignsRepository(db)

    # Same rule the scheduler applies at send time (BR-DF-08)
    from app.services.suppression import suppressed_emails
    unsubscribed = suppressed_emails(db, tenant_id, emails)
    enrolled: Set[str] = set()
    removed: Dict[str, bool] = {}
    if campaign_id:
        enrolled = {r[0] for r in db.query(CampaignProspect.prospect_id).filter(
            CampaignProspect.campaign_id == campaign_id, CampaignProspect.prospect_id.in_(ids))}
        removed = repo.removed_enrolments(tenant_id, campaign_id, ids)
        if not allow_reenrol:
            removed = {}
    facts = _Facts(
        unsubscribed=unsubscribed,
        enrolled=enrolled,
        removed=removed,
        skip=skip_ids or set(),
        positive=repo.positive_repliers(tenant_id, ids),
        elsewhere=repo.active_in_other_active_campaigns(tenant_id, ids, campaign_id),
        recent=recently_contacted or set(),
    )

    eligible, rejections = [], []
    for p in prospects:
        code = facts.block_reason(p)
        if code is None:
            eligible.append(p)
        else:
            rejections.append(rejection(p, code))
    return eligible, rejections


class _Facts:
    """What screen() knows about a batch of contacts; block_reason() applies the rules in order."""

    def __init__(self, *, unsubscribed: set, enrolled: Set[str], removed: Dict[str, bool], skip: Set[str],
                 positive: Set[str], elsewhere: Set[str], recent: set) -> None:
        self.unsubscribed = unsubscribed
        self.enrolled = enrolled
        self.removed = removed
        self.skip = skip
        self.positive = positive
        self.elsewhere = elsewhere
        self.recent = recent

    def _enrolment_reason(self, pid: str) -> Optional[str]:
        if pid in self.removed:
            # A removed contact never emailed may come back; one already emailed may not
            return "previously_removed" if self.removed[pid] else None
        return "already_enrolled" if pid in self.enrolled else None

    def block_reason(self, p: Prospect) -> Optional[str]:
        pid = p.prospect_id
        if (p.email or "").strip().lower() in self.unsubscribed:
            return "unsubscribed"
        if p.consent_status != "OPT_IN":
            return "opted_out"
        if p.is_valid_email is False:
            return "invalid_email"
        enrolment = self._enrolment_reason(pid)
        if enrolment:
            return enrolment
        for code, ids in (("in_source_campaign", self.skip), ("replied_positive", self.positive),
                          ("active_elsewhere", self.elsewhere), ("recently_contacted", self.recent)):
            if pid in ids:
                return code
        return None


def reactivate_removed(db: Session, tenant_id: str, campaign_id: str, prospect_ids: Iterable[str],
                       next_scheduled_at: Optional[datetime]) -> Set[str]:
    """
    Re-enrol contacts whose earlier enrolment in this campaign the user removed (they passed screen(),
    so they were never emailed): the kept row goes back to ACTIVE at step 1. Returns their ids; the
    caller inserts rows only for the others (one row per contact per campaign).
    """
    back: Set[str] = set()
    for cp in CampaignsRepository(db).removed_rows(tenant_id, campaign_id, list(prospect_ids)):
        cp.status = "ACTIVE"  # direct write: leaving the removed state is not a status-ladder event
        cp.stopped_reason = None
        cp.current_step = 1
        cp.enrolled_at = utcnow()
        cp.next_scheduled_at = next_scheduled_at
        back.add(cp.prospect_id)
    return back


def check(eligible: List[Prospect], rejections: List[dict]) -> dict:
    """The dry-run response: counts plus who would be skipped and why (same shape as an enrolment)."""
    return {
        "would_enroll_count": len(eligible),
        "would_enroll": [{"prospect_id": p.prospect_id, "email": p.email, "name": p.full_name}
                         for p in eligible[:MAX_REJECTIONS_RETURNED]],
        "rejected_count": len(rejections),
        "rejected_summary": summarize(rejections),
        "rejected": rejections[:MAX_REJECTIONS_RETURNED],
    }


def load_tenant_prospects(db: Session, tenant_id: str, prospect_ids: Iterable[str]) -> Tuple[List[Prospect], List[dict]]:
    """Fetch prospects by id, scoped to the workspace; ids from elsewhere become not_found rejections."""
    wanted = list(dict.fromkeys(prospect_ids))
    found = {p.prospect_id: p for p in db.query(Prospect).filter(
        Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(wanted),
        Prospect.deleted_at.is_(None))} if wanted else {}
    missing = [rejection(None, "not_found", pid) for pid in wanted if pid not in found]
    return [found[pid] for pid in wanted if pid in found], missing


def list_member_ids(db: Session, tenant_id: str, list_ids: Iterable[str]) -> List[str]:
    """
    Contact ids in the given lists, so any list can be a campaign audience (BR-CM-24).
    Active lists are evaluated now from their filters, as their creator would see them.
    """
    from app.models.prospect_list import ProspectList
    from app.models.user import User
    from app.services import crm

    ids: List[str] = []
    for plist in db.query(ProspectList).filter(ProspectList.tenant_id == tenant_id,
                                               ProspectList.list_id.in_(list(list_ids))):
        owner = db.query(User).filter(User.user_id == plist.uploaded_by).first()
        ids.extend(r[0] for r in crm.list_member_ids(db, plist, owner))
    return list(dict.fromkeys(ids))
