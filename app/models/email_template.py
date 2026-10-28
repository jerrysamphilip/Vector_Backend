# app/models/email_template.py
"""
AI Prompts, Email Templates, and Version History models.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime


class AIPrompt(Base):
    """
    AI prompt templates for email generation.
    Stores base prompts and persona-specific rules.
    """
    __tablename__ = "ai_prompts"

    prompt_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    base_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    persona_rules: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)

    # Relationships
    templates = relationship("EmailTemplate", back_populates="ai_prompt", lazy="dynamic")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<AIPrompt {self.prompt_id[:8]}...>"


class EmailTemplate(Base):
    """
    Email template with subject and body.
    Can be AI-generated or manually created.
    """
    __tablename__ = "email_templates"

    template_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.campaign_id"), nullable=False)
    sequence_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("email_sequences.sequence_id"),
                                                       nullable=True)

    # Template Content
    designation: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)  # Target designation for personalization
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # AI Generation Details
    tone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # professional / casual / formal
    ai_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    is_ai_generated: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    ai_prompt_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("ai_prompts.prompt_id"), nullable=True)
    # SHA256 hash for duplicate detection
    content_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Enhanced AI Generation Metadata
    model_settings: Mapped[Any] = mapped_column(JSON, nullable=True)  # {"temperature": 0.7, "max_tokens": 500}
    generated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)  # When AI generated this content
    cta_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # Call-to-action text
    cta_link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Call-to-action URL/Link
    personalization_tokens: Mapped[Any] = mapped_column(JSON, nullable=True)  # ["{{first_name}}", "{{company}}"]

    # Approval Workflow
    approved_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    # Retention
    # 12 months from creation for retention policy
    expires_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)

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

    def __repr__(self) -> str:
        return f"<EmailTemplate {self.subject[:30]}...>"


class EmailTemplateVersion(Base):
    """
    Version history for email templates.
    Tracks all edits for audit and rollback.
    """
    __tablename__ = "email_template_versions"

    version_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    template_id: Mapped[str] = mapped_column(String(36), ForeignKey("email_templates.template_id"), nullable=False)

    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)
    is_active: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=True)

    # Relationships
    template = relationship("EmailTemplate", back_populates="versions")
    creator = relationship("User", back_populates="template_versions")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<EmailTemplateVersion v{self.version_number}>"
