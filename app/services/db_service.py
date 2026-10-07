# app/services/db_service.py

from sqlalchemy.orm import Session
from app.models.prospect_model import Prospect
from app.schemas.prospect_schema import ProspectCreate
from app.core.database import SessionLocal
import uuid


# ============================
# HELPERS
# ============================

def uuid_to_bin(u: uuid.UUID) -> bytes:
    """Convert UUID to MySQL BINARY(16)"""
    return u.bytes


def new_uuid_bin() -> bytes:
    """Generate new UUID BINARY(16)"""
    return uuid.uuid4().bytes



# ============================
# DB OPERATIONS
# ============================

def save_prospect(prospect_data: ProspectCreate) -> Prospect:
    """
    Save a prospect to MySQL database.
    One prospect per call.
    """

    db: Session = SessionLocal()

    try:
        db_prospect = Prospect(
            prospect_id=new_uuid_bin(),
            tenant_id=uuid_to_bin(prospect_data.tenant_id),

            research_month=prospect_data.research_month,
            research_by=prospect_data.research_by,
            source_sheet=prospect_data.source_sheet,
            disposition=prospect_data.disposition,

            first_name=prospect_data.first_name,
            last_name=prospect_data.last_name,
            designation=prospect_data.designation,
            company_name=prospect_data.company_name,

            email=prospect_data.email,
            email_domain=prospect_data.email_domain,
            is_valid_email=prospect_data.is_valid_email,
            is_business_email=prospect_data.is_business_email,

            linkedin_url=prospect_data.linkedin_url,
            poc_city=prospect_data.poc_city,
            poc_state=prospect_data.poc_state,
            emp_band=prospect_data.emp_band,
            industry=prospect_data.industry,

            consent_status=prospect_data.consent_status,
            domain_confidence=prospect_data.domain_confidence,
            is_anonymized=prospect_data.is_anonymized,
        )

        db.add(db_prospect)
        db.commit()
        db.refresh(db_prospect)

        return db_prospect

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()
