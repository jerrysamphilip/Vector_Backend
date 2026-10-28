# app/models/email_attachment.py
"""
File attachments for email templates.
Uploaded files are stored on local disk (see app.core.config.ATTACHMENTS_DIR);
this table stores the metadata + path needed to serve and to attach at send time.
"""

from sqlalchemy import Column, String, Integer, ForeignKey, Index
from sqlalchemy.orm import relationship

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id


class EmailAttachment(Base):
    __tablename__ = "email_attachments"

    attachment_id = Column(String(36), primary_key=True, default=new_id)
    template_id = Column(String(36), ForeignKey("email_templates.template_id"), nullable=False)
    # The template's tenant (D3, 0028_child_tenant_ids); filled from the template by app/models/tenant_fill.py
    tenant_id = Column(String(36), nullable=False)

    filename = Column(String(255), nullable=False)
    content_type = Column(String(150), nullable=True)
    size_bytes = Column(Integer, nullable=False)
    storage_path = Column(String(500), nullable=False)  # path relative to ATTACHMENTS_DIR

    created_at = Column(UtcDateTime, server_default=NOW6)

    template = relationship("EmailTemplate", back_populates="attachments")

    __table_args__ = (
        Index("ix_email_attachments_tenant_template", "tenant_id", "template_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<EmailAttachment {self.filename}>"
