"""A7 automation rules: domain rules, the CRUD service and the engine's actions with a fake repository."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, NotFoundError
from app.domain.automation import ActionType, RuleTemplateError, TriggerType, action_template_id, target_step
from app.models.automation_rule import TriggerType as ModelTriggerType
from app.schemas.automation_schema import AutomationRuleCreate, AutomationRuleUpdate
from app.services.automation_rule_service import AutomationRuleService, AutomationRulesService

ME = SimpleNamespace(user_id="me", tenant_id="t1")


def test_trigger_vocabulary_has_the_bounce_trigger():
    assert TriggerType.EMAIL_BOUNCED.value == "EMAIL_BOUNCED" and ModelTriggerType is TriggerType


def test_action_template_and_step_rules():
    assert action_template_id("PAUSE_PROSPECT", None) is None
    assert action_template_id("SEND_EMAIL", {"template_id": "t"}) == "t"
    with pytest.raises(RuleTemplateError):
        action_template_id("SEND_EMAIL", {})
    assert target_step({"step_id": "5"}) == 5 and target_step({"step_id": 0}) is None
    assert target_step({"step_id": "x"}) is None and target_step(None) is None


class FakeRepo:
    def __init__(self):
        self.campaigns = {"c1": "t1"}
        self.templates = {("tpl1", "t1")}
        self.rules_by_id = {}
        self.added = []
        self.deleted = []
        self.runs = set()
        self.mail = set()
        self.enrolled = {}
        self.prospects = {"p1": SimpleNamespace(prospect_id="p1", email="p1@x.example")}

    def campaign(self, tenant_id, campaign_id):
        return SimpleNamespace(campaign_id=campaign_id) if self.campaigns.get(campaign_id) == tenant_id else None

    def rules(self, tenant_id, campaign_id):
        return list(self.rules_by_id.values())

    def rule(self, tenant_id, rule_id):
        return self.rules_by_id.get(rule_id)

    def template_in_tenant(self, template_id, tenant_id):
        return (template_id, tenant_id) in self.templates

    def add(self, entity):
        self.added.append(entity)
        return entity

    def delete(self, entity):
        self.deleted.append(entity)

    def flush(self):
        pass

    def refresh(self, entity):
        pass

    def campaign_tenant(self, campaign_id):
        return self.campaigns.get(campaign_id)

    def prospect(self, tenant_id, prospect_id):
        return self.prospects.get(prospect_id)

    def rule_mail_exists(self, campaign_id, prospect_id, template_id, ignore_statuses):
        return (campaign_id, prospect_id, template_id) in self.mail

    def claim_run(self, rule_id, prospect_id, tenant_id):
        if (rule_id, prospect_id) in self.runs:
            return False
        self.runs.add((rule_id, prospect_id))
        return True

    def enrollment(self, campaign_id, prospect_id):
        return self.enrolled.get((campaign_id, prospect_id))


class FakeUow:
    def __init__(self):
        self.rules_repo = FakeRepo()
        self.session = None
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def savepoint(self):
        return nullcontext()


def _create(**over):
    body = {"campaign_id": "c1", "name": "R", "trigger_type": "EMAIL_BOUNCED", "action_type": "SEND_EMAIL",
            "action_config": {"template_id": "tpl1"}}
    body.update(over)
    return AutomationRuleCreate(**body)


def test_create_checks_campaign_and_template():
    uow = FakeUow()
    svc = AutomationRulesService(uow)
    rule = svc.create(ME, _create())
    assert rule.trigger_type == "EMAIL_BOUNCED" and uow.commits == 1
    with pytest.raises(NotFoundError, match="Campaign"):
        svc.create(ME, _create(campaign_id="other"))
    with pytest.raises(NotFoundError, match="Template"):
        svc.create(ME, _create(action_config={"template_id": "foreign"}))
    with pytest.raises(NotFoundError, match="Template"):
        svc.create(ME, _create(action_config={"template_id": 7}))
    with pytest.raises(BadRequestError):
        svc.create(ME, _create(action_config={}))
    svc.create(ME, _create(action_type="PAUSE_PROSPECT", action_config={}))


def test_update_and_delete():
    uow = FakeUow()
    svc = AutomationRulesService(uow)
    rule = SimpleNamespace(rule_id="r1", action_type="SEND_EMAIL", action_config={"template_id": "tpl1"}, description=None)
    uow.rules_repo.rules_by_id["r1"] = rule
    with pytest.raises(NotFoundError):
        svc.update(ME, "r1", AutomationRuleUpdate(action_config={"template_id": "foreign"}))
    assert rule.action_config == {"template_id": "tpl1"}
    assert svc.update(ME, "r1", AutomationRuleUpdate(description="kept")).description == "kept"
    svc.delete(ME, "r1")
    assert uow.rules_repo.deleted == [rule]
    with pytest.raises(NotFoundError):
        svc.delete(ME, "missing")


def _rule(action, config=None, rule_id="r1"):
    return SimpleNamespace(rule_id=rule_id, name="Rule", campaign_id="c1", action_type=action.value,
                           action_config=config or {})


def test_send_email_once_per_rule_and_contact():
    ctx = FakeUow()
    engine = AutomationRuleService()
    rule = _rule(ActionType.SEND_EMAIL, {"template_id": "tpl1"})
    assert engine._evaluate_and_execute(rule, "p1", {}, ctx) is True
    assert engine._evaluate_and_execute(rule, "p1", {}, ctx) is False  # run already claimed
    queued = [m for m in ctx.rules_repo.added if type(m).__name__ == "EmailMessage"]
    assert len(queued) == 1 and queued[0].to_email == "p1@x.example" and queued[0].status == "QUEUED"


def test_send_email_refuses_foreign_template_and_unknown_contact():
    ctx = FakeUow()
    engine = AutomationRuleService()
    assert engine._evaluate_and_execute(_rule(ActionType.SEND_EMAIL, {"template_id": "foreign"}), "p1", {}, ctx) is False
    assert engine._evaluate_and_execute(_rule(ActionType.SEND_EMAIL, {"template_id": "tpl1"}), "ghost", {}, ctx) is False
    assert ctx.rules_repo.runs == set()


def test_pause_and_change_step_claim_only_when_they_act():
    ctx = FakeUow()
    engine = AutomationRuleService()
    pause = _rule(ActionType.PAUSE_PROSPECT)
    assert engine._evaluate_and_execute(pause, "p1", {}, ctx) is False  # not enrolled: nothing claimed
    assert ctx.rules_repo.runs == set()
    cp = SimpleNamespace(status="ACTIVE", current_step=1, stopped_reason=None)
    ctx.rules_repo.enrolled[("c1", "p1")] = cp
    assert engine._evaluate_and_execute(pause, "p1", {}, ctx) is True and cp.status == "PAUSED"
    assert engine._evaluate_and_execute(pause, "p1", {}, ctx) is False
    step = _rule(ActionType.CHANGE_CAMPAIGN_STEP, {"step_id": 3}, rule_id="r2")
    assert engine._evaluate_and_execute(step, "p1", {}, ctx) is True and cp.current_step == 3


def test_unknown_or_failing_actions_do_not_raise():
    ctx = FakeUow()
    engine = AutomationRuleService()
    assert engine._evaluate_and_execute(_rule(ActionType.MARK_INTERESTED), "p1", {}, ctx) is False
    ctx.rules_repo.enrollment = lambda *a: (_ for _ in ()).throw(RuntimeError("db down"))
    assert engine._evaluate_and_execute(_rule(ActionType.PAUSE_PROSPECT), "p1", {}, ctx) is False
