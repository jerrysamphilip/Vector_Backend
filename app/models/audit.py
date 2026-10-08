# app/models/audit.py
"""
Audit Log model for compliance and accountability.
"""

from sqlalchemy import Column, String, TIMESTAMP, ForeignKey
from sqlalchemy.sql import func
import uuid

from app.models import Base


class AuditLog(Base):
    """
    Audit trail for user actions.
    Required for GDPR and CAN-SPAM compliance.
    """
    __tablename__ = "audit_logs"

    log_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=True)  # NULL once the user is deleted
    
    action = Column(String(255), nullable=False)  # CREATE_CAMPAIGN, LAUNCH_CAMPAIGN, etc
    entity_type = Column(String(100), nullable=False)  # campaign, prospect, template, etc
    entity_id = Column(String(36), nullable=True)
    
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<AuditLog {self.action} on {self.entity_type}>"
