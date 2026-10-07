
# app/services/automation_rule_service.py
"""
Service for evaluating and executing automation rules.
"""

import logging
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from app.core.database import SessionLocal
from app.models.automation_rule import AutomationRule, TriggerType, ActionType
from app.models import (
    EmailEvent,
    Prospect,
    CampaignProspect,
    EmailMessage,
    EmailTemplate
)
from app.utils.campaign_prospect_status import set_prospect_status

logger = logging.getLogger(__name__)

class AutomationRuleService:
    """
    Evaluates triggers and executes actions for automation rules.
    """

    def process_event(self, event_type: TriggerType, campaign_id: str, prospect_id: str, metadata: Dict[str, Any] = None, db: Session = None):
        """
        Evaluate rules for a specific event trigger.
        Example: EMAIL_OPENED for prospect X in campaign Y.
        """
        close_db = False
        if not db:
            db = SessionLocal()
            close_db = True
            
        try:
            # Find active rules for this campaign and trigger
            rules = db.query(AutomationRule).filter(
                AutomationRule.campaign_id == campaign_id,
                AutomationRule.trigger_type == event_type.value,
                AutomationRule.is_active == True
            ).all()
            
            if not rules:
                return
                
            logger.info(f"[Automation] Found {len(rules)} rules for {event_type} in campaign {campaign_id}")
            
            for rule in rules:
                self._evaluate_and_execute(rule, prospect_id, metadata, db)
                
            db.commit()
            
        except Exception as e:
            import traceback
            with open("c:/Users/NTS-PranavParvekar/Desktop/SALES_PRO/error.log", "w") as f:
                traceback.print_exc(file=f)
            logger.error(f"[Automation] Error processing event {event_type}: {e}")
            db.rollback()
        finally:
            if close_db:
                db.close()

    def _evaluate_and_execute(self, rule: AutomationRule, prospect_id: str, metadata: Dict, db: Session):
        """Check conditions and execute action."""
        # 1. Check Conditions (if any in trigger_config)
        # For now, we assume if the event fired, the trigger is met.
        # Future: Check metadata (e.g. "clicked specific link")
        
        # 2. Execute Action
        try:
            if rule.action_type == ActionType.SEND_EMAIL.value:
                self._action_send_email(rule, prospect_id, db)
            elif rule.action_type == ActionType.CHANGE_CAMPAIGN_STEP.value:
                self._action_change_step(rule, prospect_id, db)
            elif rule.action_type == ActionType.PAUSE_PROSPECT.value:
                self._action_pause_prospect(rule, prospect_id, db)
            else:
                logger.warning(f"[Automation] Unknown action type: {rule.action_type}")
                
        except Exception as e:
            logger.error(f"[Automation] Failed to execute rule {rule.name}: {e}")

    def _action_send_email(self, rule: AutomationRule, prospect_id: str, db: Session):
        """Action: Schedule an immediate email."""
        template_id = rule.action_config.get("template_id")
        if not template_id:
            logger.error(f"[Automation] Rule {rule.name} missing template_id")
            return

        # Fetch prospect for email address (Required by EmailMessage)
        prospect = db.query(Prospect).filter(Prospect.prospect_id == prospect_id).first()
        if not prospect:
            logger.error(f"[Automation] Prospect {prospect_id} not found")
            return

        # Fetch invalid sequence ID (Required by EmailMessage)
        # We rely on the campaign having at least one sequence or use a placeholder if appropriate.
        # For now, grab the first sequence of the campaign.
        from app.models.email_sequence import EmailSequence
        sequence = db.query(EmailSequence).filter(
            EmailSequence.campaign_id == rule.campaign_id
        ).first()

        if not sequence:
            logger.error(f"[Automation] Campaign {rule.campaign_id} has no sequence for context")
            # Fallback or return? Return for now to avoid constraint error.
            return

        # Create message scheduled for NOW
        msg = EmailMessage(
            campaign_id=rule.campaign_id,
            prospect_id=prospect_id,
            sequence_id=sequence.sequence_id,
            template_id=template_id,
            status="QUEUED",
            scheduled_at=datetime.utcnow(),
            retry_count=0,
            to_email=prospect.email  # Required field
        )
        db.add(msg)
        logger.info(f"[Automation] Scheduled email (Template {template_id}) from Rule {rule.name}")

    def _action_change_step(self, rule: AutomationRule, prospect_id: str, db: Session):
        """Action: Move prospect to a different campaign step (e.g., skip to step 5)."""
        target_step = rule.action_config.get("step_id") # e.g. 5
        if not target_step:
            return

        cp = db.query(CampaignProspect).filter(
            CampaignProspect.campaign_id == rule.campaign_id,
            CampaignProspect.prospect_id == prospect_id
        ).first()
        
        if cp:
            old_step = cp.current_step
            cp.current_step = target_step
            logger.info(f"[Automation] Moved Prospect {prospect_id} from Step {old_step} to {target_step}")

    def _action_pause_prospect(self, rule: AutomationRule, prospect_id: str, db: Session):
        """Action: Mark prospect as REPLIED or similar to stop sequence."""
        cp = db.query(CampaignProspect).filter(
            CampaignProspect.campaign_id == rule.campaign_id,
            CampaignProspect.prospect_id == prospect_id
        ).first()
        
        if cp:
            set_prospect_status(cp, "PAUSED", stopped_reason=f"Automation Rule: {rule.name}")
            logger.info(f"[Automation] Paused Prospect {prospect_id}")

# Singleton
automation_service = AutomationRuleService()
