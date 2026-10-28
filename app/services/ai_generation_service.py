"""
AI email generation routes (persona classification, blueprint and sequence generation, compliance
checks): one method per route of app/routers/ai_email_router.py.

Contacts are looked up in the caller's tenant (another tenant's id is 404). Persona blueprints are
global and only platform admins create them (the route's role gate). LLM use goes through
app/services/ai_email_service.py / sequence_generator.py (LLM budget, rule-based fallback). No
sqlalchemy / fastapi imports: SQL lives in app/repositories/personas.py.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.errors import BadRequestError, NotFoundError
from app.models.persona_blueprint import PersonaBlueprint
from app.models.prospect import Prospect
from app.repositories.personas import PersonasUnitOfWork, Session, SqlAlchemyPersonasUnitOfWork
from app.services.ai_email_service import (
    PERSONA_DISPLAY_NAMES, classify_prospect, generate_email_for_prospect, get_or_create_prospect_persona,
)
from app.services.compliance_service import is_business_email, quick_compliance_check
from app.services.sequence_generator import SequenceGeneratorService, get_cta_options
from app.utils.email_utils import normalize_cta_link

__all__ = ["AiGenerationService", "CTA_PURPOSES"]

CTA_PURPOSES = ["intro", "reminder", "last_chance"]


class AiGenerationService:
    def __init__(self, uow: PersonasUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> AiGenerationService:
        return cls(SqlAlchemyPersonasUnitOfWork.for_session(db))

    # no database -----------------------------------------------------------------
    @staticmethod
    def classify_test(designation: str, company_name: Any) -> dict:
        persona_type, confidence = classify_prospect(designation=designation, company_name=company_name)
        return {"designation": designation, "persona_type": persona_type, "confidence_score": confidence}

    @staticmethod
    def compliance_check(subject: str, body: str) -> Dict[str, Any]:
        return quick_compliance_check(subject, body)

    @staticmethod
    def validate_email(email: str) -> Dict[str, Any]:
        domain = email.split("@")[-1] if "@" in email else ""
        return {"email": email, "is_business": is_business_email(email), "domain": domain}

    @staticmethod
    def cta_options(purpose: str) -> Dict[str, Any]:
        if purpose not in CTA_PURPOSES:
            raise BadRequestError(f"Invalid purpose. Use: {', '.join(CTA_PURPOSES)}")
        return {"purpose": purpose, "options": get_cta_options(purpose)}

    @staticmethod
    def personas() -> List[dict]:
        """The user-selectable personas (OTHER is the auto-classification fallback, not a choice)."""
        return [{"persona_type": persona_type, "persona_label": label}
                for persona_type, label in PERSONA_DISPLAY_NAMES.items() if persona_type != "OTHER"]

    # contacts --------------------------------------------------------------------
    def _contact(self, uow: PersonasUnitOfWork, prospect_id: str, user: Any) -> Prospect:
        prospect = uow.personas.contact(user.tenant_id, prospect_id)
        if not prospect:
            raise NotFoundError(f"Prospect {prospect_id} not found")
        return prospect

    def classify_batch(self, prospect_ids: List[str], user: Any) -> List[dict]:
        """Classify (and store) the persona of each of the tenant's contacts; unknown ids are skipped."""
        with self.uow as uow:
            results = []
            for prospect_id in prospect_ids:
                prospect = uow.personas.contact(user.tenant_id, prospect_id)
                if not prospect:
                    continue
                persona = get_or_create_prospect_persona(uow.session, prospect)
                results.append({
                    "prospect_id": prospect_id,
                    "persona_type": persona.persona_type,
                    "confidence_score": persona.confidence_score,
                    "classification_method": persona.classification_method,
                })
            return results

    def generate_email(self, prospect_id: str, product_name: Any, cta_link: Optional[str],
                       user: Any) -> Dict[str, str]:
        with self.uow as uow:
            prospect = self._contact(uow, prospect_id, user)
            result = generate_email_for_prospect(uow.session, prospect, product_name, normalize_cta_link(cta_link))
            if not result:
                raise BadRequestError("No blueprint available for this persona type")
            return result

    def generate_sequence(self, prospect_id: str, product_name: str, product_description: str, use_llm: bool,
                          cta_link: str, user: Any) -> Dict[str, Any]:
        with self.uow as uow:
            self._contact(uow, prospect_id, user)
            result: Dict[str, Any] = SequenceGeneratorService(uow.session).generate_email_sequence(
                prospect_id=prospect_id,
                product_name=product_name,
                product_description=product_description,
                use_llm=use_llm,
                cta_link=normalize_cta_link(cta_link),
            )
            if "error" in result:
                raise BadRequestError(result["error"])
            return result

    # blueprints --------------------------------------------------------------------
    def blueprints(self, active_only: bool) -> List[PersonaBlueprint]:
        with self.uow as uow:
            return uow.personas.blueprints(active_only)

    def blueprint(self, persona_type: str) -> PersonaBlueprint:
        with self.uow as uow:
            blueprint = uow.personas.blueprint_for_type(persona_type.upper())
            if not blueprint:
                raise NotFoundError(f"Blueprint for {persona_type} not found")
            return blueprint

    def create_blueprint(self, data: Any) -> PersonaBlueprint:
        with self.uow as uow:
            if uow.personas.blueprint_for_type(data.persona_type):
                raise BadRequestError(f"Blueprint for {data.persona_type} already exists")
            blueprint = uow.personas.add(PersonaBlueprint(
                persona_type=data.persona_type,
                openers=data.openers,
                value_angles=data.value_angles,
                ctas=data.ctas,
                proof_points=data.proof_points,
                tone_rules=data.tone_rules,
            ))
            uow.commit()
            uow.personas.refresh(blueprint)
            return blueprint
