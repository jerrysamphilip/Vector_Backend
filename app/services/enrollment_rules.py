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
  recently_contacted  cool-off rule of the calling path
Emails are compared case-insensitively.
"""

from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignProspect
from app.models.email_message import EmailMessage
from app.models.prospect import GlobalUnsubscribe, Prospect

REASONS = {
    "unsubscribed": "On the global unsubscribe list",
    "opted_out": "Contact has unsubscribed",
    "invalid_email": "Email address is invalid or has bounced",
    "already_enrolled": "Already in this campaign",
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
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    return {r[0] for r in db.query(EmailMessage.prospect_id).filter(
        EmailMessage.prospect_id.in_(prospect_ids), EmailMessage.sent_at > cutoff).distinct()}


def enrolled_elsewhere_within(db: Session, prospect_ids: List[str], days: int) -> set:
    """Cool-off used by the wizard: enrolled in an active or completed campaign in the last N days."""
    cutoff = datetime.utcnow() - timedelta(days=days)
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
) -> Tuple[List[Prospect], List[dict]]:
    """Split prospects into (eligible, rejections). Never mutates anything."""
    if not prospects:
        return [], []
    ids = [p.prospect_id for p in prospects]
    emails = {(p.email or "").strip().lower() for p in prospects}

    now = datetime.utcnow()
    unsubscribed = {(e or "").strip().lower() for (e,) in db.query(GlobalUnsubscribe.email).filter(
        GlobalUnsubscribe.tenant_id == tenant_id,
        func.lower(GlobalUnsubscribe.email).in_(emails),
        or_(GlobalUnsubscribe.suppression_expires_at.is_(None), GlobalUnsubscribe.suppression_expires_at > now),
    )}
    enrolled = set()
    if campaign_id:
        enrolled = {r[0] for r in db.query(CampaignProspect.prospect_id).filter(
            CampaignProspect.campaign_id == campaign_id, CampaignProspect.prospect_id.in_(ids))}
    recently_contacted = recently_contacted or set()

    eligible, rejections = [], []
    for p in prospects:
        if (p.email or "").strip().lower() in unsubscribed:
            code = "unsubscribed"
        elif p.consent_status != "OPT_IN":
            code = "opted_out"
        elif p.is_valid_email is False:
            code = "invalid_email"
        elif p.prospect_id in enrolled:
            code = "already_enrolled"
        elif p.prospect_id in recently_contacted:
            code = "recently_contacted"
        else:
            eligible.append(p)
            continue
        rejections.append(rejection(p, code))
    return eligible, rejections


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
