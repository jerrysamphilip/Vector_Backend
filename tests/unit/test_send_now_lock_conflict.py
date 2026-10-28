"""Send now: a MySQL deadlock on one message rolls the session back first (no PendingRollbackError on the
next statement, which turned the batch into a 500), retries the message once while it is still QUEUED, and
otherwise marks it FAILED and goes on."""
import asyncio
from types import SimpleNamespace

from sqlalchemy.exc import OperationalError

from app.services.campaign_send_now_service import CampaignSendNowService, _Tally


class FakeDb:
    def __init__(self, status="QUEUED"):
        self.status, self.calls = status, []

    def rollback(self):
        self.calls.append("rollback")

    def refresh(self, msg):
        self.calls.append("refresh")
        msg.status = self.status

    def add(self, msg):
        self.calls.append("add")

    def commit(self):
        self.calls.append("commit")


def deadlock():
    return OperationalError("UPDATE email_messages", {}, Exception(1213, "Deadlock found"))


def run(db, outcomes):
    svc = CampaignSendNowService(db)
    msg, tally, attempts = SimpleNamespace(message_id="m1", status="QUEUED"), _Tally(), []

    async def send_one(campaign, email_msg, sender_name, t):
        attempts.append(1)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    svc._send_one = send_one
    asyncio.run(svc._send_safely(SimpleNamespace(campaign_id="c1"), msg, None, tally))
    return msg, tally, attempts


def test_deadlock_is_rolled_back_and_retried_once():
    db = FakeDb()
    msg, tally, attempts = run(db, [deadlock(), None])
    assert len(attempts) == 2 and db.calls[0] == "rollback" and tally.failed == 0 and msg.status == "QUEUED"


def test_second_deadlock_marks_failed_without_raising():
    db = FakeDb()
    msg, tally, attempts = run(db, [deadlock(), deadlock()])
    assert len(attempts) == 2 and msg.status == "FAILED" and tally.failed == 1 and db.calls[-1] == "commit"


def test_deadlock_after_the_claim_is_not_retried():
    db = FakeDb(status="SENDING")  # another path already claimed it: don't send it twice
    msg, tally, attempts = run(db, [deadlock()])
    assert len(attempts) == 1 and msg.status == "FAILED" and tally.failed == 1


def test_other_errors_are_not_retried():
    db = FakeDb()
    msg, tally, attempts = run(db, [ValueError("bad template")])
    assert len(attempts) == 1 and msg.status == "FAILED" and db.calls[0] == "rollback"
