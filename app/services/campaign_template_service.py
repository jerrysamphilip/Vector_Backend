# app/services/campaign_template_service.py
"""
Templates (chunk A2, SQL in app/repositories/campaigns_content.py):

* ``CampaignTemplateService``: the campaign page's template actions: AI-regenerate one template (F-05:
  any persona version, one at a time or each in turn), approve one, approve all pending. A regenerated
  template of a launched campaign is pushed into its unsent emails.
* ``EmailTemplateService``: the /templates API (app/routers/template_router.py): create, read, edit with
  version history, approve, delete, and the template's attachments.

Every route method is one unit of work and finds the template through a campaign of the caller's tenant.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.core.clock import utcnow
from app.core.config import settings
from app.core.errors import BadRequestError, DomainError, NotFoundError, OperationFailed
from app.domain.campaigns_edit import LAUNCHED_STATUSES
from app.models.campaign import Campaign
from app.models.email_attachment import EmailAttachment
from app.models.email_template import EmailTemplate, EmailTemplateVersion
from app.models.persona_blueprint import PersonaBlueprint
from app.repositories.campaigns import CampaignsUnitOfWork
from app.schemas.template_schema import (EmailAttachmentResponse, EmailTemplateCreate, EmailTemplateResponse,
                                         EmailTemplateUpdate, TemplateVersionResponse)
from app.services import ai_email_service
from app.services.audit_service import AuditService
from app.services.campaign_common import require_campaign
from app.services.campaign_email_service import CampaignEmailService
from app.services.sender_identity import bind_tenant
from app.utils.email_utils import finalize_email_body, has_effective_cta_link, normalize_cta_link

logger = logging.getLogger(__name__)

DEFAULT_AI_MODEL = "gpt-4o-mini"
FALLBACK_PERSONA = "OTHER"
TEMPLATE_NOT_FOUND = "Template not found"

# Placeholders the LLM writes into a template; resolved per contact at send time
TEMPLATE_PROSPECT_TOKENS = {
    "first_name": "{{first_name}}",
    "last_name": "{{last_name}}",
    "company_name": "{{company_name}}",
    "industry": "{{industry}}",
    "city": "{{city}}",
    "state": "{{state}}",
    "linkedin_url": "{{linkedin_url}}",
}


def fallback_template(step_number: int, designation: str, cta_link: Optional[str]) -> Dict[str, str]:
    """Starter content used when the LLM returns nothing."""
    cta_line = f"\n\nWould you be open to a quick call?: {cta_link}" if has_effective_cta_link(cta_link) else ""
    return {
        "subject": f"[Step {step_number}] {designation} Outreach",
        "body": (f"Hi {{{{first_name}}}},\n\nI noticed your role as {designation} and wanted to reach out."
                 f"{cta_line}\n\nRegards,\n{{{{signature_block}}}}"),
        "model_used": "fallback",
    }


def content_hash(subject: str, body: str) -> str:
    """Content fingerprint for duplicate detection."""
    return hashlib.sha256(f"{subject}|{body}".encode()).hexdigest()


@dataclass(frozen=True)
class Regeneration:
    tone: Optional[str]
    regenerate_subject: bool = True
    regenerate_body: bool = True
    creative_email: bool = False
    custom_instruction: Optional[str] = None


class CampaignTemplateService:
    def __init__(self, db: Any = None, *, uow: Any = None) -> None:
        self.uow = uow if uow is not None else CampaignsUnitOfWork.for_session(db)
        self.db = db

    @classmethod
    def for_session(cls, db: Any) -> "CampaignTemplateService":
        return cls(db)

    def _get_template(self, campaign_id: str, template_id: str) -> EmailTemplate:
        template = self.uow.content.template(campaign_id, template_id)
        if not template:
            raise NotFoundError(TEMPLATE_NOT_FOUND)
        return template

    def _step_number(self, template: EmailTemplate) -> int:
        seq = self.uow.content.step_by_id(template.sequence_id) if template.sequence_id else None
        return seq.step_number if seq else 1

    def _blueprint_for(self, persona_type: str) -> PersonaBlueprint:
        blueprint = self.uow.content.blueprint(persona_type) or self.uow.content.blueprint(FALLBACK_PERSONA)
        if not blueprint:
            raise OperationFailed("No persona blueprint found")
        return blueprint

    # ── routes ────────────────────────────────────────────────────────────────────────────────

    def regenerate_for(self, user: Any, campaign_id: str, template_id: str, request: Regeneration) -> EmailTemplate:
        campaign = require_campaign(self.uow.campaigns, user.tenant_id, campaign_id)
        return self.regenerate(campaign, template_id, tone=request.tone,
                               regenerate_subject=request.regenerate_subject,
                               regenerate_body=request.regenerate_body, creative_email=request.creative_email,
                               custom_instruction=request.custom_instruction)

    def approve_for(self, user: Any, campaign_id: str, template_id: str) -> EmailTemplate:
        require_campaign(self.uow.campaigns, user.tenant_id, campaign_id)
        return self.approve(campaign_id, template_id, user.user_id)

    def approve_all_for(self, user: Any, campaign_id: str) -> List[str]:
        require_campaign(self.uow.campaigns, user.tenant_id, campaign_id)
        return self.approve_all(campaign_id, user.user_id)

    def regenerate(
        self,
        campaign: Campaign,
        template_id: str,
        *,
        tone: Optional[str],
        regenerate_subject: bool,
        regenerate_body: bool,
        creative_email: bool,
        custom_instruction: Optional[str],
    ) -> EmailTemplate:
        """Rewrite the template with the LLM (falls back to starter content) and clear its approval. The
        LLM call goes through ai_email_service (LLM budget, rule-based fallback without a key)."""
        template = self._get_template(campaign.campaign_id, template_id)
        step_number = self._step_number(template)
        designation = template.designation or "General"
        try:
            persona_type, _ = ai_email_service.classify_prospect(designation)
            blueprint = self._blueprint_for(persona_type)
            cta_link = normalize_cta_link(campaign.cta_link)
            bind_tenant(campaign.tenant_id, self.uow.session)
            result = ai_email_service.generate_email_with_llm(
                prospect_data={**TEMPLATE_PROSPECT_TOKENS, "designation": designation},
                blueprint=blueprint,
                product_name=campaign.campaign_name,
                product_description=(campaign.campaign_description or f"Email campaign: {campaign.campaign_name}"),
                cta_link=cta_link,
                creative_email=creative_email,
                custom_instruction=custom_instruction or "",  # callee only checks truthiness
                step_number=step_number,  # follow-ups get the follow-up tone, not the intro's
            ) or fallback_template(step_number, designation, cta_link)
            with self.uow as uow:
                self._apply_regeneration(template, result, tone, regenerate_subject, regenerate_body)
                if campaign.status in LAUNCHED_STATUSES:
                    # The unsent emails built from it follow (they are rendered per contact at launch)
                    CampaignEmailService(self.db, uow=uow).propagate_template_edit(template.template_id)
                uow.commit()
            return template
        except DomainError:
            raise
        except Exception as e:  # safety net around the LLM call: surfaced as the API's 500 message
            logger.error("[regenerate_template] AI generation failed for %s: %s", template_id, e, exc_info=True)
            raise OperationFailed(f"AI generation failed: {str(e)}") from e

    @staticmethod
    def _apply_regeneration(template: EmailTemplate, result: Dict[str, Any], tone: Optional[str],
                            regenerate_subject: bool, regenerate_body: bool) -> None:
        if regenerate_subject:
            template.subject = result.get("subject", template.subject)
        if regenerate_body:
            template.body = finalize_email_body(result.get("body", template.body))
        template.tone = tone
        template.ai_model = result.get("model_used", DEFAULT_AI_MODEL)
        template.is_ai_generated = True
        template.generated_at = utcnow()
        template.approved_by = None
        template.approved_at = None

    def approve(self, campaign_id: str, template_id: str, user_id: str) -> EmailTemplate:
        with self.uow as uow:
            template = self._get_template(campaign_id, template_id)
            template.approved_by = user_id
            template.approved_at = utcnow()
            uow.commit()
            return template

    def approve_all(self, campaign_id: str, user_id: str) -> List[str]:
        """Approve every not-yet-approved template of the campaign; returns their IDs."""
        with self.uow as uow:
            approved_ids = []
            for template in uow.content.unapproved_templates(campaign_id):
                template.approved_by = user_id
                template.approved_at = utcnow()
                approved_ids.append(template.template_id)
            uow.commit()
            return approved_ids


class EmailTemplateService:
    """The /templates API. Templates have no tenant of their own: another tenant's template is "not found"."""

    def __init__(self, db: Any = None, *, uow: Any = None, audit: Any = None) -> None:
        self.uow = uow if uow is not None else CampaignsUnitOfWork.for_session(db)
        self.db = db
        self._audit = audit

    @classmethod
    def for_session(cls, db: Any) -> "EmailTemplateService":
        return cls(db)

    @property
    def audit(self) -> AuditService:
        if self._audit is None:
            self._audit = AuditService(self.uow.session)
        return self._audit

    def _log(self, user: Any, action: str, template_id: str) -> None:
        self.audit.log_action(tenant_id=user.tenant_id, user_id=user.user_id, action=action,
                              entity_type=AuditService.ENTITY_TEMPLATE, entity_id=template_id)

    def template_for(self, user: Any, template_id: str) -> EmailTemplate:
        template = self.uow.content.template_for_tenant(user.tenant_id, template_id)
        if not template:
            raise NotFoundError(TEMPLATE_NOT_FOUND)
        return template

    def to_response(self, template: EmailTemplate) -> EmailTemplateResponse:
        content = self.uow.content
        return EmailTemplateResponse(
            template_id=template.template_id, campaign_id=template.campaign_id, sequence_id=template.sequence_id,
            subject=template.subject, body=template.body, designation=template.designation, tone=template.tone,
            is_ai_generated=template.is_ai_generated, ai_model=template.ai_model, ai_prompt_id=template.ai_prompt_id,
            content_fingerprint=template.content_fingerprint, approved_by=template.approved_by,
            approved_at=template.approved_at, is_approved=template.approved_by is not None,
            created_at=template.created_at, version_count=content.version_count(template.template_id),
            attachments=[EmailAttachmentResponse.model_validate(a) for a in content.attachments_of(template.template_id)],
        )

    # ── templates ─────────────────────────────────────────────────────────────────────────────

    def create(self, user: Any, data: EmailTemplateCreate) -> EmailTemplateResponse:
        """A template for one of the caller's campaigns (and one of its steps), with version 1."""
        with self.uow as uow:
            require_campaign(uow.campaigns, user.tenant_id, data.campaign_id)
            if data.sequence_id and not uow.content.step_in_campaign(data.campaign_id, data.sequence_id):
                raise NotFoundError("Sequence not found")
            body = finalize_email_body(data.body) if data.body else data.body
            template = EmailTemplate(
                template_id=str(uuid.uuid4()), campaign_id=data.campaign_id, sequence_id=data.sequence_id,
                subject=data.subject, body=body, designation=data.designation,
                tone=data.tone.value if data.tone else None, is_ai_generated=data.is_ai_generated,
                ai_prompt_id=data.ai_prompt_id, content_fingerprint=content_hash(data.subject, data.body),
            )
            uow.content.add(template)
            uow.content.add(EmailTemplateVersion(
                version_id=str(uuid.uuid4()), template_id=template.template_id, subject=data.subject, body=body,
                version_number=1, created_by=user.user_id, is_active=True))
            self._log(user, AuditService.ACTION_CREATE, template.template_id)
            uow.commit()
            return self.to_response(template)

    def get(self, user: Any, template_id: str) -> EmailTemplateResponse:
        return self.to_response(self.template_for(user, template_id))

    def update(self, user: Any, template_id: str, data: EmailTemplateUpdate) -> EmailTemplateResponse:
        """Edit with a new version in the history; clears approval; unsent emails built from it follow."""
        with self.uow as uow:
            template = self.template_for(user, template_id)
            max_version = uow.content.version_count(template_id)
            if data.subject is not None:
                template.subject = data.subject
            if data.body is not None:
                template.body = finalize_email_body(data.body)
            if data.designation is not None:
                template.designation = data.designation
            if data.tone is not None:
                template.tone = data.tone.value
            template.content_fingerprint = content_hash(template.subject, template.body)
            template.approved_by = None
            template.approved_at = None
            version = EmailTemplateVersion(
                version_id=str(uuid.uuid4()), template_id=template_id, subject=template.subject, body=template.body,
                version_number=max_version + 1, created_by=user.user_id, is_active=True)
            uow.content.add(version)
            uow.content.flush()
            uow.content.deactivate_other_versions(template_id, version.version_id)
            # The emails are rendered per contact at launch: push the edit into the unsent ones
            CampaignEmailService(self.db, uow=uow).propagate_template_edit(template_id)
            self._log(user, AuditService.ACTION_UPDATE, template_id)
            uow.commit()
            return self.to_response(template)

    def approve(self, user: Any, template_id: str) -> EmailTemplateResponse:
        """The approver is always the caller (an approved_by in the body is ignored, so it can't be spoofed)."""
        with self.uow as uow:
            template = self.template_for(user, template_id)
            template.approved_by = user.user_id
            template.approved_at = utcnow()
            self._log(user, AuditService.ACTION_APPROVE, template_id)
            uow.commit()
            return self.to_response(template)

    def versions(self, user: Any, template_id: str) -> List[TemplateVersionResponse]:
        self.template_for(user, template_id)
        return [TemplateVersionResponse(version_id=v.version_id, template_id=v.template_id, subject=v.subject,
                                        body=v.body, version_number=v.version_number, created_by=v.created_by,
                                        created_at=v.created_at, is_active=v.is_active)
                for v in self.uow.content.versions(template_id)]

    def delete(self, user: Any, template_id: str) -> None:
        with self.uow as uow:
            template = self.template_for(user, template_id)
            self._log(user, AuditService.ACTION_DELETE, template_id)
            uow.content.delete_versions(template_id)
            uow.content.delete(template)
            uow.commit()

    # ── attachments ───────────────────────────────────────────────────────────────────────────

    def add_attachment(self, user: Any, template_id: str, filename: Optional[str], content_type: Optional[str],
                       contents: bytes, store: Any) -> EmailAttachmentResponse:
        """Store an uploaded file for the template (Gmail-style compose attachment): size, count and emptiness
        are checked first; the stored name is unique."""
        with self.uow as uow:
            self.template_for(user, template_id)
            if uow.content.attachment_count(template_id) >= settings.MAX_ATTACHMENTS_PER_TEMPLATE:
                raise BadRequestError(
                    f"Maximum of {settings.MAX_ATTACHMENTS_PER_TEMPLATE} attachments per email reached")
            if len(contents) > settings.MAX_ATTACHMENT_SIZE_MB * 1024 * 1024:
                raise BadRequestError(f"File exceeds the {settings.MAX_ATTACHMENT_SIZE_MB}MB attachment size limit")
            if not contents:
                raise BadRequestError("File is empty")
            original_name = os.path.basename(filename or "attachment")
            stored_name = f"{uuid.uuid4()}_{original_name}"
            store.put(stored_name, contents)
            attachment = EmailAttachment(attachment_id=str(uuid.uuid4()), template_id=template_id,
                                         filename=original_name, content_type=content_type,
                                         size_bytes=len(contents), storage_path=stored_name)
            uow.content.add(attachment)
            uow.commit()
            uow.content.refresh(attachment)
            return EmailAttachmentResponse.model_validate(attachment)

    def attachments(self, user: Any, template_id: str) -> List[EmailAttachmentResponse]:
        self.template_for(user, template_id)
        return [EmailAttachmentResponse.model_validate(a) for a in self.uow.content.attachments_of(template_id)]

    def attachment_for(self, user: Any, template_id: str, attachment_id: str) -> EmailAttachment:
        self.template_for(user, template_id)
        attachment = self.uow.content.attachment(template_id, attachment_id)
        if not attachment:
            raise NotFoundError("Attachment not found")
        return attachment

    def delete_attachment(self, user: Any, template_id: str, attachment_id: str, store: Any) -> None:
        """Remove the row; the stored file only when no other attachment (a duplicated campaign) uses it."""
        with self.uow as uow:
            attachment = self.attachment_for(user, template_id, attachment_id)
            if not uow.content.storage_shared(attachment.storage_path, attachment.attachment_id) \
                    and store.exists(attachment.storage_path):
                try:
                    store.delete(attachment.storage_path)
                except OSError:
                    pass
            uow.content.delete(attachment)
            uow.commit()
