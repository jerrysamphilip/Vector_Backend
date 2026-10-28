"""A8: pure sending rules (app/domain/sending.py) and warm-up scoring (app/domain/sending_warmup.py). No fakes."""
import random
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain import sending as rules
from app.domain import sending_warmup as warm

NOW = datetime(2026, 10, 27, 12, 0, 0)


# ── Pass statistics and the worker's next wake-up ──

@pytest.mark.parametrize("stats, expected", [
    ({"processed": 3}, 2.0),  # work was done: straight back
    ({"processed": 1, "paced": 2, "recheck_in": 25}, 2.0),
    ({"processed": 0, "paced": 1, "recheck_in": 10}, 10.0),  # wake when the soonest inbox is ready
    ({"processed": 0, "paced": 1, "recheck_in": 0.0}, 2.0),  # ready now (another process sent): soon
    ({"processed": 0, "paced": 1, "recheck_in": 0.5}, 2.0),  # never busier than every 2 s
    ({"processed": 0, "paced": 1, "recheck_in": 30}, None),  # far away: the normal interval
    ({"processed": 0, "paced": 0}, None),
    ({}, None),
])
def test_a8_next_run_in(stats, expected):
    assert rules.next_run_in(stats) == expected


def test_a8_pass_stats_add_and_dict():
    stats = rules.PassStats()
    stats.add({"processed": 2, "sent": 1, "failed": 1})
    stats.add({"paced": 1, "skipped": 0})
    assert stats.as_dict() == {"processed": 2, "sent": 1, "failed": 1, "suppressed": 0, "skipped": 0, "paced": 1,
                               "next_run_in": 2.0}
    stats.recheck_in = 7.5
    assert stats.as_dict()["recheck_in"] == 7.5


def test_a8_tally_outcome_counts_other_outcomes_as_skipped():
    counts = {"sent": 0, "failed": 0, "suppressed": 0, "skipped": 0}
    for outcome in (rules.SENT, rules.FAILED, rules.SUPPRESSED, rules.SKIPPED, rules.DEFERRED):
        rules.tally_outcome(counts, outcome)
    assert counts == {"sent": 1, "failed": 1, "suppressed": 1, "skipped": 2}


def test_a8_soonest_ready():
    assert rules.soonest_ready([1010.0, 1004.0, 990.0], 1000.0) == 4.0
    assert rules.soonest_ready([990.0], 1000.0) == 0.0
    assert rules.soonest_ready([], 1000.0) == 0.0


# ── Fair lanes ──

def test_a8_lane_cap_follows_the_inbox_pace():
    assert rules.lane_cap(60, 120) == 2
    assert rules.lane_cap(45, 120) == 3  # ceil
    assert rules.lane_cap(600, 120) == 1  # always at least one
    assert rules.lane_cap(0, 120) == rules.DEFAULT_LANE_CAP
    assert rules.lane_cap(None, 120) == rules.DEFAULT_LANE_CAP


def test_a8_lane_queues_keep_order_and_trim_per_lane():
    due = [rules.DueRow("a1", "inbox-a", "inbox-a"), rules.DueRow("b1", None, "camp-b"),
           rules.DueRow("a2", "inbox-a", "inbox-a"), rules.DueRow("a3", "inbox-a", "inbox-a"),
           rules.DueRow("b2", None, "camp-b")]
    queues = rules.lane_queues(due, {"inbox-a": 60}, 120)
    assert queues == {"inbox-a": ["a1", "a2"], "camp-b": ["b1", "b2"]}


# ── Claim ──

def test_a8_send_key():
    assert rules.send_key("c", "s", " Bob@X.example.com ") == "c:s:bob@x.example.com"
    assert rules.send_key("c", None, "a@x.example.com") is None
    assert rules.send_key(None, "s", "a@x.example.com") is None
    assert len(rules.send_key("c" * 200, "s" * 200, "a@x.example.com")) == rules.SEND_KEY_MAX


def test_a8_duplicate_reason_and_claim_result():
    assert rules.duplicate_reason("m9") == "Duplicate: this step was already sent to this address (message m9)"
    assert rules.duplicate_reason(None) == "Duplicate: this step was already sent to this address"
    assert rules.ClaimResult(claimed=False, duplicate=True).duplicate_of is None
    assert issubclass(rules.SendKeyConflict, ValueError)


# ── Pacing ──

def test_a8_pacing_state_waits_on_either_clock():
    state = rules.PacingState(delay=60, last_sent_at=NOW - timedelta(seconds=20), ready_at_mono=None)
    assert state.wait(15, now=NOW, now_mono=100.0) == 25  # shortest allowed gap 45 - 20
    state = rules.PacingState(delay=60, last_sent_at=None, ready_at_mono=130.0)
    assert state.wait(15, now=NOW, now_mono=100.0) == 30
    assert rules.PacingState(delay=0, last_sent_at=NOW, ready_at_mono=None).wait(15, now=NOW, now_mono=1.0) == 0


def test_a8_next_gap_is_jittered_within_bounds():
    rng = random.Random(3)
    assert all(45 <= rules.next_gap(60, 3, 15, rng) <= 75 for _ in range(100))
    assert rules.next_gap(None, 0, 0) == 0


def test_a8_pace_shortfall():
    assert rules.pace_shortfall(600, 200) == 144  # 86400 / 600
    assert rules.pace_shortfall(60, 200) is None
    assert rules.pace_shortfall(0, 200) is None and rules.pace_shortfall(None, 5) is None


# ── The sender's answer ──

def test_a8_send_attempt_from_result():
    ok = rules.SendAttempt.from_result({"success": True, "ses_message_id": "ses-1", "internet_message_id": "<m@x>"})
    assert ok.success and ok.provider_message_id == "<m@x>" and not ok.blocked
    assert rules.SendAttempt.from_result({"success": True, "ses_message_id": "ses-1"}).provider_message_id == "ses-1"
    blocked = rules.SendAttempt.from_result({"success": False, "status": "BLOCKED_SPAM", "error": "lint"})
    assert blocked.blocked and blocked.error == "lint"
    assert not rules.SendAttempt.from_result({"success": False, "status": None}).blocked


# ── Message transitions ──

def _msg(**kw):
    base = dict(message_id="m1", status="SENDING", send_key="k", scheduled_at=None, next_retry_at=None,
                failure_reason=None, final_status=None, final_status_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_a8_requeue_frees_the_claim():
    msg = _msg()
    rules.requeue(msg, at=NOW, retry_at=NOW + timedelta(minutes=2))
    assert msg.status == "QUEUED" and msg.send_key is None and msg.scheduled_at == NOW
    assert msg.next_retry_at == NOW + timedelta(minutes=2)
    msg = _msg(scheduled_at=NOW)
    rules.requeue(msg)
    assert msg.scheduled_at == NOW and msg.next_retry_at is None


def test_a8_fail_with_and_without_final_status():
    msg = _msg()
    rules.fail(msg, "boom", NOW)
    assert (msg.status, msg.failure_reason, msg.final_status, msg.final_status_at) == ("FAILED", "boom", "FAILED", NOW)
    msg = _msg()
    rules.fail(msg, "Prospect not found", NOW, final=False)
    assert msg.status == "FAILED" and msg.final_status is None


def test_a8_retry_budget_and_backoff():
    assert rules.retry_delay(1) == timedelta(minutes=2) and rules.retry_delay(3) == timedelta(minutes=8)
    assert rules.retries_exhausted(3, None) and not rules.retries_exhausted(2, None)
    assert rules.retries_exhausted(1, 1) and not rules.retries_exhausted(4, 5)


@pytest.mark.parametrize("fields, events, accepted", [
    ({"final_status": "BOUNCED"}, set(), True),
    ({"provider_message_id": "<p>"}, set(), True),
    ({}, {"m"}, True),
    ({"final_status": "FAILED"}, set(), False),
    ({}, set(), False),
])
def test_a8_provider_accepted(fields, events, accepted):
    msg = SimpleNamespace(**{**dict(message_id="m", ses_message_id=None, provider_message_id=None,
                                    delivered_at=None, final_status=None), **fields})
    assert rules.provider_accepted(msg, events) is accepted


# ── Warm-up scoring ──

def _metric(sent=10, opened=0, replied=0, saved=0, bounced=0, complaints=0):
    return SimpleNamespace(actual_sends=sent, open_count=opened, reply_count=replied, saved_from_spam_count=saved,
                           bounce_count=bounced, complaint_count=complaints)


def _old_formula(metrics, warmup_day, issue, external=None):
    """The pre-A8 WarmupService._score_reputation arithmetic, kept here as the golden reference."""
    total_sent = sum(max(m.actual_sends, 0) for m in metrics)
    denom = max(total_sent, 1)
    open_rate = (sum(m.open_count or 0 for m in metrics) / denom) * 100
    reply_rate = (sum(m.reply_count or 0 for m in metrics) / denom) * 100
    score = 58.0
    score += min(warmup_day or 0, 20) * 1.6
    score += min(open_rate, 95) * 0.18
    score += min(reply_rate, 40) * 0.25
    score += min(sum(m.saved_from_spam_count or 0 for m in metrics), 5) * 1.2
    score -= sum(m.bounce_count or 0 for m in metrics) * 18
    score -= sum(m.complaint_count or 0 for m in metrics) * 22
    if issue in {"POOL_TOO_SMALL", "SEND_FAILED"}:
        score -= 10
    elif issue in {"SYNC_STALE", "IMAP_NOT_CONFIGURED", "SMTP_NOT_CONFIGURED"}:
        score -= 4
    if external:
        if external.complaint_rate:
            score -= float(external.complaint_rate) * 200
        if external.bounce_rate:
            score -= float(external.bounce_rate) * 120
        if external.reputation_score:
            score = (score * 0.7) + (float(external.reputation_score) * 0.3)
    return max(25.0, min(round(score, 1), 100.0))


EXTERNAL = SimpleNamespace(complaint_rate=0.001, bounce_rate=0.02, reputation_score=88)


@pytest.mark.parametrize("metrics, day, issue, external", [
    ([_metric(10, 6, 2, 1)], 5, None, None),
    ([_metric(10, 9, 4, 3), _metric(8, 6, 3, 0), _metric(0)], 25, None, None),
    ([_metric(10, 2, 0, 0, bounced=1)], 3, "SEND_FAILED", None),
    ([_metric(4, 4, 1)], 12, "SYNC_STALE", EXTERNAL),
    ([_metric(-1)], None, None, None),
    ([_metric(10, 1, 0, 0, bounced=3, complaints=2)], 1, "POOL_TOO_SMALL", None),  # floor 25
])
def test_a8_reputation_score_matches_the_previous_formula(metrics, day, issue, external):
    assert warm.reputation_score(metrics, day, issue, external) == _old_formula(metrics, day, issue, external)


@pytest.mark.parametrize("score, day, issue, pool", [
    (99.5, 21, None, "ULTRA_PREMIUM"),
    (99.5, 20, None, "PREMIUM"),
    (97, 14, None, "PREMIUM"),
    (93, 7, None, "GROWTH"),
    (93, 6, None, "FOUNDATION"),
    (100, 30, "SEND_FAILED", "FOUNDATION"),
    (100, 30, "SYNC_STALE", "ULTRA_PREMIUM"),
])
def test_a8_pool_for_score(score, day, issue, pool):
    assert warm.pool_for_score(score, day, issue) == pool


def test_a8_dominant_pool_display_name_and_domain():
    assert warm.dominant_pool(["GROWTH", None, "PREMIUM", "FOUNDATION"]) == "PREMIUM"
    assert warm.dominant_pool([]) == "FOUNDATION"
    assert warm.display_name("jane_doe.smith@x.example.com") == "Jane Doe Smith"
    assert warm.email_domain("A@X.Example.com") == "x.example.com" and warm.email_domain("nobody") == ""


def test_a8_warmup_overview():
    assert warm.warmup_overview([]) == warm.EMPTY_OVERVIEW
    inboxes = [
        SimpleNamespace(warmup_enabled=True, warmup_reputation=90, warmup_issue_code=None, warmup_pool="GROWTH",
                        warmup_day=10, warmup_daily_target=5, warmup_today_sent=3, warmup_today_replied=1,
                        warmup_today_saved=0),
        SimpleNamespace(warmup_enabled=True, warmup_reputation=80, warmup_issue_code="SYNC_STALE",
                        warmup_pool="FOUNDATION", warmup_day=4, warmup_daily_target=4, warmup_today_sent=2,
                        warmup_today_replied=0, warmup_today_saved=1),
        SimpleNamespace(warmup_enabled=False, warmup_reputation=10, warmup_issue_code="X", warmup_pool="PREMIUM",
                        warmup_day=99, warmup_daily_target=50, warmup_today_sent=50, warmup_today_replied=9,
                        warmup_today_saved=9),
    ]
    assert warm.warmup_overview(inboxes) == {
        "total_accounts": 3, "warmup_enabled": 2, "accounts_with_issues": 1, "average_reputation": 85.0,
        "current_pool": "GROWTH", "pool_growth_score": 92, "planned_today": 9, "warmup_sent_today": 5,
        "warmup_replied_today": 1, "saved_today": 1,
    }
