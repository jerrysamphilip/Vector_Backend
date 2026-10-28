# app/services/automation_rule_service.py
"""
Automation rules (ADR 0009, chunk A7): the CRUD routes (``AutomationRulesService``) and the engine that
runs rules when a campaign event happens (``AutomationRuleService.process_event``).

Events: EMAIL_OPENED / EMAIL_CLICKED (SES tracking) and EMAIL_BOUNCED (a hard bounce), raised by
app/routers/ses_webhook_router.py. A rule acts on a contact at most once (``automation_rule_runs``,
unique per rule and contact): every action claims its run before it changes anything, so repeated or
simultaneous events never repeat it. SQL lives in app/repositories/automation.py.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from app.core.clock import utcnow
from app.core.database import SessionLocal
from app.core.errors import BadRequestError, NotFoundError
from app.domain.automation import ActionType, RuleTemplateError, TriggerType, action_template_id, target_step
from app.models.automation_rule import AutomationRule
from app.models.email_message import EmailMessage
from app.models.user import User
from app.repositories.automation import (
    AutomationAccess,
    AutomationRepository,
    AutomationUnitOfWork,
    Session,
    automation_uow,
    borrow,
)
from app.schemas.automation_schema import AutomationRuleCreate, AutomationRuleUpdate
from app.utils.campaign_prospect_status import set_prospect_status

logger = logging.getLogger(__name__)

# Automation mail that does not count as "already sent" for the once-per-template check
_DEAD_STATUSES = ("CANCELLED", "FAILED")


def template_in_tenant(db: Session, template_id: str, tenant_id: Optional[str]) -> bool:
    """True when the template belongs to a campaign of this workspace (F058)."""
    return borrow(db).rules_repo.template_in_tenant(template_id, tenant_id)


def claim_rule_run(db: Session, rule_id: str, prospect_id: str, tenant_id: Optional[str]) -> bool:
    """Record that this rule has acted on this contact; False when it already had (19b)."""
    return borrow(db).rules_repo.claim_run(rule_id, prospect_id, tenant_id)


# ── Routes ─────────────────────────────────────────────────────────

def check_action_template(repo: AutomationRepository, action_type: str, action_config: Optional[Dict[str, Any]],
                          tenant_id: str) -> None:
    """The template a rule sends must be one of the caller's workspace (F058): another workspace's
    template is 404, exactly like one that does not exist. SEND_EMAIL without a template is 400."""
    try:
        template_id = action_template_id(action_type, action_config)
    except RuleTemplateError as exc:
        raise BadRequestError(str(exc)) from exc
    if template_id is None:
        return
    if not isinstance(template_id, str) or not repo.template_in_tenant(template_id, tenant_id):
        raise NotFoundError("Template not found")


class AutomationRulesService:
    """The /automation-rules routes; each method is one unit of work."""

    def __init__(self, uow: AutomationUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> "AutomationRulesService":
        return cls(automation_uow(db))

    def create(self, user: User, data: AutomationRuleCreate) -> AutomationRule:
        with self.uow as uow:
            repo = uow.rules_repo
            if not repo.campaign(user.tenant_id, data.campaign_id):
                raise NotFoundError("Campaign not found")
            check_action_template(repo, data.action_type.value, data.action_config, user.tenant_id)
            rule = repo.add(AutomationRule(**data.model_dump()))
            uow.commit()
            repo.refresh(rule)
            return rule

    def list(self, user: User, campaign_id: Optional[str]) -> List[AutomationRule]:
        with self.uow as uow:
            return uow.rules_repo.rules(user.tenant_id, campaign_id)

    def _rule(self, repo: AutomationRepository, user: User, rule_id: str) -> AutomationRule:
        rule = repo.rule(user.tenant_id, rule_id)
        if not rule:
            raise NotFoundError("Rule not found")
        return rule

    def update(self, user: User, rule_id: str, data: AutomationRuleUpdate) -> AutomationRule:
        with self.uow as uow:
            repo = uow.rules_repo
            rule = self._rule(repo, user, rule_id)
            changes = data.model_dump(exclude_unset=True)
            if "action_config" in changes:
                check_action_template(repo, rule.action_type, changes["action_config"], user.tenant_id)
            for name, value in changes.items():
                setattr(rule, name, value)
            uow.commit()
            repo.refresh(rule)
            return rule

    def delete(self, user: User, rule_id: str) -> None:
        with self.uow as uow:
            repo = uow.rules_repo
            repo.delete(self._rule(repo, user, rule_id))
            uow.commit()


# ── Engine ─────────────────────────────────────────────────────────

class AutomationRuleService:
    """Evaluates triggers and executes actions for automation rules."""

    def process_event(self, event_type: TriggerType, campaign_id: str, prospect_id: str,
                      metadata: Optional[Dict[str, Any]] = None, db: Optional[Session] = None) -> int:
        """Run the campaign's active rules for this trigger on the contact (e.g. EMAIL_BOUNCED for
        contact X in campaign Y). Commits; never raises (an automation must not break the event that
        raised it). Returns the number of rules that acted."""
        own = db is None
        session = SessionLocal() if db is None else db
        acted = 0
        try:
            ctx = borrow(session)
            rules = ctx.rules_repo.active_rules(campaign_id, event_type.value)
            if rules:
                logger.info("[Automation] %s rule(s) for %s in campaign %s", len(rules), event_type.value, campaign_id)
            for rule in rules:
                acted += 1 if self._evaluate_and_execute(rule, prospect_id, metadata or {}, ctx) else 0
            session.commit()
        except Exception as e:
            logger.exception("[Automation] Error processing event %s: %s", event_type, e)
            session.rollback()
        finally:
            if own:
                session.close()
        return acted

    def _actions(self) -> Dict[str, Callable[[AutomationRule, str, AutomationAccess], bool]]:
        return {
            ActionType.SEND_EMAIL.value: lambda r, p, c: self._send_email(r, p, c),
            ActionType.CHANGE_CAMPAIGN_STEP.value: lambda r, p, c: self._change_step(r, p, c),
            ActionType.PAUSE_PROSPECT.value: lambda r, p, c: self._pause_prospect(r, p, c),
        }

    def _evaluate_and_execute(self, rule: AutomationRule, prospect_id: str, metadata: Dict[str, Any],
                              ctx: AutomationAccess) -> bool:
        """Every rule whose trigger fired acts (trigger_config conditions are not evaluated yet)."""
        action = self._actions().get(rule.action_type)
        if action is None:
            logger.warning("[Automation] Unknown action type: %s", rule.action_type)
            return False
        try:
            with ctx.savepoint():  # a failing action undoes only itself
                return action(rule, prospect_id, ctx)
        except Exception as e:
            logger.error("[Automation] Failed to execute rule %s: %s", rule.name, e)
            return False

    def _claim(self, ctx: AutomationAccess, rule: AutomationRule, prospect_id: str, tenant_id: Optional[str]) -> bool:
        if ctx.rules_repo.claim_run(rule.rule_id, prospect_id, tenant_id):
            return True
        logger.info("[Automation] Rule %s already ran for prospect %s; not again", rule.name, prospect_id)
        return False

    # Kept for callers of the first version: (rule, prospect_id, db)
    def _action_send_email(self, rule: AutomationRule, prospect_id: str, db: Session) -> bool:
        return self._send_email(rule, prospect_id, borrow(db))

    def _send_email(self, rule: AutomationRule, prospect_id: str, ctx: AutomationAccess) -> bool:
        """Queue the rule's template to the contact now."""
        repo = ctx.rules_repo
        template_id = (rule.action_config or {}).get("template_id")
        if not template_id:
            logger.error("[Automation] Rule %s missing template_id", rule.name)
            return False
        # The rule, its template and the contact must all be in the rule's workspace: a rule saved
        # before the API checked templates (F058) must not mail another workspace's content.
        tenant_id = repo.campaign_tenant(rule.campaign_id)
        if not repo.template_in_tenant(template_id, tenant_id):
            logger.warning("[Automation] Rule %s: template %s is not in the rule's workspace; not sending",
                           rule.rule_id, template_id)
            return False
        prospect = repo.prospect(tenant_id, prospect_id)
        if not prospect:
            logger.error("[Automation] Prospect %s not found", prospect_id)
            return False
        # The rule's mail is ad-hoc, not a sequence step: no sequence_id, so the scheduler's step
        # duplicate guard does not cancel it against the contact's real step-1 email. One per contact.
        if repo.rule_mail_exists(rule.campaign_id, prospect_id, template_id, _DEAD_STATUSES):
            logger.info("[Automation] Rule %s: template %s already queued/sent to %s", rule.name, template_id,
                        prospect_id)
            return False
        if not self._claim(ctx, rule, prospect_id, tenant_id):
            return False
        repo.add(EmailMessage(campaign_id=rule.campaign_id, prospect_id=prospect_id, template_id=template_id,
                              status="QUEUED", scheduled_at=utcnow(), retry_count=0, to_email=prospect.email))
        repo.flush()  # visible to the de-duplication check if the rule fires again in this session
        logger.info("[Automation] Scheduled email (template %s) from rule %s", template_id, rule.name)
        return True

    def _change_step(self, rule: AutomationRule, prospect_id: str, ctx: AutomationAccess) -> bool:
        """Move the contact to another step of the campaign (e.g. skip to step 5)."""
        step = target_step(rule.action_config)
        cp = ctx.rules_repo.enrollment(rule.campaign_id, prospect_id) if step else None
        if cp is None or not self._claim(ctx, rule, prospect_id, ctx.rules_repo.campaign_tenant(rule.campaign_id)):
            return False
        old_step, cp.current_step = cp.current_step, step
        logger.info("[Automation] Moved prospect %s from step %s to %s", prospect_id, old_step, step)
        return True

    def _pause_prospect(self, rule: AutomationRule, prospect_id: str, ctx: AutomationAccess) -> bool:
        """Stop the contact's sequence in this campaign."""
        cp = ctx.rules_repo.enrollment(rule.campaign_id, prospect_id)
        if cp is None or not self._claim(ctx, rule, prospect_id, ctx.rules_repo.campaign_tenant(rule.campaign_id)):
            return False
        set_prospect_status(cp, "PAUSED", stopped_reason=f"Automation Rule: {rule.name}")
        logger.info("[Automation] Paused prospect %s", prospect_id)
        return True


# Singleton
automation_service = AutomationRuleService()
