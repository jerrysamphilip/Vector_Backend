# app/services/campaign_wizard_template_service.py
"""
Campaign-wizard template editing: regenerate one field (subject or body) of a template with the
LLM, or save the user's manual edit and propagate it to the step's other persona variants.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, DomainError, ExternalServiceUnavailable, NotFoundError, OperationFailed
from app.models import Campaign, EmailTemplate
from app.prompts.regeneration_prompts import REGENERATABLE_FIELDS, SYSTEM_PROMPT, build_regeneration_prompt
from app.utils.email_utils import finalize_email_body, has_effective_cta_link, strip_cta_content_no_link
from app.utils.retry_utils import retry_with_backoff

logger = logging.getLogger(__name__)

SIGNATURE_SUFFIX = "\n\nRegards,\n{{your_name}}"
_TRAILING_SIGNATURE_TOKEN = re.compile(
    r'(?i)(<p>|^\s*|<br\s*/?>)*\s*(Best(\s+Regards)?|Sincerely|Regards|Thanks|Cheers)?,?(\s*<br\s*/?>)?\s*'
    r'(\{|\[)*your_name(\}|\])*(<\/p>)?\s*$'
)
_TRAILING_SIGN_OFF = re.compile(r'(?i)\n+\s*(Best(\s+Regards)?|Sincerely|Regards|Thanks|Cheers),?\s*$')


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers (unit-tested)
# ──────────────────────────────────────────────────────────────────────────────

def strip_trailing_signature(content: str) -> str:
    """Remove every trailing sign-off / {{your_name}} line the LLM may have written."""
    while True:
        prev_len = len(content)
        content = _TRAILING_SIGNATURE_TOKEN.sub('', content).strip()
        content = _TRAILING_SIGN_OFF.sub('', content).strip()
        if len(content) == prev_len:
            return content


def prepare_regenerated_body(content: str, cta_link: Optional[str]) -> Tuple[str, str]:
    """LLM body -> (content with exactly one standard signature, finalized template body).
    Without an effective CTA link any CTA text is removed first."""
    if not has_effective_cta_link(cta_link):
        content = strip_cta_content_no_link(content)
    content = f"{strip_trailing_signature(content + SIGNATURE_SUFFIX)}{SIGNATURE_SUFFIX}"
    return content, finalize_email_body(content)


# ──────────────────────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────────────────────

class CampaignWizardTemplateService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_template_for_tenant(self, template_id: str, tenant_id: str) -> EmailTemplate:
        template = self.db.query(EmailTemplate).join(
            Campaign, Campaign.campaign_id == EmailTemplate.campaign_id
        ).filter(
            EmailTemplate.template_id == template_id,
            Campaign.tenant_id == tenant_id,
        ).first()
        if not template:
            raise NotFoundError("Template not found")
        return template

    def regenerate_field(
        self,
        tenant_id: str,
        template_id: str,
        *,
        field: str,
        campaign_description: str,
        persona_type: str,
        product_name: str,
        cta_link: Optional[str],
        custom_instruction: Optional[str],
        creative_email: bool,
        step_number: int,
    ) -> Dict[str, Any]:
        """Ask the LLM for a new subject or body, store it and return the new content."""
        from app.services.sender_identity import bind_tenant
        bind_tenant(tenant_id, self.db)
        # Looked up at call time so the configured (or test-patched) client is used
        from app.services.sequence_generator import openai_client
        from app.core.config import settings

        template = self.get_template_for_tenant(template_id, tenant_id)
        if field not in REGENERATABLE_FIELDS:
            raise BadRequestError("Field must be 'subject' or 'body'")

        prompt = build_regeneration_prompt(
            field=field,
            current_subject=template.subject,
            campaign_description=campaign_description,
            persona_type=persona_type,
            product_name=product_name,
            cta_link=cta_link,
            custom_instruction=custom_instruction,
            creative_email=creative_email,
            step_number=step_number,
        )

        if openai_client is None:
            # No OpenAI key configured: a clear 503 instead of retrying and failing with a 500
            raise ExternalServiceUnavailable("AI generation is not configured on this server.")

        def _call_openai() -> Any:
            return openai_client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.8,
                max_tokens=500,
            )

        try:
            response = retry_with_backoff(_call_openai, max_attempts=3, base_delay=1.0, max_delay=30.0)
            if response is None:
                logger.error("[regenerate_email_part] All retry attempts exhausted.")
                raise ExternalServiceUnavailable(
                    "AI service is temporarily unavailable. Please try again in a few moments.")

            new_content = response.choices[0].message.content.strip()
            if field == 'subject':
                template.subject = new_content
            else:
                new_content, template.body = prepare_regenerated_body(new_content, cta_link)
            self.db.commit()
            return {
                "template_id": template.template_id,
                "field": field,
                "new_content": new_content,
                "subject": template.subject,
                "body": template.body,
            }
        except DomainError:
            raise
        except Exception as e:  # safety net around the LLM call: surfaced as the API's 500 message
            logger.error("[regenerate_email_part] Unexpected error: %s", e)
            raise OperationFailed(f"Failed to regenerate: {str(e)}") from e

    def save_manual_edit(
        self, tenant_id: str, template_id: str, subject: Optional[str], body: Optional[str]
    ) -> EmailTemplate:
        """
        Save the user's subject/body edit. Each step has one template per persona group but the UI
        shows only one, so the edit is copied to every other template of the same campaign step;
        otherwise the other personas would silently keep stale content.
        """
        template = self.get_template_for_tenant(template_id, tenant_id)
        if subject is not None:
            template.subject = subject
        if body is not None:
            template.body = finalize_email_body(body)

        if template.sequence_id:
            siblings = self.db.query(EmailTemplate).filter(
                EmailTemplate.campaign_id == template.campaign_id,
                EmailTemplate.sequence_id == template.sequence_id,
                EmailTemplate.template_id != template.template_id,
            ).all()
            for sibling in siblings:
                if subject is not None:
                    sibling.subject = subject
                if body is not None:
                    sibling.body = template.body

        self.db.commit()
        return template
