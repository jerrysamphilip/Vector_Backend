# app/services/campaign_wizard_service.py
"""
Campaign Creation Wizard Service.
Handles the complete flow from list selection to campaign activation.

Flow:
1. Create campaign (metadata)
2. Select prospect list
3. Apply segmentation rules (persona, email type, industry)
4. Preview matching count
5. Run safety checks (unsubscribed, bounced, cool-off)
6. Bulk enroll to campaign_prospects
7. Generate AI templates per persona
8. Define email sequences
9. Activate campaign
"""

import logging
from typing import Dict, Iterable, List, Optional, Tuple
from datetime import datetime, timedelta, time, date as date_type
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, or_, not_
import uuid
import random

from app.models import (
    Campaign,
    CampaignProspect,
    CampaignStateEvent,
    EmailSequence,
    EmailTemplate,
    EmailMessage,
    Prospect,
    ProspectList,
    ProspectListMember,
    ProspectPersona,
    PersonaBlueprint,
    GlobalUnsubscribe,
    SendingInbox,
)
from app.models.domain_reputation import SendingDomain
from app.core.concurrency import claim_version
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.services.ai_email_service import classify_prospect
from app.services.batch_processor_service import (batch_classify_prospects, generate_persona_sequence,
                                                  load_persona_blueprint, save_persona_templates)
from app.services.enrollment_rules import MAX_REJECTIONS_RETURNED, summarize
from app.utils.email_context_detector import detect_email_context
from app.utils.business_calendar import calculate_send_time
from app.utils.email_utils import (
    normalize_unsubscribe_footer,
    strip_cta_content_no_link,
    normalize_cta_link,
    has_effective_cta_link,
)

logger = logging.getLogger(__name__)


# ============================================================
# SEGMENTATION RULES
# ============================================================

class SegmentationRules:
    """Defines filtering rules for prospect selection."""
    
    def __init__(
        self,
        persona_types: Optional[List[str]] = None,
        email_types: Optional[List[str]] = None,  # BUSINESS, PERSONAL
        industries: Optional[List[str]] = None,
        exclude_personal_emails: bool = True,
        min_confidence: float = 0.0,
    ):
        self.persona_types = persona_types or []
        self.email_types = email_types or ["BUSINESS"]
        self.industries = industries or []
        self.exclude_personal_emails = exclude_personal_emails
        self.min_confidence = min_confidence


# ============================================================
# SAFETY CHECKS
# ============================================================

def check_unsubscribed(db: Session, tenant_id: str, emails: List[str]) -> List[str]:
    """Get list of emails that are in global_unsubscribes."""
    unsubscribed = db.query(GlobalUnsubscribe.email).filter(
        GlobalUnsubscribe.tenant_id == tenant_id,
        GlobalUnsubscribe.email.in_(emails)
    ).all()
    return [u.email for u in unsubscribed]


def check_invalid_emails(db: Session, prospect_ids: List[str]) -> List[str]:
    """Get prospect IDs with invalid emails."""
    invalid = db.query(Prospect.prospect_id).filter(
        Prospect.prospect_id.in_(prospect_ids),
        Prospect.is_valid_email == False
    ).all()
    return [p.prospect_id for p in invalid]


def check_cool_off_period(
    db: Session, 
    prospect_ids: List[str], 
    days: int = 7
) -> List[str]:
    """Get prospect IDs that were contacted too recently."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    # Find prospects already in active campaigns
    recently_contacted = db.query(CampaignProspect.prospect_id).join(
        Campaign
    ).filter(
        CampaignProspect.prospect_id.in_(prospect_ids),
        Campaign.status.in_(["ACTIVE", "COMPLETED"]),
        CampaignProspect.enrolled_at >= cutoff
    ).all()
    
    return [p.prospect_id for p in recently_contacted]


# ============================================================
# WIZARD STEPS
# ============================================================

def step_1_create_campaign(
    db: Session,
    tenant_id: str,
    user_id: str,
    campaign_name: str,
    send_start_hour: int = 9,
    send_end_hour: int = 17,
    timezone: str = "Asia/Kolkata",
    cta_link: Optional[str] = None,
    inbox_ids: List[str] = [],
) -> Campaign:
    """
    Step 1: Create a new campaign with basic settings.
    """
    campaign = Campaign(
        campaign_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        created_by=user_id,
        campaign_name=campaign_name,
        status="DRAFT",
        send_window_start=time(hour=send_start_hour),
        send_window_end=time(hour=send_end_hour),
        respect_timezone=True,
        cta_link=normalize_cta_link(cta_link),
    )
    
    # Link selected sender inboxes
    if inbox_ids:
        campaign.inboxes = _validate_inboxes_for_campaign(db, tenant_id, inbox_ids)
        
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    
    # Log event
    _log_campaign_event(db, campaign.campaign_id, user_id, "DRAFT", None, "Campaign created")
    
    return campaign


def step_2_get_prospects_from_list(
    db: Session,
    list_id: str,
    excluded_prospect_ids: List[str] | None = None,
) -> List[Prospect]:
    """
    Step 2: Get all prospects from a prospect list.

    `excluded_prospect_ids`, if provided, drops those prospects from the
    result — lets a user deselect specific contacts in the wizard rather
    than always enrolling the whole list.
    """
    from app.models.prospect_list import ProspectList
    from app.services.enrollment_rules import list_member_ids
    plist = db.query(ProspectList).filter(ProspectList.list_id == list_id).first()
    prospect_ids = list_member_ids(db, plist.tenant_id, [list_id]) if plist else []
    if excluded_prospect_ids:
        excluded = set(excluded_prospect_ids)
        prospect_ids = [pid for pid in prospect_ids if pid not in excluded]

    prospects = db.query(Prospect).filter(
        Prospect.prospect_id.in_(prospect_ids), Prospect.deleted_at.is_(None)
    ).all()

    return prospects


def step_3_apply_segmentation(
    db: Session,
    prospects: List[Prospect],
    rules: SegmentationRules,
    rejections: Optional[List[dict]] = None,
) -> List[Prospect]:
    """
    Step 3: Filter prospects by segmentation rules. Pass a list as `rejections` to
    collect a reason for each prospect filtered out.
    """
    from app.services.enrollment_rules import rejection
    filtered = []
    
    def _drop(prospect, code):
        if rejections is not None:
            rejections.append(rejection(prospect, code))

    for prospect in prospects:
        # Filter by email type
        if rules.exclude_personal_emails:
            if prospect.email_type == "PERSONAL":
                _drop(prospect, "personal_email")
                continue
        
        if rules.email_types and prospect.email_type:
            if prospect.email_type not in rules.email_types:
                _drop(prospect, "segment_mismatch")
                continue
        
        # Filter by industry
        if rules.industries and prospect.industry:
            if prospect.industry not in rules.industries:
                _drop(prospect, "segment_mismatch")
                continue
        
        # Filter by persona type (requires classification)
        if rules.persona_types:
            persona = db.query(ProspectPersona).filter(
                ProspectPersona.prospect_id == prospect.prospect_id
            ).first()
            
            if persona:
                persona_type, confidence = persona.persona_type, persona.confidence_score
            else:
                # Classify on the fly
                persona_type, confidence = classify_prospect(
                    designation=prospect.designation or "",
                    company_name=prospect.company_name
                )
            if persona_type not in rules.persona_types or confidence < rules.min_confidence:
                _drop(prospect, "segment_mismatch")
                continue
        
        filtered.append(prospect)
    
    return filtered


def step_4_run_safety_checks(
    db: Session,
    tenant_id: str,
    prospects: List[Prospect],
    cool_off_days: int = 7,
    campaign_id: Optional[str] = None,
) -> Dict:
    """
    Step 4: Run safety checks (app/services/enrollment_rules.py) and return the safe
    prospects, counts per check, and a reason for each prospect excluded.
    """
    from app.services import enrollment_rules
    cool_off_ids = enrollment_rules.enrolled_elsewhere_within(
        db, [p.prospect_id for p in prospects], cool_off_days) if prospects else set()
    safe_prospects, rejections = enrollment_rules.screen(
        db, tenant_id, campaign_id, prospects, recently_contacted=cool_off_ids)
    counts = enrollment_rules.summarize(rejections)

    return {
        "total_before": len(prospects),
        "unsubscribed_count": counts.get("unsubscribed", 0) + counts.get("opted_out", 0),
        "invalid_count": counts.get("invalid_email", 0),
        "cool_off_count": counts.get("recently_contacted", 0),
        "already_enrolled_count": counts.get("already_enrolled", 0),
        "safe_prospects": safe_prospects,
        "safe_count": len(safe_prospects),
        "rejections": rejections,
    }


def step_5_bulk_enroll(
    db: Session,
    campaign_id: str,
    prospects: List[Prospect],
) -> int:
    """
    Step 5: Bulk enroll prospects into campaign_prospects.

    Spreads next_scheduled_at evenly across the campaign's send window so
    emails don't all fire at the same second. If send_window_end is set,
    prospects are distributed from start to end time on the campaign's
    start_date (or today). If only send_window_start is set, prospects are
    spaced 2 minutes apart from the start time.
    """
    from app.utils.business_calendar import calculate_spread_send_time

    # Fetch campaign to get send window and timezone
    campaign = db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
    tz_str = (campaign.campaign_timezone or "UTC") if campaign else "UTC"
    win_start = (campaign.send_window_start or time(9, 0)) if campaign else time(9, 0)
    win_end = campaign.send_window_end if campaign else None
    base_date = (campaign.start_date or date_type.today()) if campaign else date_type.today()
    raw_min_gap = (campaign.min_gap_minutes if campaign else None)
    min_gap_minutes = max(int(raw_min_gap if raw_min_gap is not None else 2), 0)

    # Collect already-enrolled prospect IDs to skip duplicates
    existing_ids = {
        row[0]
        for row in db.query(CampaignProspect.prospect_id).filter(
            CampaignProspect.campaign_id == campaign_id
        )
    }

    new_prospects = [p for p in prospects if p.prospect_id not in existing_ids]
    total = len(new_prospects)
    enrolled = 0

    for idx, prospect in enumerate(new_prospects):
        scheduled_at = calculate_spread_send_time(
            send_window_start=win_start,
            send_window_end=win_end,
            timezone_str=tz_str,
            base_date=base_date,
            total_prospects=total,
            prospect_index=idx,
            min_gap_minutes=min_gap_minutes,
        )

        enrollment = CampaignProspect(
            campaign_id=campaign_id,
            prospect_id=prospect.prospect_id,
            status="ACTIVE",
            enrolled_at=datetime.utcnow(),
            next_scheduled_at=scheduled_at,
        )
        db.add(enrollment)
        enrolled += 1

    db.commit()
    return enrolled


def step_6_create_email_sequences(
    db: Session,
    campaign_id: str,
    schedule: List[Dict] | None = None,
) -> List[EmailSequence]:
    """
    Step 6: Create email sequence steps (Day 1, 3, 7).
    """
    if schedule is None:
        schedule = [
            {"step": 1, "wait_days": 0, "purpose": "Introduction"},
            {"step": 2, "wait_days": 2, "purpose": "Reminder"},
            {"step": 3, "wait_days": 4, "purpose": "Last Chance"},
        ]
    
    sequences = []
    
    for s in schedule:
        seq = EmailSequence(
            sequence_id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            step_number=s["step"],
            wait_days=s["wait_days"],
            stop_on_reply=True,
            stop_on_bounce=True,
        )
        db.add(seq)
        sequences.append(seq)
    
    db.commit()
    return sequences


def step_7_activate_campaign(
    db: Session,
    campaign_id: str,
    user_id: str,
) -> Optional[Campaign]:
    """
    Step 7: Activate the campaign (status = ACTIVE).
    Delegates to CampaignEmailService for robust pre-scheduling.
    """
    from app.services.campaign_email_service import CampaignEmailService
    campaign = CampaignEmailService(db).launch(campaign_id, user_id)
    
    if campaign:
        campaign.launched_at = datetime.utcnow()
        
        # Log campaign specific wizard event
        _log_campaign_event(db, campaign_id, user_id, "ACTIVE", "DRAFT", "Campaign activated via wizard")
        
        db.commit()
        db.refresh(campaign)
        
    return campaign


def _log_campaign_event(
    db: Session,
    campaign_id: str,
    user_id: str,
    new_status: str,
    old_status: Optional[str],
    reason: str,
):
    """Log a campaign state change event."""
    event = CampaignStateEvent(
        campaign_id=campaign_id,
        from_state=old_status,
        to_state=new_status,
        reason=reason,
    )
    db.add(event)
    db.commit()


def _validate_inboxes_for_campaign(db: Session, tenant_id: str, inbox_ids: List[str]) -> List[SendingInbox]:
    """
    Validate selected inboxes:
    - must exist in current tenant
    - must be ACTIVE
    - tracked domain must not be blacklisted
    """
    unique_ids = list(dict.fromkeys(inbox_ids or []))
    if not unique_ids:
        return []

    inboxes = (
        db.query(SendingInbox)
        .filter(
            SendingInbox.tenant_id == tenant_id,
            SendingInbox.inbox_id.in_(unique_ids),
        )
        .all()
    )

    if len(inboxes) != len(unique_ids):
        found_ids = {i.inbox_id for i in inboxes}
        missing = [i for i in unique_ids if i not in found_ids]
        raise ValueError(f"Invalid inbox selection. Not found in tenant: {', '.join(missing)}")

    inactive = [i.email_address for i in inboxes if (i.status or "").upper() != "ACTIVE"]
    if inactive:
        raise ValueError(f"Selected inboxes are not ACTIVE: {', '.join(inactive)}")

    domains = {
        i.email_address.split("@", 1)[1].lower()
        for i in inboxes
        if i.email_address and "@" in i.email_address
    }
    if domains:
        domain_rows = (
            db.query(SendingDomain.domain_name, SendingDomain.is_blacklisted)
            .filter(SendingDomain.tenant_id == tenant_id, SendingDomain.domain_name.in_(domains))
            .all()
        )
        blocked = [name for name, is_blacklisted in domain_rows if is_blacklisted]
        if blocked:
            raise ValueError(
                f"Selected inbox domains are blocked for sending: {', '.join(sorted(set(blocked)))}"
            )

    return inboxes


# ============================================================
# COMPLETE WIZARD
# ============================================================

def run_campaign_wizard(
    db: Session,
    tenant_id: str,
    user_id: str,
    campaign_name: str,
    list_id: str,
    rules: SegmentationRules,
    cool_off_days: int = 7,
) -> Dict:
    """
    Run the complete campaign creation wizard.
    
    Returns dict with campaign and stats.
    """
    # Step 1: Create campaign
    campaign = step_1_create_campaign(
        db, tenant_id, user_id, campaign_name
    )
    
    # Step 2: Get prospects from list
    all_prospects = step_2_get_prospects_from_list(db, list_id)
    
    # Step 3: Apply segmentation
    segmented = step_3_apply_segmentation(db, all_prospects, rules)
    
    # Step 4: Safety checks
    safety_result = step_4_run_safety_checks(
        db, tenant_id, segmented, cool_off_days
    )
    
    safe_prospects = safety_result["safe_prospects"]
    
    # Step 5: Bulk enroll
    enrolled_count = step_5_bulk_enroll(db, campaign.campaign_id, safe_prospects)
    
    # Step 6: Create sequences
    sequences = step_6_create_email_sequences(db, campaign.campaign_id)
    
    return {
        "campaign": campaign,
        "list_total": len(all_prospects),
        "after_segmentation": len(segmented),
        "unsubscribed": safety_result["unsubscribed_count"],
        "invalid_emails": safety_result["invalid_count"],
        "cool_off_excluded": safety_result["cool_off_count"],
        "enrolled": enrolled_count,
        "sequences_created": len(sequences),
        "status": "DRAFT",  # Still needs activation
    }





# ============================================================
# API OPERATIONS (tenant-scoped; used by campaign_wizard_router)
# ============================================================

SCHEDULE_PREVIEW_SEND_HOUR = 9


def get_campaign_for_tenant(db: Session, campaign_id: str, tenant_id: str) -> Campaign:
    """The campaign if it belongs to the tenant; NotFoundError otherwise."""
    campaign = db.query(Campaign).filter(
        Campaign.campaign_id == campaign_id,
        Campaign.tenant_id == tenant_id,
    ).first()
    if not campaign:
        raise NotFoundError("Campaign not found")
    return campaign


def get_list_for_tenant(db: Session, list_id: str, tenant_id: str) -> ProspectList:
    """The prospect list if it belongs to the tenant; NotFoundError otherwise."""
    prospect_list = db.query(ProspectList).filter(
        ProspectList.list_id == list_id,
        ProspectList.tenant_id == tenant_id,
    ).first()
    if not prospect_list:
        raise NotFoundError("Prospect list not found")
    return prospect_list


def list_available_lists(db: Session, tenant_id: str) -> List[Dict]:
    """The tenant's prospect lists with their member counts, for the wizard's list picker."""
    lists = db.query(ProspectList).filter(ProspectList.tenant_id == tenant_id).all()
    counts = dict(
        db.query(ProspectListMember.list_id, func.count(ProspectListMember.id))
        .filter(ProspectListMember.list_id.in_([pl.list_id for pl in lists] or [""]))
        .group_by(ProspectListMember.list_id).all()
    )
    return [
        {
            "list_id": pl.list_id,
            "list_name": pl.list_name,
            "prospect_count": counts.get(pl.list_id, 0),
            "created_at": pl.uploaded_at,
        }
        for pl in lists
    ]


def build_schedule_preview(
    steps: Iterable[Tuple[int, int]],
    state: Optional[str],
    base_time: Optional[datetime] = None,
) -> List[Dict]:
    """
    Send dates for (step_number, wait_days) steps: 9 AM in the state's timezone, skipping weekends
    and US federal holidays, wait days accumulating across steps (including step 1). No jitter, so
    the preview is stable.
    """
    base_time = base_time or datetime.utcnow()
    schedule = []
    cumulative_wait_days = 0
    for step_number, wait_days in steps:
        cumulative_wait_days += wait_days
        send_datetime = calculate_send_time(
            base_time=base_time,
            wait_days=cumulative_wait_days,
            state=state,
            send_hour=SCHEDULE_PREVIEW_SEND_HOUR,
            send_minute=0,
            add_jitter=False,
        )
        schedule.append({
            "step_number": step_number,
            "send_date": send_datetime.isoformat() + "Z",
            "formatted": send_datetime.strftime("%a, %b %d"),  # e.g. "Mon, Jan 20"
            "wait_days": wait_days if step_number > 1 else 0,
        })
    return schedule


def create_campaign_for_tenant(
    db: Session,
    tenant_id: str,
    user_id: str,
    campaign_name: str,
    send_start_hour: int,
    send_end_hour: int,
    timezone: str,
    cta_link: Optional[str],
    inbox_ids: List[str],
) -> Campaign:
    """Wizard step 1; an invalid setting (e.g. inbox selection) is a BadRequestError."""
    try:
        return step_1_create_campaign(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            campaign_name=campaign_name,
            send_start_hour=send_start_hour,
            send_end_hour=send_end_hour,
            timezone=timezone,
            cta_link=normalize_cta_link(cta_link),
            inbox_ids=inbox_ids,
        )
    except ValueError as exc:
        db.rollback()
        raise BadRequestError(str(exc)) from exc


def generate_persona_template(
    db: Session,
    campaign: Campaign,
    tenant_id: str,
    persona_type: str,
    list_id: str,
    product_name: str,
    campaign_description: str,
    sequence_steps: Optional[List[dict]],
    excluded_prospect_ids: Optional[List[str]],
    include_first_name_in_subject: bool,
    creative_email: bool,
) -> Dict:
    """
    (Re)generate one persona's templates for the campaign. Works for any canonical persona, even
    one with no classified prospects yet. Only this persona's previous templates are replaced.

    No transaction is open during the LLM call (T-1): the inputs are read (and new persona
    classifications saved) and committed first; afterwards one short transaction re-checks the
    campaign and swaps the persona's old templates for the new ones.
    """
    campaign_id = campaign.campaign_id
    email_context = detect_email_context(product_name, campaign_description)
    cta_link = normalize_cta_link(campaign.cta_link)
    try:
        # Reuse (or create) classifications for this list; may yield 0 prospects for this persona.
        prospects = batch_classify_prospects(db, list_id, tenant_id, excluded_prospect_ids).get(persona_type, [])
        blueprint = load_persona_blueprint(db, campaign_id, persona_type)
        db.commit()  # keep the classifications; no locks or snapshot held across the LLM call
    except Exception:
        db.rollback()
        raise

    sequence, fallback_reason = generate_persona_sequence(
        persona_type, blueprint, product_name, campaign_description, sequence_steps,
        include_first_name_in_subject, creative_email, email_context, cta_link)
    return _replace_persona_templates(
        db, campaign_id, persona_type, prospects, sequence, fallback_reason, sequence_steps,
        include_first_name_in_subject, cta_link)


def _replace_persona_templates(db: Session, campaign_id: str, persona_type: str, prospects: List[dict],
                               sequence: Optional[dict], fallback_reason: Optional[str],
                               sequence_steps: Optional[List[dict]], include_first_name_in_subject: bool,
                               cta_link: str) -> Dict:
    """The short write transaction after generation: re-reads the campaign (it may have been deleted
    while the LLM ran) and its current steps, then replaces this persona's templates all-or-nothing."""
    try:
        if not db.query(Campaign.campaign_id).filter(Campaign.campaign_id == campaign_id).first():
            raise NotFoundError("Campaign not found")
        step_to_seq_id = {s.step_number: s.sequence_id for s in db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id
        ).all()}
        db.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == campaign_id,
            EmailTemplate.designation == persona_type,
        ).delete(synchronize_session=False)
        result = save_persona_templates(
            db, campaign_id, persona_type, prospects, sequence, fallback_reason, sequence_steps,
            include_first_name_in_subject, cta_link, has_effective_cta_link(cta_link), step_to_seq_id)
        db.commit()
        return result
    except Exception:
        db.rollback()  # all-or-nothing: never leave this persona half-replaced
        raise


def _rejection_report(rejections: List[Dict]) -> Dict:
    return {
        "rejected_count": len(rejections),
        "rejected_summary": summarize(rejections),
        "rejected": rejections[:MAX_REJECTIONS_RETURNED],
    }


def preview_list_segmentation(db: Session, tenant_id: str, list_id: str, rules: SegmentationRules) -> Dict:
    """Counts after segmentation and safety checks for a list, without enrolling anyone."""
    get_list_for_tenant(db, list_id, tenant_id)
    all_prospects = step_2_get_prospects_from_list(db, list_id)
    if not all_prospects:
        raise NotFoundError("No prospects found in this list")

    segment_rejections: List[Dict] = []
    segmented = step_3_apply_segmentation(db, all_prospects, rules, segment_rejections)
    safety = step_4_run_safety_checks(db, tenant_id, segmented)
    return {
        "list_total": len(all_prospects),
        "after_segmentation": len(segmented),
        "after_safety_checks": safety["safe_count"],
        "breakdown": {
            "unsubscribed": safety["unsubscribed_count"],
            "invalid_emails": safety["invalid_count"],
            "cool_off_excluded": safety["cool_off_count"],
        },
        "ready_to_enroll": safety["safe_count"],
        **_rejection_report(segment_rejections + safety["rejections"]),
    }


def enroll_list_into_campaign(
    db: Session,
    tenant_id: str,
    campaign_id: str,
    list_id: str,
    rules: SegmentationRules,
    cool_off_days: int,
    excluded_prospect_ids: Optional[List[str]],
) -> Dict:
    """Segment a list, run the safety checks and bulk-enroll whoever passes."""
    get_campaign_for_tenant(db, campaign_id, tenant_id)
    get_list_for_tenant(db, list_id, tenant_id)

    all_prospects = step_2_get_prospects_from_list(db, list_id, excluded_prospect_ids)
    logger.info(f"[ENROLL] Step 2 - Prospects from list '{list_id}': {len(all_prospects)}")
    logger.info(f"[ENROLL] Step 3 - Segmentation rules: exclude_personal={rules.exclude_personal_emails}, "
                f"persona_types={rules.persona_types}")

    segment_rejections: List[Dict] = []
    segmented = step_3_apply_segmentation(db, all_prospects, rules, segment_rejections)
    logger.info(f"[ENROLL] Step 3 - After segmentation: {len(segmented)} "
                f"(dropped {len(all_prospects) - len(segmented)})")

    safety = step_4_run_safety_checks(db, tenant_id, segmented, cool_off_days, campaign_id=campaign_id)
    logger.info(f"[ENROLL] Step 4 - Safety: safe={safety['safe_count']}, unsub={safety['unsubscribed_count']}, "
                f"invalid={safety['invalid_count']}, cool_off={safety['cool_off_count']}")

    enrolled = step_5_bulk_enroll(db, campaign_id, safety["safe_prospects"])
    logger.info(f"[ENROLL] Step 5 - Enrolled: {enrolled}")

    return {
        "campaign_id": campaign_id,
        "enrolled_count": enrolled,
        "sequences_created": 0,
        "status": "READY_TO_ACTIVATE",
        **_rejection_report(segment_rejections + safety["rejections"]),
        "_debug": {
            "prospects_in_list": len(all_prospects),
            "after_segmentation": len(segmented),
            "safe_count": safety["safe_count"],
            "unsubscribed": safety["unsubscribed_count"],
            "invalid": safety["invalid_count"],
            "cool_off": safety["cool_off_count"],
        }
    }


def activate_campaign_for_tenant(db: Session, tenant_id: str, campaign_id: str, user_id: str,
                                 expected_version: Optional[int] = None) -> Campaign:
    """Wizard activation: no sender identity -> BadRequestError, not activatable -> ConflictError,
    a stale expected_version (the campaign's row_version the wizard loaded, T-3) -> 409."""
    from app.services.sender_identity import SenderIdentityMissing

    campaign = get_campaign_for_tenant(db, campaign_id, tenant_id)
    claim_version(db, campaign, expected_version, record="campaign")
    try:
        campaign = step_7_activate_campaign(db, campaign_id, user_id)
    except SenderIdentityMissing as exc:
        db.rollback()
        raise BadRequestError(str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise ConflictError(str(exc)) from exc
    if not campaign:
        raise NotFoundError("Campaign not found")
    return campaign


def run_campaign_wizard_for_tenant(
    db: Session,
    tenant_id: str,
    user_id: str,
    campaign_name: str,
    list_id: str,
    rules: SegmentationRules,
    cool_off_days: int,
) -> Dict:
    """The all-in-one wizard on one of the tenant's lists; invalid input is a BadRequestError."""
    get_list_for_tenant(db, list_id, tenant_id)
    try:
        return run_campaign_wizard(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            campaign_name=campaign_name,
            list_id=list_id,
            rules=rules,
            cool_off_days=cool_off_days,
        )
    except ValueError as exc:
        db.rollback()
        raise BadRequestError(str(exc)) from exc
