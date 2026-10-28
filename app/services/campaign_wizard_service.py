# app/services/campaign_wizard_service.py
"""
Campaign Creation Wizard Service (chunk A2: SQL in app/repositories/campaigns*.py).
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

``CampaignWizardService`` holds one method per route of app/routers/campaign_wizard_router.py, each one
unit of work on the caller's tenant. The enrolment rules are app/services/enrollment_rules.py (shared
with the campaign page), the AI templates app/services/batch_processor_service.py.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core.clock import utcnow
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.campaign import Campaign, CampaignProspect
from app.models.email_sequence import EmailSequence
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectList
from app.schemas.campaign_schema import CampaignEnrollmentRequest, EnrollmentCheckRequest
from app.services import batch_processor_service, enrollment_rules
from app.services.ai_email_service import classify_prospect
from app.services.campaign_common import CampaignServiceBase, require_campaign, validate_inboxes
from app.services.campaign_email_service import CampaignEmailService
from app.services.campaign_service import pacing_for
from app.services.enrollment_rules import MAX_REJECTIONS_RETURNED, rejection, summarize
from app.services.sender_identity import SenderIdentityMissing
from app.utils.business_calendar import calculate_send_time, calculate_spread_send_time
from app.utils.email_context_detector import detect_email_context
from app.utils.email_utils import has_effective_cta_link, normalize_cta_link

logger = logging.getLogger(__name__)

SCHEDULE_PREVIEW_SEND_HOUR = 9
DEFAULT_SCHEDULE = [
    {"step": 1, "wait_days": 0, "purpose": "Introduction"},
    {"step": 2, "wait_days": 2, "purpose": "Reminder"},
    {"step": 3, "wait_days": 4, "purpose": "Last Chance"},
]


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


def segment_mismatch(prospect: Any, rules: SegmentationRules, persona: Optional[Tuple[str, float]]) -> Optional[str]:
    """The rejection code when a contact falls outside the segment, else None (pure)."""
    if rules.exclude_personal_emails and prospect.email_type == "PERSONAL":
        return "personal_email"
    if rules.email_types and prospect.email_type and prospect.email_type not in rules.email_types:
        return "segment_mismatch"
    if rules.industries and prospect.industry and prospect.industry not in rules.industries:
        return "segment_mismatch"
    if rules.persona_types and persona is not None:
        persona_type, confidence = persona
        if persona_type not in rules.persona_types or confidence < rules.min_confidence:
            return "segment_mismatch"
    return None


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
    base_time = base_time or utcnow()
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


def _rejection_report(rejections: List[Dict]) -> Dict:
    return {
        "rejected_count": len(rejections),
        "rejected_summary": summarize(rejections),
        "rejected": rejections[:MAX_REJECTIONS_RETURNED],
    }


@dataclass
class Screened:
    """Wizard steps 2-4 for one list: what enrolment and its dry run both use (S-20)."""

    all: List[Prospect]
    segmented: List[Prospect]
    safety: Dict[str, Any]
    rejections: List[Dict] = field(default_factory=list)


@dataclass(frozen=True)
class PersonaGeneration:
    """Input of one persona's template (re)generation."""

    persona_type: str
    list_id: str
    product_name: str
    campaign_description: str
    sequence_steps: Optional[List[dict]]
    excluded_prospect_ids: Optional[List[str]]
    include_first_name_in_subject: bool
    creative_email: bool


class CampaignWizardService(CampaignServiceBase):

    # ── scoping ───────────────────────────────────────────────────────────────────────────────

    def campaign(self, tenant_id: str, campaign_id: str) -> Campaign:
        return require_campaign(self.uow.campaigns, tenant_id, campaign_id)

    def prospect_list(self, tenant_id: str, list_id: str) -> ProspectList:
        prospect_list = self.uow.wizard.prospect_list(tenant_id, list_id)
        if not prospect_list:
            raise NotFoundError("Prospect list not found")
        return prospect_list

    # ── wizard steps (inside the unit of work) ────────────────────────────────────────────────

    def create(self, tenant_id: str, user_id: str, campaign_name: str, send_start_hour: int = 9,
               send_end_hour: int = 17, cta_link: Optional[str] = None,
               inbox_ids: Optional[List[str]] = None) -> Campaign:
        """Step 1: a DRAFT campaign with its send window, CTA link and mailboxes."""
        campaign = Campaign(campaign_id=str(uuid.uuid4()), tenant_id=tenant_id, created_by=user_id,
                            campaign_name=campaign_name, status="DRAFT", send_window_start=time(hour=send_start_hour),
                            send_window_end=time(hour=send_end_hour), respect_timezone=True,
                            cta_link=normalize_cta_link(cta_link))
        if inbox_ids:
            campaign.inboxes = validate_inboxes(self.uow.campaigns, tenant_id, inbox_ids)
        self.uow.campaigns.add(tenant_id, campaign)
        self.uow.campaigns.flush()
        self.uow.campaigns.add_state_event(campaign.campaign_id, None, "DRAFT", "Campaign created")
        return campaign

    def list_contacts(self, list_id: str, excluded_prospect_ids: Optional[List[str]] = None) -> List[Prospect]:
        """Step 2: the list's live contacts, minus the ones deselected in the wizard."""
        tenant_id = self.uow.wizard.list_tenant(list_id)
        prospect_ids = enrollment_rules.list_member_ids(self.uow.session, tenant_id, [list_id]) if tenant_id else []
        excluded = set(excluded_prospect_ids or [])
        return self.uow.wizard.live_contacts([pid for pid in prospect_ids if pid not in excluded])

    def segment(self, prospects: List[Prospect], rules: SegmentationRules,
                rejections: Optional[List[dict]] = None) -> List[Prospect]:
        """Step 3: the contacts inside the segment; a reason is collected for each one left out."""
        kept = []
        for prospect in prospects:
            persona = self._persona(prospect) if rules.persona_types else None
            code = segment_mismatch(prospect, rules, persona)
            if code:
                if rejections is not None:
                    rejections.append(rejection(prospect, code))
                continue
            kept.append(prospect)
        return kept

    def _persona(self, prospect: Prospect) -> Tuple[str, float]:
        stored = self.uow.wizard.persona(prospect.prospect_id)
        if stored:
            return stored.persona_type, stored.confidence_score
        # Classify on the fly
        return classify_prospect(designation=prospect.designation or "", company_name=prospect.company_name)

    def safety_checks(self, tenant_id: str, prospects: List[Prospect], cool_off_days: int = 7,
                      campaign_id: Optional[str] = None) -> Dict[str, Any]:
        """Step 4: the enrolment rules' safety checks: the safe contacts, counts per check and each reason."""
        db = self.uow.session
        cool_off_ids = enrollment_rules.enrolled_elsewhere_within(
            db, [p.prospect_id for p in prospects], cool_off_days) if prospects else set()
        safe, rejections = enrollment_rules.screen(db, tenant_id, campaign_id, prospects,
                                                   recently_contacted=cool_off_ids,
                                                   allow_reenrol=campaign_id is not None)
        counts = summarize(rejections)
        return {
            "total_before": len(prospects),
            "unsubscribed_count": counts.get("unsubscribed", 0) + counts.get("opted_out", 0),
            "invalid_count": counts.get("invalid_email", 0),
            "cool_off_count": counts.get("recently_contacted", 0),
            "already_enrolled_count": counts.get("already_enrolled", 0),
            "safe_prospects": safe,
            "safe_count": len(safe),
            "rejections": rejections,
        }

    def bulk_enroll(self, campaign_id: str, prospects: List[Prospect]) -> int:
        """
        Step 5: enrol the contacts, their start times spread evenly across the send window on the start
        date (or today); contacts removed earlier and never emailed come back on their kept row (S-21).
        """
        repo = self.uow.campaigns
        campaign = repo.by_id(campaign_id)
        if campaign is None:
            return 0
        existing = repo.enrolled_contact_ids(campaign_id)
        reactivated = {cp.prospect_id for cp in repo.removed_rows(campaign.tenant_id, campaign_id,
                                                                    [p.prospect_id for p in prospects])}
        new = [p for p in prospects if p.prospect_id not in existing or p.prospect_id in reactivated]
        raw_gap = campaign.min_gap_minutes
        for index, prospect in enumerate(new):
            scheduled_at = calculate_spread_send_time(
                send_window_start=campaign.send_window_start or time(9, 0),
                send_window_end=campaign.send_window_end, timezone_str=campaign.campaign_timezone or "UTC",
                base_date=campaign.start_date or date_type.today(), total_prospects=len(new), prospect_index=index,
                min_gap_minutes=max(int(raw_gap if raw_gap is not None else 2), 0),
            )
            if prospect.prospect_id in reactivated:
                enrollment_rules.reactivate_removed(self.uow.session, campaign.tenant_id, campaign_id,
                                                    [prospect.prospect_id], scheduled_at)
            else:
                repo.add_enrolment(CampaignProspect(campaign_id=campaign_id, prospect_id=prospect.prospect_id,
                                                    status="ACTIVE", enrolled_at=utcnow(),
                                                    next_scheduled_at=scheduled_at))
        repo.flush()
        return len(new)

    def default_steps(self, campaign_id: str, schedule: Optional[List[Dict]] = None) -> List[EmailSequence]:
        """Step 6: the sequence steps (Day 1, 3, 7 by default)."""
        steps = [EmailSequence(sequence_id=str(uuid.uuid4()), campaign_id=campaign_id, step_number=s["step"],
                               wait_days=s["wait_days"], stop_on_reply=True, stop_on_bounce=True)
                 for s in (schedule or DEFAULT_SCHEDULE)]
        for step in steps:
            self.uow.content.add(step)
        self.uow.content.flush()
        return steps

    def screen_list(self, tenant_id: str, campaign_id: str, list_id: str, rules: SegmentationRules,
                    cool_off_days: int, excluded_prospect_ids: Optional[List[str]]) -> Screened:
        """Wizard steps 2-4 for one list: what enrolment and its dry run both use (S-20)."""
        self.campaign(tenant_id, campaign_id)
        self.prospect_list(tenant_id, list_id)
        everyone = self.list_contacts(list_id, excluded_prospect_ids)
        segment_rejections: List[Dict] = []
        segmented = self.segment(everyone, rules, segment_rejections)
        safety = self.safety_checks(tenant_id, segmented, cool_off_days, campaign_id=campaign_id)
        return Screened(everyone, segmented, safety, segment_rejections + safety["rejections"])

    # ── routes ────────────────────────────────────────────────────────────────────────────────

    def available_lists(self, user: Any) -> List[Dict]:
        """The tenant's prospect lists with their member counts, for the wizard's list picker."""
        return [{"list_id": pl.list_id, "list_name": pl.list_name, "prospect_count": size,
                 "created_at": pl.uploaded_at} for pl, size in self.uow.wizard.lists_with_sizes(user.tenant_id)]

    def create_campaign(self, user: Any, campaign_name: str, send_start_hour: int, send_end_hour: int,
                        cta_link: Optional[str], inbox_ids: List[str]) -> Dict[str, Any]:
        """Wizard step 1; an invalid setting (e.g. inbox selection) is a BadRequestError."""
        with self.uow as uow:
            try:
                campaign = self.create(user.tenant_id, user.user_id, campaign_name, send_start_hour, send_end_hour,
                                       cta_link, inbox_ids)
            except ValueError as exc:
                raise BadRequestError(str(exc)) from exc
            uow.commit()
            return {"campaign_id": campaign.campaign_id, "campaign_name": campaign.campaign_name,
                    "status": campaign.status}

    def process_list(self, user: Any, campaign_id: str, list_id: str, **options: Any) -> Dict[str, Any]:
        """Classify the list's contacts and generate one template set per detected persona."""
        self.campaign(user.tenant_id, campaign_id)
        self.prospect_list(user.tenant_id, list_id)
        return batch_processor_service.process_prospect_list(db=self.uow.session, list_id=list_id,
                                                             campaign_id=campaign_id, tenant_id=user.tenant_id,
                                                             **options)

    def generate_persona_template(self, user: Any, campaign_id: str, request: PersonaGeneration) -> Dict:
        """
        (Re)generate one persona's templates for the campaign. Works for any canonical persona, even one
        with no classified prospects yet. Only this persona's previous templates are replaced.

        No transaction is open during the LLM call (T-1): the inputs are read (and new persona
        classifications saved) and committed first; afterwards one short transaction re-checks the
        campaign and swaps the persona's old templates for the new ones.
        """
        with self.uow as uow:
            campaign = self.campaign(user.tenant_id, campaign_id)
            self.prospect_list(user.tenant_id, request.list_id)
            email_context = detect_email_context(request.product_name, request.campaign_description)
            cta_link = normalize_cta_link(campaign.cta_link)
            # Reuse (or create) classifications for this list; may yield 0 prospects for this persona.
            prospects = batch_processor_service.batch_classify_prospects(
                uow.session, request.list_id, user.tenant_id, request.excluded_prospect_ids
            ).get(request.persona_type, [])
            blueprint = batch_processor_service.load_persona_blueprint(uow.session, campaign_id, request.persona_type)
            uow.commit()  # keep the classifications; no locks or snapshot held across the LLM call
        sequence, fallback_reason = batch_processor_service.generate_persona_sequence(
            request.persona_type, blueprint, request.product_name, request.campaign_description,
            request.sequence_steps, request.include_first_name_in_subject, request.creative_email, email_context,
            cta_link)
        return self._replace_persona_templates(campaign_id, request, prospects, sequence, fallback_reason, cta_link)

    def _replace_persona_templates(self, campaign_id: str, request: PersonaGeneration, prospects: List[dict],
                                   sequence: Optional[dict], fallback_reason: Optional[str], cta_link: str) -> Dict:
        """The short write transaction after generation: re-reads the campaign (it may have been deleted while
        the LLM ran) and its current steps, then replaces this persona's templates all-or-nothing."""
        with self.uow as uow:
            if not uow.campaigns.exists(campaign_id):
                raise NotFoundError("Campaign not found")
            step_to_seq_id = {s.step_number: s.sequence_id for s in uow.content.steps(campaign_id)}
            uow.content.delete_persona_templates(campaign_id, request.persona_type)
            result = batch_processor_service.save_persona_templates(
                uow.session, campaign_id, request.persona_type, prospects, sequence, fallback_reason,
                request.sequence_steps, request.include_first_name_in_subject, cta_link,
                has_effective_cta_link(cta_link), step_to_seq_id)
            uow.commit()
            return result

    def preview_segmentation(self, user: Any, list_id: str, rules: SegmentationRules) -> Dict:
        """Counts after segmentation and safety checks for a list, without enrolling anyone."""
        self.prospect_list(user.tenant_id, list_id)
        everyone = self.list_contacts(list_id)
        if not everyone:
            raise NotFoundError("No prospects found in this list")
        segment_rejections: List[Dict] = []
        segmented = self.segment(everyone, rules, segment_rejections)
        safety = self.safety_checks(user.tenant_id, segmented)
        return {
            "list_total": len(everyone),
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

    def check_list_enrollment(self, tenant_id: str, campaign_id: str, list_id: str, rules: SegmentationRules,
                              cool_off_days: int, excluded_prospect_ids: Optional[List[str]]) -> Dict:
        """Dry run of enroll_list: who would be enrolled and why the others would not."""
        screened = self.screen_list(tenant_id, campaign_id, list_id, rules, cool_off_days, excluded_prospect_ids)
        return {"list_total": len(screened.all),
                **enrollment_rules.check(screened.safety["safe_prospects"], screened.rejections)}

    def enroll_list(self, user: Any, campaign_id: str, list_id: str, rules: SegmentationRules, cool_off_days: int,
                    excluded_prospect_ids: Optional[List[str]]) -> Dict:
        """Segment a list, run the safety checks and bulk-enroll whoever passes."""
        with self.uow as uow:
            screened = self.screen_list(user.tenant_id, campaign_id, list_id, rules, cool_off_days,
                                        excluded_prospect_ids)
            safety = screened.safety
            logger.info("[ENROLL] list %s: %s contacts, %s after segmentation, %s safe",
                        list_id, len(screened.all), len(screened.segmented), safety["safe_count"])
            enrolled = self.bulk_enroll(campaign_id, safety["safe_prospects"])
            uow.commit()
            logger.info("[ENROLL] Step 5 - Enrolled: %s", enrolled)
            return {
                "campaign_id": campaign_id,
                "enrolled_count": enrolled,
                "sequences_created": 0,
                "status": "READY_TO_ACTIVATE",
                **_rejection_report(screened.rejections),
                "_debug": {
                    "prospects_in_list": len(screened.all),
                    "after_segmentation": len(screened.segmented),
                    "safe_count": safety["safe_count"],
                    "unsubscribed": safety["unsubscribed_count"],
                    "invalid": safety["invalid_count"],
                    "cool_off": safety["cool_off_count"],
                },
            }

    def activate(self, user: Any, campaign_id: str, expected_version: Optional[int] = None) -> Dict[str, Any]:
        """Wizard activation: no sender identity -> BadRequestError, not activatable -> ConflictError, a stale
        expected_version (the campaign's row_version the wizard loaded, T-3) -> 409. The answer carries
        ``pacing_warning`` when the plan outruns the mailboxes."""
        with self.uow as uow:
            campaign = self.campaign(user.tenant_id, campaign_id)
            uow.claim_version(campaign, expected_version, "campaign")
            try:
                launched = CampaignEmailService(self.db, uow=uow, audit=self._audit).launch(campaign_id, user.user_id)
            except SenderIdentityMissing as exc:
                raise BadRequestError(str(exc)) from exc
            except ValueError as exc:
                raise ConflictError(str(exc)) from exc
            if not launched:
                raise NotFoundError("Campaign not found")
            launched.launched_at = utcnow()
            uow.campaigns.add_state_event(campaign_id, "DRAFT", "ACTIVE", "Campaign activated via wizard")
            pacing = pacing_for(launched, uow.messages.planned_per_day(campaign_id))
            uow.commit()
            uow.campaigns.refresh(launched)
            return {
                "campaign_id": launched.campaign_id,
                "campaign_name": launched.campaign_name,
                "status": launched.status,
                "launched_at": getattr(launched, "launched_at", None),
                "row_version": launched.row_version,
                "pacing_warning": pacing,
            }

    def run_wizard(self, user: Any, campaign_name: str, list_id: str, rules: SegmentationRules,
                   cool_off_days: int) -> Dict:
        """The all-in-one wizard on one of the tenant's lists (create, segment, enrol, default steps)."""
        self.prospect_list(user.tenant_id, list_id)
        with self.uow as uow:
            try:
                campaign = self.create(user.tenant_id, user.user_id, campaign_name)
            except ValueError as exc:
                raise BadRequestError(str(exc)) from exc
            everyone = self.list_contacts(list_id)
            segmented = self.segment(everyone, rules)
            safety = self.safety_checks(user.tenant_id, segmented, cool_off_days)
            enrolled = self.bulk_enroll(campaign.campaign_id, safety["safe_prospects"])
            steps = self.default_steps(campaign.campaign_id)
            uow.commit()
            return {
                "campaign": campaign,
                "list_total": len(everyone),
                "after_segmentation": len(segmented),
                "unsubscribed": safety["unsubscribed_count"],
                "invalid_emails": safety["invalid_count"],
                "cool_off_excluded": safety["cool_off_count"],
                "enrolled": enrolled,
                "sequences_created": len(steps),
                "status": "DRAFT",  # Still needs activation
            }

    def check_enrollment_request(self, user: Any, campaign_id: str, request: EnrollmentCheckRequest) -> Dict:
        """
        POST /campaigns/{id}/enrollments/checks (S-20) for a tenant-scoped campaign: the dry run of the
        campaign page's enrolment (``path='campaign'``) or of the wizard's (``path='wizard'``).
        """
        campaign = self.campaign(user.tenant_id, campaign_id)
        if request.path == "wizard":
            rules = SegmentationRules(persona_types=request.persona_types,
                                      exclude_personal_emails=request.exclude_personal_emails)
            return self.check_list_enrollment(campaign.tenant_id, campaign.campaign_id, request.list_id or "", rules,
                                              request.cool_off_days, request.excluded_prospect_ids)
        enrol = CampaignEnrollmentRequest(list_ids=request.list_ids, prospect_ids=request.prospect_ids)
        return CampaignEmailService(self.db, uow=self.uow).check_enrollment(campaign, enrol)
