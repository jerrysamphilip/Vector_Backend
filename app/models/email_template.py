# app/models/email_template.py
"""
AI Prompts, Email Templates, and Version History models.
"""

from sqlalchemy import Column, String, Boolean, Integer, Text, TIMESTAMP, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models.base import Base


class AIPrompt(Base):
    """
    AI prompt templates for email generation.
    Stores base prompts and persona-specific rules.
    """
    __tablename__ = "ai_prompts"

    prompt_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    base_prompt = Column(Text, nullable=False)
    persona_rules = Column(JSON, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    templates = relationship("EmailTemplate", back_populates="ai_prompt", lazy="dynamic")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<AIPrompt {self.prompt_id[:8]}...>"


class EmailTemplate(Base):
    """
    Email template with subject and body.
    Can be AI-generated or manually created.
    """
    __tablename__ = "email_templates"

    template_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String(36), ForeignKey("campaigns.campaign_id"), nullable=False)
    sequence_id = Column(String(36), ForeignKey("email_sequences.sequence_id"), nullable=True)

    # Template Content
    designation = Column(String(150), nullable=True)  # Target designation for personalization
    subject = Column(Text, nullable=False)
    body = Column(Text, nullable=False)

    # AI Generation Details
    tone = Column(String(50), nullable=True)  # professional / casual / formal
    ai_model = Column(String(100), nullable=True)
    is_ai_generated = Column(Boolean, default=False)
    ai_prompt_id = Column(String(36), ForeignKey("ai_prompts.prompt_id"), nullable=True)
    content_fingerprint = Column(String(64), nullable=True)  # SHA256 hash for duplicate detection
    
    # Enhanced AI Generation Metadata
    model_settings = Column(JSON, nullable=True)  # {"temperature": 0.7, "max_tokens": 500}
    generated_at = Column(TIMESTAMP, nullable=True)  # When AI generated this content
    cta_text = Column(String(255), nullable=True)  # Call-to-action text
    cta_link = Column(Text, nullable=True)  # Call-to-action URL/Link
    personalization_tokens = Column(JSON, nullable=True)  # ["{{first_name}}", "{{company}}"]

    # Approval Workflow
    approved_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    approved_at = Column(TIMESTAMP, nullable=True)
    
    # Retention
    expires_at = Column(TIMESTAMP, nullable=True)  # 12 months from creation for retention policy
    
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    campaign = relationship("Campaign", back_populates="templates")
    sequence = relationship("EmailSequence", back_populates="templates")
    ai_prompt = relationship("AIPrompt", back_populates="templates")
    approver = relationship("User", back_populates="approved_templates")
    versions = relationship("EmailTemplateVersion", back_populates="template", lazy="dynamic")
    attachments = relationship("EmailAttachment", back_populates="template", cascade="all, delete-orphan")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<EmailTemplate {self.subject[:30]}...>"


class EmailTemplateVersion(Base):
    """
    Version history for email templates.
    Tracks all edits for audit and rollback.
    """
    __tablename__ = "email_template_versions"

    version_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    template_id = Column(String(36), ForeignKey("email_templates.template_id"), nullable=False)
    
    subject = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    version_number = Column(Integer, nullable=False)
    
    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    is_active = Column(Boolean, default=True)

    # Relationships
    template = relationship("EmailTemplate", back_populates="versions")
    creator = relationship("User", back_populates="template_versions")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<EmailTemplateVersion v{self.version_number}>"
