# app/models/compliance_check.py
"""
Compliance Check model for email content validation.
Tracks GDPR, CAN-SPAM, spam score, and disallowed phrases.
"""

from sqlalchemy import Column, String, Boolean, Float, ForeignKey, JSON, Index
from sqlalchemy.orm import relationship

from app.models import Base
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id, short_id


class ComplianceCheck(Base):
    """
    Compliance check result for an email template.
    Validates content against spam triggers, GDPR, and CAN-SPAM requirements.
    """
    __tablename__ = "compliance_checks"

    check_id = Column(String(36), primary_key=True, default=new_id)
    template_id = Column(String(36), ForeignKey("email_templates.template_id"), nullable=False)
    # The template's tenant (D3, 0028_child_tenant_ids); filled from the template by app/models/tenant_fill.py
    tenant_id = Column(String(36), nullable=False)

    # Spam Detection
    spam_score = Column(Float, default=0.0)  # 0-100 scale
    spam_triggers_found = Column(JSON, nullable=True)  # List of flagged words/phrases
    
    # Required Elements
    has_unsubscribe_link = Column(Boolean, default=False)
    has_physical_address = Column(Boolean, default=False)  # CAN-SPAM requirement
    
    # Disallowed Content
    has_disallowed_phrases = Column(Boolean, default=False)
    disallowed_phrases_found = Column(JSON, nullable=True)  # List of flagged phrases
    
    # Personal Data Check
    contains_personal_data = Column(Boolean, default=False)  # SSN, credit card, etc.
    personal_data_types = Column(JSON, nullable=True)  # Types of PII found
    
    # Regional Compliance
    gdpr_compliant = Column(Boolean, default=True)
    can_spam_compliant = Column(Boolean, default=True)
    region = Column(String(50), nullable=True)  # US, EU, APAC
    
    # Overall Result
    status = Column(String(50), default="pending")  # pending, passed, warning, failed
    failure_reasons = Column(JSON, nullable=True)  # List of reasons for failure
    
    # Metadata
    checked_at = Column(UtcDateTime, server_default=NOW6)
    checked_by = Column(String(50), default="system")  # system or user_id

    # Relationships
    template = relationship("EmailTemplate", backref="compliance_checks")

    __table_args__ = (
        Index("ix_compliance_checks_tenant_template", "tenant_id", "template_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<ComplianceCheck {short_id(self.check_id)}... ({self.status})>"
    
    @property
    def is_approved(self) -> bool:
        """Check if content passed all compliance checks."""
        return self.status == "passed"
    
    @property
    def needs_review(self) -> bool:
        """Check if content needs manual review."""
        return self.status == "warning"
