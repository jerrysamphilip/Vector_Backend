# app/models/email_attachment.py
"""
File attachments for email templates.
Uploaded files are stored on local disk (see app.core.config.ATTACHMENTS_DIR);
this table stores the metadata + path needed to serve and to attach at send time.
"""

from sqlalchemy import Column, String, Integer, TIMESTAMP, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models.base import Base


class EmailAttachment(Base):
    __tablename__ = "email_attachments"

    attachment_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    template_id = Column(String(36), ForeignKey("email_templates.template_id"), nullable=False)

    filename = Column(String(255), nullable=False)
    content_type = Column(String(150), nullable=True)
    size_bytes = Column(Integer, nullable=False)
    storage_path = Column(String(500), nullable=False)  # path relative to ATTACHMENTS_DIR

    created_at = Column(TIMESTAMP, server_default=func.now())

    template = relationship("EmailTemplate", back_populates="attachments")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<EmailAttachment {self.filename}>"
