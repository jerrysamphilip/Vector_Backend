"""CampaignProspect status priority rules (app/utils/campaign_prospect_status.set_prospect_status).

A later, less important event (the scheduler marking COMPLETED) must never erase a more important one
(a bounce, a reply, an unsubscribe), whatever order the events arrive in.
"""
from itertools import product
from types import SimpleNamespace

import pytest

from app.utils.campaign_prospect_status import STATUS_PRIORITY, set_prospect_status


def cp(status, reason=None):
    return SimpleNamespace(status=status, stopped_reason=reason, prospect_id="p-1", campaign_id="c-1")


def test_none_is_ignored():
    assert set_prospect_status(None, "REPLIED") is False


def test_priority_order_is_the_documented_one():
    assert STATUS_PRIORITY["ACTIVE"] < STATUS_PRIORITY["OPENED"] < STATUS_PRIORITY["COMPLETED"] \
        < STATUS_PRIORITY["PAUSED"] < STATUS_PRIORITY["BOUNCED"] < STATUS_PRIORITY["REPLIED"] \
        < STATUS_PRIORITY["UNSUBSCRIBED"]
    assert STATUS_PRIORITY["PAUSED"] == STATUS_PRIORITY["RECONNECT_ELIGIBLE"]


@pytest.mark.parametrize("current,new", list(product(STATUS_PRIORITY, STATUS_PRIORITY)))
def test_every_transition_follows_priority(current, new):
    row = cp(current)
    changed = set_prospect_status(row, new)
    allowed = current == new or STATUS_PRIORITY[new] >= STATUS_PRIORITY[current]
    assert changed is allowed
    assert row.status == (new if allowed else current)


def test_completed_after_bounce_is_refused_and_reason_kept():
    row = cp("BOUNCED", "hard bounce")
    assert set_prospect_status(row, "COMPLETED", "sequence finished") is False
    assert (row.status, row.stopped_reason) == ("BOUNCED", "hard bounce")


def test_unsubscribe_beats_reply():
    row = cp("REPLIED")
    assert set_prospect_status(row, "UNSUBSCRIBED", "reply: unsubscribe") is True
    assert (row.status, row.stopped_reason) == ("UNSUBSCRIBED", "reply: unsubscribe")
    assert set_prospect_status(row, "REPLIED") is False


def test_same_status_updates_only_a_given_reason():
    row = cp("PAUSED", "manual")
    assert set_prospect_status(row, "PAUSED", "auto-pause: bounces") is True
    assert row.stopped_reason == "auto-pause: bounces"
    assert set_prospect_status(row, "PAUSED") is True
    assert row.stopped_reason == "auto-pause: bounces"


def test_paused_and_reconnect_have_equal_rank():
    row = cp("PAUSED")
    assert set_prospect_status(row, "RECONNECT_ELIGIBLE") is True
    assert set_prospect_status(row, "PAUSED") is True


def test_unknown_statuses_rank_lowest():
    row = cp("SOMETHING_NEW")
    assert set_prospect_status(row, "OPENED") is True and row.status == "OPENED"
    assert set_prospect_status(cp("ACTIVE"), "SOMETHING_NEW") is True
    assert set_prospect_status(cp("OPENED"), "SOMETHING_NEW") is False
