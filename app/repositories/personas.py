"""
Personas repository and unit of work (ADR 0009): persona blueprints (global generation recipes, no
tenant), the persona inferred for each contact, and the tenant's contacts the AI routes generate for.
Contact lookups take the tenant; blueprints are platform-wide by design. Never commits.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any, Optional, Protocol

from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.email_attachment import EmailAttachment
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate, EmailTemplateVersion
from app.models.persona_blueprint import PersonaBlueprint
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectListMember
from app.models.prospect_persona import ProspectPersona
from app.repositories.base import SqlAlchemyUnitOfWork, TenantId

__all__ = [
    "PersonaRepository", "PersonasUnitOfWork", "Session", "SqlAlchemyPersonaCampaignRepository",
    "SqlAlchemyPersonaRepository", "SqlAlchemyPersonasUnitOfWork", "personas_repo",
]


class PersonaRepository(Protocol):
    def contact(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]: ...
    def persona_of(self, prospect_id: str) -> Optional[ProspectPersona]: ...
    def active_blueprint(self, persona_type: str) -> Optional[PersonaBlueprint]: ...
    def blueprint(self, blueprint_id: str) -> Optional[PersonaBlueprint]: ...
    def blueprint_for_type(self, persona_type: str) -> Optional[PersonaBlueprint]: ...
    def blueprints(self, active_only: bool) -> list[PersonaBlueprint]: ...
    def add(self, entity: Any) -> Any: ...
    def refresh(self, entity: Any) -> None: ...


class SqlAlchemyPersonaRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def contact(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]:
        return self._session.query(Prospect).filter(Prospect.prospect_id == prospect_id,
                                                    Prospect.tenant_id == tenant_id).first()

    def persona_of(self, prospect_id: str) -> Optional[ProspectPersona]:
        return self._session.query(ProspectPersona).filter(ProspectPersona.prospect_id == prospect_id).first()

    def active_blueprint(self, persona_type: str) -> Optional[PersonaBlueprint]:
        return self._session.query(PersonaBlueprint).filter(PersonaBlueprint.persona_type == persona_type,
                                                            PersonaBlueprint.is_active == True).first()  # noqa: E712

    def blueprint(self, blueprint_id: str) -> Optional[PersonaBlueprint]:
        return self._session.query(PersonaBlueprint).filter(PersonaBlueprint.blueprint_id == blueprint_id).first()

    def blueprint_for_type(self, persona_type: str) -> Optional[PersonaBlueprint]:
        """Active or not (blueprint types are unique)."""
        return self._session.query(PersonaBlueprint).filter(PersonaBlueprint.persona_type == persona_type).first()

    def blueprints(self, active_only: bool) -> list[PersonaBlueprint]:
        query = self._session.query(PersonaBlueprint)
        if active_only:
            query = query.filter(PersonaBlueprint.is_active == True)  # noqa: E712
        return query.all()

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def refresh(self, entity: Any) -> None:
        self._session.refresh(entity)


class SqlAlchemyPersonaCampaignRepository:
    """What per-persona template generation for a campaign reads and replaces (the wizard's batch step)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_contacts(self, tenant_id: TenantId, list_id: str,
                      excluded_ids: Optional[Sequence[str]] = None) -> list[Prospect]:
        query = (
            self._session.query(Prospect)
            .join(ProspectListMember, ProspectListMember.prospect_id == Prospect.prospect_id)
            .filter(ProspectListMember.list_id == list_id, Prospect.tenant_id == tenant_id)
        )
        if excluded_ids:
            query = query.filter(Prospect.prospect_id.notin_(list(excluded_ids)))
        return query.all()

    def personas_for(self, prospect_ids: Collection[str]) -> list[ProspectPersona]:
        return self._session.query(ProspectPersona).filter(
            ProspectPersona.prospect_id.in_(list(prospect_ids))).all()

    def add_all(self, entities: Sequence[Any]) -> None:
        self._session.add_all(list(entities))

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def campaign_tenant(self, campaign_id: str) -> Optional[str]:
        tenant_id: Optional[str] = self._session.query(Campaign.tenant_id).filter(
            Campaign.campaign_id == campaign_id).scalar()
        return tenant_id

    def detached_active_blueprint(self, persona_type: str) -> Optional[PersonaBlueprint]:
        """The active blueprint, expunged so it stays usable after the read transaction ends."""
        blueprint = self._session.query(PersonaBlueprint).filter(
            PersonaBlueprint.persona_type == persona_type,
            PersonaBlueprint.is_active == True  # noqa: E712  (SQL expression)
        ).first()
        if blueprint is not None:
            self._session.expunge(blueprint)
        return blueprint

    def delete_campaign_templates(self, campaign_id: str) -> None:
        """The campaign's templates with their versions and attachments (re-generation replaces them)."""
        old_ids = [t for (t,) in self._session.query(EmailTemplate.template_id).filter(
            EmailTemplate.campaign_id == campaign_id).all()]
        if old_ids:
            self._session.query(EmailTemplateVersion).filter(
                EmailTemplateVersion.template_id.in_(old_ids)).delete(synchronize_session=False)
            self._session.query(EmailAttachment).filter(
                EmailAttachment.template_id.in_(old_ids)).delete(synchronize_session=False)
        self._session.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == campaign_id).delete(synchronize_session=False)

    def campaign(self, campaign_id: str) -> Optional[Campaign]:
        return self._session.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()

    def sequences(self, campaign_id: str) -> list[EmailSequence]:
        return self._session.query(EmailSequence).filter(EmailSequence.campaign_id == campaign_id).all()


def personas_repo(session: Session) -> SqlAlchemyPersonaRepository:
    """For the legacy ``fn(db, ...)`` helpers in app/services/ai_email_service.py."""
    return SqlAlchemyPersonaRepository(session)


class PersonasUnitOfWork(Protocol):
    @property
    def personas(self) -> PersonaRepository: ...

    @property
    def session(self) -> Session: ...

    def __enter__(self) -> PersonasUnitOfWork: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyPersonasUnitOfWork(SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyPersonasUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> SqlAlchemyPersonasUnitOfWork:
        super().__enter__()
        return self

    @property
    def personas(self) -> SqlAlchemyPersonaRepository:
        return SqlAlchemyPersonaRepository(self.session)
