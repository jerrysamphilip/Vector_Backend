"""
Automation rules aggregate: trigger and action vocabulary and pure rules (ADR 0009, chunk A7).

A rule belongs to a campaign: when its trigger happens to a contact of the campaign (an e-mail of the
campaign is opened, clicked, or hard-bounces), its action runs for that contact, at most once per
(rule, contact) (``automation_rule_runs``). Pure: no I/O.
"""

from __future__ import annotations

import enum
from typing import Any, Mapping, Optional


class TriggerType(str, enum.Enum):
    EMAIL_OPENED = "EMAIL_OPENED"
    EMAIL_CLICKED = "EMAIL_CLICKED"
    EMAIL_BOUNCED = "EMAIL_BOUNCED"  # a hard (permanent) bounce reported by SES
    NO_REPLY = "NO_REPLY"
    TIME_DELAY = "TIME_DELAY"


class ActionType(str, enum.Enum):
    SEND_EMAIL = "SEND_EMAIL"
    CHANGE_CAMPAIGN_STEP = "CHANGE_CAMPAIGN_STEP"
    PAUSE_PROSPECT = "PAUSE_PROSPECT"
    MARK_INTERESTED = "MARK_INTERESTED"


class RuleTemplateError(ValueError):
    """The rule's action configuration names no usable template."""


def action_template_id(action_type: str, action_config: Optional[Mapping[str, Any]]) -> Any:
    """The template id a rule's action names (not checked for type or workspace), or None. SEND_EMAIL
    without one is an error; other actions may carry none."""
    template_id = (action_config or {}).get("template_id")
    if not template_id:
        if action_type == ActionType.SEND_EMAIL.value:
            raise RuleTemplateError("A SEND_EMAIL rule needs action_config.template_id")
        return None
    return template_id


def target_step(action_config: Optional[Mapping[str, Any]]) -> Optional[int]:
    """CHANGE_CAMPAIGN_STEP: the step to move the contact to (None = nothing to do)."""
    step = (action_config or {}).get("step_id")
    try:
        return int(step) if step not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return None
