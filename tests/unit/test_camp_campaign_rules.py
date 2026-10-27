"""Campaign P2 rules without a database: duplicate options, lead timing, persona gaps, weighted
rotation, clearing fields on update, the enrolment block reasons."""

from collections import Counter
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.campaigns import (
    REMOVED_BY_USER,
    StepMessage,
    check_send_window,
    copy_end_date,
    copy_start_date,
    duplicate_name,
    is_removed,
    lead_timing,
    persona_gaps,
)
from app.schemas.campaign_schema import CampaignDuplicateRequest, CampaignUpdate, EnrollmentCheckRequest
from app.services.enrollment_rules import REASONS, _Facts, check
from app.services.inbox_rotation import InboxRotation, inbox_weight, weighted_sequence
from app.services.sequence_stop import stops_sequence

T0 = datetime(2026, 11, 2, 9, 0)


# ── duplicate ────────────────────────────────────────────────────────────────────────────────────

def test_duplicate_name_defaults_and_truncates():
    assert duplicate_name("Spring") == "Copy of Spring"
    assert duplicate_name("Spring", "  Autumn ") == "Autumn"
    assert len(duplicate_name("x" * 300)) == 255


def test_copy_dates():
    today = date(2026, 10, 27)
    assert copy_start_date(None, date(2026, 10, 1), today) is None  # past source start dropped
    assert copy_start_date(None, date(2026, 11, 1), today) == date(2026, 11, 1)
    with pytest.raises(ValueError):
        copy_start_date(date(2026, 10, 1), None, today)
    assert copy_end_date(None, date(2026, 10, 20), None, today) is None
    assert copy_end_date(None, date(2026, 12, 1), date(2026, 11, 1), today) == date(2026, 12, 1)
    with pytest.raises(ValueError):
        copy_end_date(date(2026, 10, 30), None, date(2026, 11, 1), today)


def test_send_window_order():
    check_send_window(time(9), time(17))
    check_send_window(None, time(17))
    with pytest.raises(ValueError):
        check_send_window(time(17), time(9))


def test_duplicate_request_validation():
    with pytest.raises(ValidationError):
        CampaignDuplicateRequest(start_date=date(2026, 12, 2), end_date=date(2026, 12, 1))
    with pytest.raises(ValidationError):
        CampaignDuplicateRequest(send_window_start=time(10), send_window_end=time(9))
    assert CampaignDuplicateRequest().skip_source_prospects is True


def test_is_removed():
    assert is_removed("PAUSED", REMOVED_BY_USER)
    assert not is_removed("PAUSED", "Paused by automation")
    assert not is_removed("ACTIVE", REMOVED_BY_USER)


# ── lead timing ──────────────────────────────────────────────────────────────────────────────────

def _timing(msgs, status="ACTIVE", sending=True, **kw):
    return lead_timing(msgs, steps_total=kw.get("steps_total", 3), stopped_reason=kw.get("stopped_reason"),
                       ooo=kw.get("ooo", False), sending=sending,
                       step_stopped=lambda m: stops_sequence(m, status))


def test_lead_timing_basic():
    msgs = [
        StepMessage(1, "SENT", T0, T0, T0 + timedelta(minutes=1)),
        StepMessage(2, "SCHEDULED", T0 + timedelta(days=2)),
        StepMessage(3, "SCHEDULED", T0 + timedelta(days=5)),
    ]
    t = _timing(msgs)
    assert t.last_sent_at == T0 and t.delivered_at == T0 + timedelta(minutes=1)
    assert (t.steps_sent, t.steps_total) == (1, 3)
    assert t.next_email_at == T0 + timedelta(days=2)
    assert t.bounce_reason is None


def test_next_email_respects_per_step_stop_flags():
    msgs = [
        StepMessage(1, "SENT", T0, T0),
        StepMessage(2, "SCHEDULED", T0 + timedelta(days=2), stop_on_reply=True),
        StepMessage(3, "SCHEDULED", T0 + timedelta(days=5), stop_on_reply=False),
    ]
    assert _timing(msgs, status="REPLIED").next_email_at == T0 + timedelta(days=5)
    assert _timing(msgs, status="UNSUBSCRIBED").next_email_at is None


def test_next_email_none_when_not_sending_and_bounce_reason():
    msgs = [StepMessage(1, "BOUNCED", T0, T0, failure_reason="Permanent: General"),
            StepMessage(2, "PAUSED_BY_CAMPAIGN", T0 + timedelta(days=1))]
    t = _timing(msgs, sending=False, ooo=True, stopped_reason="x")
    assert t.next_email_at is None
    assert t.bounce_reason == "Permanent: General"
    assert t.ooo is True and t.stopped_reason == "x"
    assert set(t.as_dict()) == {"last_sent_at", "delivered_at", "bounce_reason", "ooo", "steps_sent",
                                "steps_total", "next_email_at", "stopped_reason"}


# ── persona gaps ─────────────────────────────────────────────────────────────────────────────────

def test_persona_gaps():
    by_step = {1: {"CFO", None}, 2: {"CFO"}, 3: {"CTO"}}
    gaps = persona_gaps({"CFO": 3, "CTO": 2, "OTHER": 1}, by_step)
    assert [(g.persona_type, g.prospect_count, list(g.missing_steps)) for g in gaps] == [
        ("CFO", 3, [3]), ("CTO", 2, [2]), ("OTHER", 1, [2, 3])]
    assert persona_gaps({"CFO": 1}, {1: {None}}) == []


# ── weighted rotation ────────────────────────────────────────────────────────────────────────────

def test_equal_weights_is_round_robin():
    assert weighted_sequence([5, 5, 5], 7) == [0, 1, 2, 0, 1, 2, 0]


def test_weights_proportional_and_smooth():
    seq = weighted_sequence([2000, 500], 50)
    counts = Counter(seq)
    assert counts[0] == 40 and counts[1] == 10
    for n in range(1, 51):  # every prefix within one contact of the ideal share
        assert abs(seq[:n].count(1) - n * 0.2) < 1


def test_zero_weights_share_equally_and_empty():
    assert weighted_sequence([0, 0], 4) == [0, 1, 0, 1]
    assert weighted_sequence([], 3) == []
    rotation = InboxRotation([])
    assert not rotation and rotation.next() is None
    with pytest.raises(ValueError):
        rotation.take()


def test_inbox_rotation_uses_effective_limit():
    a = SimpleNamespace(name="a", current_daily_limit=300)
    b = SimpleNamespace(name="b", current_daily_limit=100)
    rotation = InboxRotation([a, b])
    picks = [rotation.take().name for _ in range(8)]
    assert picks.count("a") == 6 and picks.count("b") == 2
    assert inbox_weight(SimpleNamespace(current_daily_limit=None)) == 0
    assert inbox_weight(SimpleNamespace(current_daily_limit=-5)) == 0


# ── clearing fields on update ────────────────────────────────────────────────────────────────────

def test_update_absent_vs_null():
    assert CampaignUpdate(campaign_name="X").changes() == {"campaign_name": "X"}
    cleared = CampaignUpdate(sender_title=None, cta_link=None, batch_size=None, daily_batch_size=None,
                             start_date=None, end_date=None).changes()
    assert cleared == dict.fromkeys(
        ["sender_title", "cta_link", "batch_size", "daily_batch_size", "start_date", "end_date"])
    # Fields that may not be empty ignore an explicit null
    assert CampaignUpdate(campaign_timezone=None, sending_mode=None, campaign_name=None).changes() == {}
    assert "inbox_ids" not in CampaignUpdate(inbox_ids=[], expected_version=2).changes()


def test_update_validation_kept():
    with pytest.raises(ValidationError):
        CampaignUpdate(batch_size=0)
    with pytest.raises(ValidationError):
        CampaignUpdate(start_date=date(2026, 12, 2), end_date=date(2026, 12, 1))
    with pytest.raises(ValidationError):
        CampaignUpdate(campaign_name="   ")


# ── enrolment rules ──────────────────────────────────────────────────────────────────────────────

def _p(pid, **kw):
    base = dict(prospect_id=pid, email=f"{pid}@acme.com", consent_status="OPT_IN", is_valid_email=True,
                full_name=pid)
    base.update(kw)
    return SimpleNamespace(**base)


def _facts(**kw):
    defaults = dict(unsubscribed=set(), enrolled=set(), removed={}, skip=set(), positive=set(),
                    elsewhere=set(), recent=set())
    defaults.update(kw)
    return _Facts(**defaults)


def test_block_reasons_in_order():
    f = _facts(unsubscribed={"a@acme.com"}, enrolled={"b", "r1", "r2"}, removed={"r1": False, "r2": True},
               skip={"s"}, positive={"pos", "s"}, elsewhere={"e", "pos"}, recent={"c", "e"})
    assert f.block_reason(_p("a", consent_status="OPT_OUT")) == "unsubscribed"
    assert f.block_reason(_p("x", consent_status="OPT_OUT")) == "opted_out"
    assert f.block_reason(_p("x", is_valid_email=False)) == "invalid_email"
    assert f.block_reason(_p("b")) == "already_enrolled"
    assert f.block_reason(_p("r1")) is None  # removed before any email: re-enrolable
    assert f.block_reason(_p("r2")) == "previously_removed"
    assert f.block_reason(_p("s")) == "in_source_campaign"
    assert f.block_reason(_p("pos")) == "replied_positive"
    assert f.block_reason(_p("e")) == "active_elsewhere"
    assert f.block_reason(_p("c")) == "recently_contacted"
    assert f.block_reason(_p("ok")) is None
    for code in ("previously_removed", "in_source_campaign", "replied_positive", "active_elsewhere"):
        assert REASONS[code]


def test_check_shape():
    out = check([_p("a")], [{"prospect_id": "b", "reason_code": "active_elsewhere"}])
    assert out["would_enroll_count"] == 1 and out["would_enroll"][0]["prospect_id"] == "a"
    assert out["rejected_summary"] == {"active_elsewhere": 1}


def test_enrollment_check_request_wizard_needs_list():
    with pytest.raises(ValidationError):
        EnrollmentCheckRequest(path="wizard")
    assert EnrollmentCheckRequest(list_ids=["l1"]).path == "campaign"
