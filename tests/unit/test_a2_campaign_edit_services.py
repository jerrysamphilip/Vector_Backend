"""A2 / F-04 CampaignEditService with a fake unit of work: version required, refusals roll back, dry run."""
from types import SimpleNamespace

import pytest
from datetime import date

from app.core.concurrency import PreconditionRequired
from app.core.errors import BadRequestError, ConflictError
from app.domain.campaigns_edit import CampaignEdit
from app.services.campaign_edit_service import CampaignEditService

USER = SimpleNamespace(user_id="u1", tenant_id="t1")


class FakeCampaigns:
    def __init__(self, campaign):
        self.campaign = campaign

    def lock(self, tenant_id, campaign_id):
        return self.campaign if tenant_id == "t1" else None

    def flush(self):
        pass


class FakeMessages:
    def lock_unsent(self, campaign_id):
        return []


class FakeUow:
    def __init__(self, campaign):
        self.campaigns, self.messages = FakeCampaigns(campaign), FakeMessages()
        self.entered = self.commits = self.rollbacks = 0
        self.claimed = []
        self.session = None

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, *rest):
        if exc_type:
            self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def claim_version(self, entity, expected, record):
        self.claimed.append(expected)


def _campaign(**kw):
    base = dict(campaign_id="c1", tenant_id="t1", status="DRAFT", sender_name="Ann", sender_title=None, cta_link=None,
                send_window_start=None, send_window_end=None, start_date=None, end_date=None, campaign_timezone="UTC",
                inboxes=[])
    base.update(kw)
    return SimpleNamespace(**base)


def _service(campaign):
    uow = FakeUow(campaign)
    return CampaignEditService(uow=uow, audit=SimpleNamespace(log_action=lambda **kw: None), crud=object(),
                               emails=object()), uow


def test_version_is_required_before_anything_is_opened():
    svc, uow = _service(_campaign())
    with pytest.raises(PreconditionRequired):
        svc.edit(USER, "c1", CampaignEdit())
    assert uow.entered == 0


def test_completed_campaign_is_409_and_rolled_back():
    svc, uow = _service(_campaign(status="COMPLETED"))
    with pytest.raises(ConflictError):
        svc.edit(USER, "c1", CampaignEdit(expected_version=3))
    assert uow.commits == 0 and uow.rollbacks == 1 and uow.claimed == []


def test_start_date_after_launch_is_400():
    svc, uow = _service(_campaign(status="DRAFT"))

    svc_campaign = uow.campaigns.campaign
    svc_campaign.status = "PAUSED"
    with pytest.raises(BadRequestError, match="start date"):
        svc.edit(USER, "c1", CampaignEdit(settings={"start_date": date(2031, 1, 1)}, expected_version=3))
    assert uow.claimed == [3] and uow.commits == 0


def test_draft_settings_dry_run_rolls_back_and_counts():
    svc, uow = _service(_campaign())
    answer = svc.edit(USER, "c1", CampaignEdit(settings={"sender_name": "Bob", "sender_title": None},
                                               expected_version=2), dry_run=True)
    assert answer["dry_run"] and answer["settings_changed"] == ["sender_name"]
    assert uow.commits == 0 and uow.rollbacks == 1
