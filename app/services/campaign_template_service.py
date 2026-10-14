# app/services/campaign_template_service.py
"""
Campaign template actions from the campaign page: AI-regenerate one template, approve one,
approve all pending.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.errors import DomainError, NotFoundError, OperationFailed
from app.models.campaign import Campaign
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.models.persona_blueprint import PersonaBlueprint
from app.services import ai_email_service
from app.utils.email_utils import finalize_email_body, has_effective_cta_link, normalize_cta_link

logger = logging.getLogger(__name__)

DEFAULT_AI_MODEL = "gpt-4o-mini"
FALLBACK_PERSONA = "OTHER"

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


class CampaignTemplateService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _get_template(self, campaign_id: str, template_id: str) -> EmailTemplate:
        template = self.db.query(EmailTemplate).filter(
            EmailTemplate.template_id == template_id,
            EmailTemplate.campaign_id == campaign_id
        ).first()
        if not template:
            raise NotFoundError("Template not found")
        return template

    def _step_number(self, template: EmailTemplate) -> int:
        if template.sequence_id:
            seq = self.db.query(EmailSequence).filter(EmailSequence.sequence_id == template.sequence_id).first()
            if seq:
                return seq.step_number
        return 1

    def _blueprint_for(self, persona_type: str) -> PersonaBlueprint:
        blueprint = (
            self.db.query(PersonaBlueprint).filter(PersonaBlueprint.persona_type == persona_type).first()
            or self.db.query(PersonaBlueprint).filter(PersonaBlueprint.persona_type == FALLBACK_PERSONA).first()
        )
        if not blueprint:
            raise OperationFailed("No persona blueprint found")
        return blueprint

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
        """Rewrite the template with the LLM (falls back to starter content) and clear its approval."""
        template = self._get_template(campaign.campaign_id, template_id)
        step_number = self._step_number(template)
        designation = template.designation or "General"

        try:
            persona_type, _ = ai_email_service.classify_prospect(designation)
            blueprint = self._blueprint_for(persona_type)
            cta_link = normalize_cta_link(campaign.cta_link)

            from app.services.sender_identity import bind_tenant
            bind_tenant(campaign.tenant_id, self.db)
            result = ai_email_service.generate_email_with_llm(
                prospect_data={**TEMPLATE_PROSPECT_TOKENS, "designation": designation},
                blueprint=blueprint,
                product_name=campaign.campaign_name,
                product_description=(campaign.campaign_description or f"Email campaign: {campaign.campaign_name}"),
                cta_link=cta_link,
                creative_email=creative_email,
                custom_instruction=custom_instruction or "",  # callee only checks truthiness
            ) or fallback_template(step_number, designation, cta_link)

            if regenerate_subject:
                template.subject = result.get("subject", template.subject)
            if regenerate_body:
                template.body = finalize_email_body(result.get("body", template.body))
            template.tone = tone
            template.ai_model = result.get("model_used", DEFAULT_AI_MODEL)
            template.is_ai_generated = True
            template.generated_at = datetime.utcnow()
            template.approved_by = None
            template.approved_at = None
            self.db.commit()
            return template
        except DomainError:
            raise
        except Exception as e:  # safety net around the LLM call: surfaced as the API's 500 message
            logger.error("[regenerate_template] AI generation failed for %s: %s", template_id, e, exc_info=True)
            raise OperationFailed(f"AI generation failed: {str(e)}") from e

    def approve(self, campaign_id: str, template_id: str, user_id: str) -> EmailTemplate:
        template = self._get_template(campaign_id, template_id)
        template.approved_by = user_id
        template.approved_at = datetime.utcnow()
        self.db.commit()
        return template

    def approve_all(self, campaign_id: str, user_id: str) -> List[str]:
        """Approve every not-yet-approved template of the campaign; returns their IDs."""
        templates = self.db.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == campaign_id,
            EmailTemplate.approved_by.is_(None)
        ).all()
        approved_ids = []
        for template in templates:
            template.approved_by = user_id
            template.approved_at = datetime.utcnow()
            approved_ids.append(template.template_id)
        self.db.commit()
        return approved_ids
