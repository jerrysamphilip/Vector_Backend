"""Agent follow: "Send now" reports its run start as UTC (``...Z``), and the value round-trips as ``since``."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.campaign_send_now_service import CampaignSendNowService, normalize_run_start

SINCE = datetime(2026, 10, 26, 17, 54, 3, 123456)


def _service(monkeypatch):
    svc = CampaignSendNowService(SimpleNamespace())
    criteria = []
    svc.queue = SimpleNamespace(send_now_criteria=lambda cid, started, after: criteria.append(started) or [],
                                send_now_batch=lambda crit, limit: [], messages=lambda ids: [])
    monkeypatch.setattr(CampaignSendNowService, "_require_sendable", staticmethod(lambda campaign: None))
    return svc, criteria


def test_started_at_names_utc(monkeypatch):
    svc, criteria = _service(monkeypatch)
    out = asyncio.run(svc.send_now(SimpleNamespace(campaign_id="c1"), 10, None, SINCE))
    assert out["status"] == "no_emails"
    assert out["started_at"] == "2026-10-26T17:54:03.123456Z"
    assert criteria == [SINCE]


def test_started_at_round_trips_as_since(monkeypatch):
    svc, criteria = _service(monkeypatch)
    # The browser sends the first call's started_at back; FastAPI parses the Z as an aware UTC datetime
    since = datetime.fromisoformat("2026-10-26T17:54:03.123456Z")
    assert since.tzinfo == timezone.utc and normalize_run_start(since) == SINCE
    out = asyncio.run(svc.send_now(SimpleNamespace(campaign_id="c1"), 10, None, since))
    assert out["started_at"] == "2026-10-26T17:54:03.123456Z" and criteria == [SINCE]
