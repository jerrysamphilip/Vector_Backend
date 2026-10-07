# app/services/sequence_service.py
"""
Email Sequence service for managing campaign steps.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional
import uuid

from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.schemas.sequence_schema import SequenceStepCreate, SequenceStepUpdate, SequenceStepResponse


class SequenceService:
    """Service for email sequence management."""
    
    MAX_STEPS = 7
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_step(self, sequence_id: str) -> Optional[EmailSequence]:
        """Get a specific sequence step by ID."""
        return self.db.query(EmailSequence).filter(
            EmailSequence.sequence_id == sequence_id
        ).first()
    
    def get_campaign_sequences(self, campaign_id: str) -> List[EmailSequence]:
        """Get all sequence steps for a campaign, ordered by step number."""
        return self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id
        ).order_by(EmailSequence.step_number).all()
    
    def get_step_count(self, campaign_id: str) -> int:
        """Get the number of steps in a campaign."""
        return self.db.query(func.count(EmailSequence.sequence_id)).filter(
            EmailSequence.campaign_id == campaign_id
        ).scalar() or 0
    
    def add_step(self, campaign_id: str, data: SequenceStepCreate) -> EmailSequence:
        """
        Add a sequence step to a campaign.
        
        Args:
            campaign_id: Campaign ID
            data: Step data
        
        Returns:
            Created EmailSequence
        
        Raises:
            ValueError: If max steps exceeded or step number already exists
        """
        # Check max steps
        current_count = self.get_step_count(campaign_id)
        if current_count >= self.MAX_STEPS:
            raise ValueError(f"Maximum {self.MAX_STEPS} steps allowed per campaign")
        
        # Check if step number already exists
        existing = self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.step_number == data.step_number
        ).first()
        
        if existing:
            raise ValueError(f"Step {data.step_number} already exists in this campaign")
        
        step = EmailSequence(
            sequence_id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            step_number=data.step_number,
            wait_days=data.wait_days,
            stop_on_reply=data.stop_on_reply,
            stop_on_bounce=data.stop_on_bounce,
            send_start_hour=data.send_start_hour,
            send_end_hour=data.send_end_hour,
        )
        
        self.db.add(step)
        self.db.flush()
        
        return step
    
    def update_step(self, sequence_id: str, data: SequenceStepUpdate) -> Optional[EmailSequence]:
        """
        Update a sequence step.
        
        Args:
            sequence_id: Sequence step ID
            data: Update data
        
        Returns:
            Updated EmailSequence or None if not found
        """
        step = self.get_step(sequence_id)
        if not step:
            return None
        
        if data.wait_days is not None:
            step.wait_days = data.wait_days
        if data.stop_on_reply is not None:
            step.stop_on_reply = data.stop_on_reply
        if data.stop_on_bounce is not None:
            step.stop_on_bounce = data.stop_on_bounce
        if data.send_start_hour is not None:
            step.send_start_hour = data.send_start_hour
        if data.send_end_hour is not None:
            step.send_end_hour = data.send_end_hour
        
        self.db.flush()
        return step
    
    def delete_step(self, sequence_id: str) -> bool:
        """
        Delete a sequence step.
        
        Args:
            sequence_id: Sequence step ID
        
        Returns:
            True if deleted, False if not found
        """
        step = self.get_step(sequence_id)
        if not step:
            return False
        
        self.db.delete(step)
        self.db.flush()
        return True
    
    def get_total_wait_days(self, campaign_id: str) -> int:
        """Get total wait days for all steps in a campaign."""
        result = self.db.query(func.sum(EmailSequence.wait_days)).filter(
            EmailSequence.campaign_id == campaign_id
        ).scalar()
        return result or 0
    
    def to_response(self, step: EmailSequence) -> SequenceStepResponse:
        """Convert EmailSequence to response schema."""
        # Get template info if attached
        template_subject = None
        template_id = None
        
        template = self.db.query(EmailTemplate).filter(
            EmailTemplate.sequence_id == step.sequence_id
        ).first()
        
        if template:
            template_id = template.template_id
            template_subject = template.subject[:50] + "..." if len(template.subject) > 50 else template.subject
        
        return SequenceStepResponse(
            sequence_id=step.sequence_id,
            campaign_id=step.campaign_id,
            step_number=step.step_number,
            wait_days=step.wait_days,
            stop_on_reply=step.stop_on_reply,
            stop_on_bounce=step.stop_on_bounce,
            send_start_hour=step.send_start_hour,
            send_end_hour=step.send_end_hour,
            template_id=template_id,
            template_subject=template_subject,
        )
