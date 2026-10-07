# app/services/audit_service.py
"""
Audit logging service for compliance and accountability.
"""

from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import uuid

from app.models.audit import AuditLog


class AuditService:
    """Service for logging user actions."""
    
    # Action types
    ACTION_CREATE = "CREATE"
    ACTION_UPDATE = "UPDATE"
    ACTION_DELETE = "DELETE"
    ACTION_PAUSE = "PAUSE"
    ACTION_RESUME = "RESUME"
    ACTION_LAUNCH = "LAUNCH"
    ACTION_APPROVE = "APPROVE"
    
    # Entity types
    ENTITY_CAMPAIGN = "campaign"
    ENTITY_SEQUENCE = "sequence"
    ENTITY_TEMPLATE = "template"
    ENTITY_PROSPECT = "prospect"
    ENTITY_LIST = "prospect_list"
    
    def __init__(self, db: Session):
        self.db = db
    
    def log_action(
        self,
        tenant_id: str,
        user_id: str,
        action: str,
        entity_type: str,
        entity_id: Optional[str] = None
    ) -> AuditLog:
        """
        Log a user action for audit trail.
        
        Args:
            tenant_id: Tenant ID
            user_id: User performing the action
            action: Action type (CREATE, UPDATE, etc.)
            entity_type: Entity type (campaign, template, etc.)
            entity_id: ID of the affected entity
        
        Returns:
            Created AuditLog entry
        """
        log = AuditLog(
            log_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            action=f"{action}_{entity_type.upper()}",
            entity_type=entity_type,
            entity_id=entity_id,
        )
        
        self.db.add(log)
        self.db.flush()  # Get the ID without committing
        
        return log
    
    def get_audit_trail(
        self,
        tenant_id: str,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 50
    ) -> List[AuditLog]:
        """
        Get audit trail with optional filters.
        
        Args:
            tenant_id: Tenant ID (required)
            entity_type: Filter by entity type
            entity_id: Filter by specific entity
            user_id: Filter by user
            limit: Maximum records to return
        
        Returns:
            List of AuditLog entries, newest first
        """
        query = self.db.query(AuditLog).filter(
            AuditLog.tenant_id == tenant_id
        )
        
        if entity_type:
            query = query.filter(AuditLog.entity_type == entity_type)
        
        if entity_id:
            query = query.filter(AuditLog.entity_id == entity_id)
        
        if user_id:
            query = query.filter(AuditLog.user_id == user_id)
        
        return query.order_by(AuditLog.created_at.desc()).limit(limit).all()
    
    def get_campaign_history(self, campaign_id: str, limit: int = 20) -> List[AuditLog]:
        """Get audit history for a specific campaign."""
        return self.db.query(AuditLog).filter(
            AuditLog.entity_type == self.ENTITY_CAMPAIGN,
            AuditLog.entity_id == campaign_id
        ).order_by(AuditLog.created_at.desc()).limit(limit).all()
