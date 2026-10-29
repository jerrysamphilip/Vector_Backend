"""
Campaign content (ADR 0009, chunk A2): sequence steps, persona templates with their version history and
attachments, the steps' A/B subject variants and the persona blueprints used to (re)write templates.

Part of the campaign aggregate (``CampaignsUnitOfWork.content``). Templates and steps have no
tenant_id of their own: the ``*_for_tenant`` lookups join their campaign; the other helpers take ids
the caller already scoped. Nothing here commits.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.compliance_check import ComplianceCheck
from app.models.email_attachment import EmailAttachment
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate, EmailTemplateVersion
from app.models.persona_blueprint import PersonaBlueprint
from app.models.subject_variant import EmailSubjectVariant
from app.repositories.base import TenantId




class CampaignContentRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, entity: object) -> None:
        self._s.add(entity)

    def delete(self, entity: object) -> None:
        self._s.delete(entity)

    def flush(self) -> None:
        self._s.flush()

    # ── steps ──────────────────────────────────────────────────────────────────────────────────

    def steps(self, campaign_id: str) -> list[EmailSequence]:
        return list(self._s.execute(select(EmailSequence).where(EmailSequence.campaign_id == campaign_id)
                                    .order_by(EmailSequence.step_number)).scalars())

    def step(self, campaign_id: str, step_number: int) -> Optional[EmailSequence]:
        return self._s.execute(select(EmailSequence).where(
            EmailSequence.campaign_id == campaign_id, EmailSequence.step_number == step_number)).scalars().first()

    def step_by_id(self, sequence_id: str) -> Optional[EmailSequence]:
        return self._s.get(EmailSequence, sequence_id)

    def step_for_tenant(self, tenant_id: TenantId, sequence_id: str) -> Optional[EmailSequence]:
        return self._s.execute(select(EmailSequence).join(
            Campaign, Campaign.campaign_id == EmailSequence.campaign_id
        ).where(EmailSequence.sequence_id == sequence_id, Campaign.tenant_id == tenant_id)).scalars().first()

    def step_in_campaign(self, campaign_id: str, sequence_id: str) -> bool:
        return self._s.execute(select(EmailSequence.sequence_id).where(
            EmailSequence.sequence_id == sequence_id, EmailSequence.campaign_id == campaign_id)).first() is not None

    def step_count(self, campaign_id: str) -> int:
        return int(self._s.execute(select(func.count(EmailSequence.sequence_id)).where(
            EmailSequence.campaign_id == campaign_id)).scalar() or 0)

    def total_wait_days(self, campaign_id: str) -> int:
        return int(self._s.execute(select(func.sum(EmailSequence.wait_days)).where(
            EmailSequence.campaign_id == campaign_id)).scalar() or 0)

    def step_numbers(self, campaign_id: str) -> dict[str, int]:
        return {sid: number for sid, number in self._s.execute(
            select(EmailSequence.sequence_id, EmailSequence.step_number)
            .where(EmailSequence.campaign_id == campaign_id)).all()}

    # ── templates ──────────────────────────────────────────────────────────────────────────────

    def templates(self, campaign_id: str) -> list[EmailTemplate]:
        return list(self._s.execute(select(EmailTemplate).where(EmailTemplate.campaign_id == campaign_id)).scalars())

    def template(self, campaign_id: str, template_id: str) -> Optional[EmailTemplate]:
        return self._s.execute(select(EmailTemplate).where(
            EmailTemplate.template_id == template_id, EmailTemplate.campaign_id == campaign_id)).scalars().first()

    def template_by_id(self, template_id: str) -> Optional[EmailTemplate]:
        return self._s.get(EmailTemplate, template_id)

    def template_for_tenant(self, tenant_id: TenantId, template_id: str) -> Optional[EmailTemplate]:
        """A template of one of the tenant's campaigns (templates are owned through their campaign)."""
        return self._s.execute(select(EmailTemplate).join(
            Campaign, Campaign.campaign_id == EmailTemplate.campaign_id
        ).where(EmailTemplate.template_id == template_id, Campaign.tenant_id == tenant_id)).scalars().first()

    def templates_by_ids(self, template_ids: Iterable[str]) -> dict[str, EmailTemplate]:
        ids = list(set(template_ids))
        if not ids:
            return {}
        return {t.template_id: t for t in self._s.execute(
            select(EmailTemplate).where(EmailTemplate.template_id.in_(ids))).scalars()}

    def templates_of_step(self, sequence_id: str) -> list[EmailTemplate]:
        return list(self._s.execute(select(EmailTemplate).where(EmailTemplate.sequence_id == sequence_id)
                                    .order_by(EmailTemplate.created_at, EmailTemplate.template_id)).scalars())

    def first_template_of_step(self, sequence_id: str) -> Optional[EmailTemplate]:
        return self._s.execute(select(EmailTemplate).where(EmailTemplate.sequence_id == sequence_id)).scalars().first()

    def unapproved_templates(self, campaign_id: str) -> list[EmailTemplate]:
        return list(self._s.execute(select(EmailTemplate).where(
            EmailTemplate.campaign_id == campaign_id, EmailTemplate.approved_by.is_(None))).scalars())

    def has_template(self, campaign_id: str, sequence_id: str, designation: Optional[str]) -> bool:
        """Whether the step has a template for the designation (None: a generic one, NULL or '')."""
        q = select(EmailTemplate.template_id).where(EmailTemplate.campaign_id == campaign_id,
                                                    EmailTemplate.sequence_id == sequence_id)
        if designation:
            q = q.where(EmailTemplate.designation == designation)
        else:
            q = q.where(or_(EmailTemplate.designation.is_(None), EmailTemplate.designation == ""))
        return self._s.execute(q).first() is not None

    def delete_persona_templates(self, campaign_id: str, designation: str) -> None:
        self._s.query(EmailTemplate).filter(EmailTemplate.campaign_id == campaign_id,
                                            EmailTemplate.designation == designation).delete(synchronize_session=False)

    def step_siblings(self, campaign_id: str, sequence_id: str, template_id: str) -> list[EmailTemplate]:
        """The step's other persona templates."""
        return list(self._s.execute(select(EmailTemplate).where(
            EmailTemplate.campaign_id == campaign_id, EmailTemplate.sequence_id == sequence_id,
            EmailTemplate.template_id != template_id)).scalars())

    def delete_step_templates(self, sequence_id: str) -> int:
        """A step's templates with their versions, compliance checks and attachment rows."""
        templates = self.templates_of_step(sequence_id)
        ids = [t.template_id for t in templates]
        if not ids:
            return 0
        self._s.query(EmailTemplateVersion).filter(EmailTemplateVersion.template_id.in_(ids)).delete(
            synchronize_session=False)
        self._s.query(ComplianceCheck).filter(ComplianceCheck.template_id.in_(ids)).delete(synchronize_session=False)
        for t in templates:
            self._s.delete(t)  # ORM delete: attachment rows cascade
        self._s.flush()
        return len(ids)

    # ── versions ───────────────────────────────────────────────────────────────────────────────

    def version_count(self, template_id: str) -> int:
        return int(self._s.execute(select(func.count(EmailTemplateVersion.version_id)).where(
            EmailTemplateVersion.template_id == template_id)).scalar() or 0)

    def versions(self, template_id: str) -> list[EmailTemplateVersion]:
        return list(self._s.execute(select(EmailTemplateVersion).where(
            EmailTemplateVersion.template_id == template_id
        ).order_by(EmailTemplateVersion.version_number.desc())).scalars())

    def deactivate_other_versions(self, template_id: str, keep_version_id: str) -> None:
        self._s.query(EmailTemplateVersion).filter(
            EmailTemplateVersion.template_id == template_id, EmailTemplateVersion.version_id != keep_version_id,
        ).update({"is_active": False})

    def delete_versions(self, template_id: str) -> None:
        self._s.query(EmailTemplateVersion).filter(EmailTemplateVersion.template_id == template_id).delete()

    # ── attachments ────────────────────────────────────────────────────────────────────────────

    def attachments_of(self, template_id: str) -> list[EmailAttachment]:
        return list(self._s.execute(select(EmailAttachment).where(EmailAttachment.template_id == template_id)
                                    .order_by(EmailAttachment.created_at.asc())).scalars())

    def attachment_count(self, template_id: str) -> int:
        return int(self._s.execute(select(func.count(EmailAttachment.attachment_id)).where(
            EmailAttachment.template_id == template_id)).scalar() or 0)

    def attachment(self, template_id: str, attachment_id: str) -> Optional[EmailAttachment]:
        return self._s.execute(select(EmailAttachment).where(
            EmailAttachment.attachment_id == attachment_id, EmailAttachment.template_id == template_id)).scalars().first()

    def storage_shared(self, storage_path: str, attachment_id: str) -> bool:
        """Whether another attachment row points at the same stored file (a duplicated campaign)."""
        return self._s.execute(select(EmailAttachment.attachment_id).where(
            EmailAttachment.storage_path == storage_path, EmailAttachment.attachment_id != attachment_id)).first() \
            is not None

    def refresh(self, entity: object) -> None:
        self._s.refresh(entity)

    # ── subject variants ───────────────────────────────────────────────────────────────────────

    def variants(self, sequence_id: str) -> list[EmailSubjectVariant]:
        return list(self._s.execute(select(EmailSubjectVariant).where(
            EmailSubjectVariant.sequence_id == sequence_id)).scalars())

    def variant(self, sequence_id: str, label: str) -> Optional[EmailSubjectVariant]:
        return self._s.execute(select(EmailSubjectVariant).where(
            EmailSubjectVariant.sequence_id == sequence_id, EmailSubjectVariant.label == label)).scalars().first()

    def variants_of_steps(self, campaign_id: str, sequence_ids: Sequence[str]) -> list[EmailSubjectVariant]:
        if not sequence_ids:
            return []
        return list(self._s.execute(select(EmailSubjectVariant).where(
            EmailSubjectVariant.campaign_id == campaign_id,
            EmailSubjectVariant.sequence_id.in_(list(sequence_ids)))).scalars())

    def delete_step_variants(self, sequence_id: str) -> None:
        self._s.query(EmailSubjectVariant).filter(EmailSubjectVariant.sequence_id == sequence_id).delete(
            synchronize_session=False)

    # ── persona blueprints ─────────────────────────────────────────────────────────────────────

    def blueprint(self, persona_type: str, active_only: bool = False) -> Optional[PersonaBlueprint]:
        q = select(PersonaBlueprint).where(PersonaBlueprint.persona_type == persona_type)
        if active_only:
            q = q.where(PersonaBlueprint.is_active.is_(True))
        return self._s.execute(q).scalars().first()
