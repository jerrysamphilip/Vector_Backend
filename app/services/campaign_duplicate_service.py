"""
Duplicate a campaign (S-12, F-03): POST /campaigns/{id}/duplicates.

The copy is a new DRAFT of the same workspace, recorded with ``source_campaign_id``. Copied:
settings (sender, CTA, send window, timezone, sending mode, batch / daily sizes, unsubscribe mode),
sequence steps with their stop flags, every template (each persona's version of each step, approval
state kept), the steps' A/B subject variants (no winner), template attachment metadata (the copy
points at the same stored file) and automation rules (a SEND_EMAIL rule is pointed at the copied
template). Never copied: enrolments, emails and their events, metrics, state history.

Overrides (DuplicateOverrides): name, start / end date, send window, timezone, mailboxes. Without
``inbox_ids`` the source's mailboxes that can still send are kept and the others reported in
``skipped_inboxes``. Optionally lists / contacts are enrolled into the copy through the normal
enrolment rules (app/services/enrollment_rules.py), skipping contacts already in the source; the
result then lists the personas that have no template for some steps (``persona_gaps``).

One call is one unit of work (ADR 0009): everything is committed together or not at all.
"""

from __future__ import annotations

import copy
import uuid
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from app.core.errors import BadRequestError, NotFoundError
from app.domain.campaigns import (
    DuplicateOverrides,
    PersonaGap,
    check_send_window,
    copy_end_date,
    copy_start_date,
    duplicate_name,
    persona_gaps,
)
from app.models.automation_rule import AutomationRule
from app.models.campaign import Campaign
from app.models.email_attachment import EmailAttachment
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.models.subject_variant import EmailSubjectVariant
from app.repositories.campaigns import CampaignsRepository, CampaignsUnitOfWork
from app.schemas.campaign_schema import CampaignEnrollmentRequest, CampaignStatus
from app.services.ai_email_service import classify_prospect
from app.services.audit_service import AuditService
from app.services.campaign_crud_service import CampaignCRUDService
from app.services.campaign_email_service import CampaignEmailService

# Campaign settings copied as they are
_COPIED_SETTINGS = (
    "campaign_description", "sender_name", "sender_title", "cta_link", "unsubscribe_mode",
    "respect_timezone", "sending_mode", "min_gap_minutes", "batch_size", "batch_gap_minutes",
    "daily_batch_size",
)
_TEMPLATE_FIELDS = (
    "designation", "subject", "body", "tone", "ai_model", "is_ai_generated", "ai_prompt_id",
    "content_fingerprint", "generated_at", "cta_text", "cta_link", "approved_by", "approved_at", "expires_at",
)
_TEMPLATE_JSON_FIELDS = ("model_settings", "personalization_tokens")


@dataclass
class DuplicateResult:
    campaign_id: str
    source_campaign_id: str
    copied: Dict[str, int]
    skipped_inboxes: List[str] = field(default_factory=list)
    enrollment: Optional[Dict[str, Any]] = None
    enrolled_prospect_ids: List[str] = field(default_factory=list)
    persona_gaps: List[PersonaGap] = field(default_factory=list)


class CampaignDuplicateService:
    """``db`` is the request session (app.core.database.get_db); the service owns its unit of work."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._uow = CampaignsUnitOfWork.for_session(db)
        self._crud = CampaignCRUDService(db)
        self._audit = AuditService(db)

    def duplicate(self, tenant_id: str, user_id: str, source_id: str, opts: DuplicateOverrides) -> DuplicateResult:
        with self._uow as uow:
            repo = uow.campaigns
            source = repo.get(tenant_id, source_id)
            if source is None:
                raise NotFoundError("Campaign not found")
            try:
                campaign = self._new_campaign(source, user_id, opts)
                skipped = self._assign_inboxes(campaign, source, opts.inbox_ids)
            except ValueError as exc:
                raise BadRequestError(str(exc)) from exc
            repo.add(tenant_id, campaign)
            repo.flush()
            copied = self._copy_content(repo, tenant_id, source.campaign_id, campaign.campaign_id, opts)
            self._crud.metrics.get_or_create_metrics(campaign.campaign_id)
            result = DuplicateResult(campaign.campaign_id, source.campaign_id, copied, skipped)
            if opts.enrols:
                self._enrol(repo, tenant_id, user_id, source.campaign_id, campaign.campaign_id, opts, result)
            self._audit.log_action(
                tenant_id=tenant_id, user_id=user_id, action=f"{AuditService.ACTION_CREATE}_DUPLICATE",
                entity_type=AuditService.ENTITY_CAMPAIGN, entity_id=campaign.campaign_id,
            )
            uow.commit()
            return result

    # ── campaign + schedule ────────────────────────────────────────────────────────────────────

    @staticmethod
    def _new_campaign(source: Campaign, user_id: str, opts: DuplicateOverrides) -> Campaign:
        tz_name = opts.campaign_timezone or source.campaign_timezone or "UTC"
        try:
            today = datetime.now(zoneinfo.ZoneInfo(tz_name)).date()
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {tz_name}") from exc
        window_start = opts.send_window_start or source.send_window_start
        window_end = opts.send_window_end or source.send_window_end
        check_send_window(window_start, window_end)
        start = copy_start_date(opts.start_date, source.start_date, today)
        campaign = Campaign(
            campaign_id=str(uuid.uuid4()),
            tenant_id=source.tenant_id,
            campaign_name=duplicate_name(source.campaign_name, opts.campaign_name),
            status=CampaignStatus.DRAFT.value,
            created_by=user_id,
            campaign_timezone=tz_name,
            send_window_start=window_start,
            send_window_end=window_end,
            start_date=start,
            end_date=copy_end_date(opts.end_date, source.end_date, start, today),
            source_campaign_id=source.campaign_id,
        )
        for name in _COPIED_SETTINGS:
            setattr(campaign, name, getattr(source, name))
        return campaign

    def _assign_inboxes(self, campaign: Campaign, source: Campaign,
                        inbox_ids: Optional[Sequence[str]]) -> List[str]:
        """Explicit mailboxes are validated strictly; otherwise the source's usable ones are kept."""
        if inbox_ids is not None:
            campaign.inboxes = self._crud.validate_and_load_inboxes(campaign.tenant_id, list(inbox_ids))
            return []
        kept, skipped = [], []
        for inbox in source.inboxes or []:
            try:
                kept.extend(self._crud.validate_and_load_inboxes(campaign.tenant_id, [inbox.inbox_id]))
            except ValueError:
                skipped.append(inbox.email_address)
        campaign.inboxes = kept
        return skipped

    # ── content ────────────────────────────────────────────────────────────────────────────────

    def _copy_content(self, repo: CampaignsRepository, tenant_id: str, source_id: str, target_id: str,
                      opts: DuplicateOverrides) -> Dict[str, int]:
        seq_map = self._copy_sequences(repo, tenant_id, source_id, target_id)
        template_map = self._copy_templates(repo, tenant_id, source_id, target_id, seq_map)
        copied = {
            "sequences": len(seq_map),
            "templates": len(template_map),
            "subject_variants": self._copy_variants(repo, tenant_id, source_id, target_id, seq_map),
            "attachments": 0,
            "automation_rules": 0,
        }
        if opts.copy_attachments:
            copied["attachments"] = self._copy_attachments(repo, tenant_id, template_map)
        if opts.copy_automation_rules:
            copied["automation_rules"] = self._copy_rules(repo, tenant_id, source_id, target_id, template_map)
        repo.flush()
        return copied

    @staticmethod
    def _copy_sequences(repo: CampaignsRepository, tenant_id: str, source_id: str, target_id: str) -> Dict[str, str]:
        seq_map: Dict[str, str] = {}
        for step in repo.sequences(tenant_id, source_id):
            new = EmailSequence(
                sequence_id=str(uuid.uuid4()), campaign_id=target_id, step_number=step.step_number,
                wait_days=step.wait_days, stop_on_reply=step.stop_on_reply, stop_on_bounce=step.stop_on_bounce,
                send_start_hour=step.send_start_hour, send_end_hour=step.send_end_hour,
            )
            repo.add(tenant_id, new)
            seq_map[step.sequence_id] = new.sequence_id
        repo.flush()
        return seq_map

    @staticmethod
    def _copy_templates(repo: CampaignsRepository, tenant_id: str, source_id: str, target_id: str,
                        seq_map: Dict[str, str]) -> Dict[str, str]:
        """Each persona's version of each step; templates keep their {{tokens}}, so they fit any contacts."""
        template_map: Dict[str, str] = {}
        for t in repo.templates(tenant_id, source_id):
            new = EmailTemplate(template_id=str(uuid.uuid4()), campaign_id=target_id,
                                sequence_id=seq_map.get(t.sequence_id) if t.sequence_id else None)
            for name in _TEMPLATE_FIELDS:
                setattr(new, name, getattr(t, name))
            for name in _TEMPLATE_JSON_FIELDS:
                setattr(new, name, copy.deepcopy(getattr(t, name)))
            repo.add(tenant_id, new)
            template_map[t.template_id] = new.template_id
        repo.flush()
        return template_map

    @staticmethod
    def _copy_variants(repo: CampaignsRepository, tenant_id: str, source_id: str, target_id: str,
                       seq_map: Dict[str, str]) -> int:
        count = 0
        for v in repo.subject_variants(tenant_id, source_id):
            if v.sequence_id not in seq_map:
                continue
            repo.add(tenant_id, EmailSubjectVariant(
                variant_id=str(uuid.uuid4()), tenant_id=tenant_id, campaign_id=target_id,
                sequence_id=seq_map[v.sequence_id], label=v.label, subject=v.subject, weight=v.weight,
                is_winner=False, created_by=v.created_by,
            ))
            count += 1
        return count

    @staticmethod
    def _copy_attachments(repo: CampaignsRepository, tenant_id: str, template_map: Dict[str, str]) -> int:
        attachments = repo.attachments(list(template_map))
        for a in attachments:
            repo.add(tenant_id, EmailAttachment(
                attachment_id=str(uuid.uuid4()), template_id=template_map[a.template_id], filename=a.filename,
                content_type=a.content_type, size_bytes=a.size_bytes, storage_path=a.storage_path,
            ))
        return len(attachments)

    @staticmethod
    def _copy_rules(repo: CampaignsRepository, tenant_id: str, source_id: str, target_id: str,
                    template_map: Dict[str, str]) -> int:
        rules = repo.automation_rules(tenant_id, source_id)
        for r in rules:
            action_config = copy.deepcopy(r.action_config)
            if isinstance(action_config, dict) and action_config.get("template_id") in template_map:
                action_config["template_id"] = template_map[action_config["template_id"]]
            repo.add(tenant_id, AutomationRule(
                rule_id=str(uuid.uuid4()), campaign_id=target_id, name=r.name, description=r.description,
                trigger_type=r.trigger_type, trigger_config=copy.deepcopy(r.trigger_config),
                action_type=r.action_type, action_config=action_config, is_active=r.is_active,
            ))
        return len(rules)

    # ── contacts ───────────────────────────────────────────────────────────────────────────────

    def _enrol(self, repo: CampaignsRepository, tenant_id: str, user_id: str, source_id: str, target_id: str,
               opts: DuplicateOverrides, result: DuplicateResult) -> None:
        skip = repo.enrolled_prospect_ids(tenant_id, source_id) if opts.skip_source_prospects else set()
        request = CampaignEnrollmentRequest(list_ids=list(opts.list_ids) or None,
                                            prospect_ids=list(opts.prospect_ids) or None)
        try:
            count, rejections = CampaignEmailService(self._db).enroll_prospects_with_report(
                target_id, user_id, request, skip_ids=skip)
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        result.enrollment = {"enrolled_count": count, "rejections": rejections}
        result.enrolled_prospect_ids = sorted(repo.enrolled_prospect_ids(tenant_id, target_id))
        counts: Dict[str, int] = {}
        for prospect, persona in repo.enrolled_personas(tenant_id, target_id):
            persona = persona or classify_prospect(prospect.designation or "", prospect.company_name)[0]
            counts[persona] = counts.get(persona, 0) + 1
        result.persona_gaps = persona_gaps(counts, repo.designations_by_step(tenant_id, target_id))
