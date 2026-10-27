"""A suspended workspace sends nothing: the scheduler skips its due emails until it is reactivated."""
import uuid
from datetime import timedelta

import pytest

from tests.conftest import RUN, SessionLocal, register_tenant, sql
from app.core.clock import utcnow


@pytest.fixture(scope="module")
def tenant_sus(client):
    return register_tenant(client, "sus")


def _due_message(client, ws) -> str:
    from app.models.email_message import EmailMessage

    h = ws["headers"]
    r = client.post("/contacts", headers=h, json={
        "email": f"sus.{uuid.uuid4().hex[:6]}.{RUN}@susclient{RUN}.example.com", "first_name": "Sue"})
    assert r.status_code == 201, r.text
    pid = r.json()["prospect_id"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Suspend {uuid.uuid4().hex[:6]} {RUN}"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    sql("UPDATE campaigns SET status = 'ACTIVE' WHERE campaign_id = :c", c=cid)
    with SessionLocal() as db:
        msg = EmailMessage(campaign_id=cid, prospect_id=pid, direction="OUTBOUND", status="SCHEDULED",
                           subject="Hi", body_text="Hello", to_email=f"{pid}@x.example.com",
                           scheduled_at=utcnow() - timedelta(minutes=1))
        db.add(msg)
        db.commit()
        return msg.message_id


def _due_ids() -> set:
    from app.services.email_scheduler_service import email_scheduler

    with SessionLocal() as db:
        return {row.message_id for row in email_scheduler._select_due(db, utcnow())}


def test_suspended_tenant_emails_are_not_picked_up(client, tenant_sus):
    mid = _due_message(client, tenant_sus)
    assert mid in _due_ids()
    try:
        sql("UPDATE tenants SET status = 'SUSPENDED' WHERE tenant_id = :t", t=tenant_sus["tenant_id"])
        assert mid not in _due_ids()
    finally:
        sql("UPDATE tenants SET status = 'ACTIVE' WHERE tenant_id = :t", t=tenant_sus["tenant_id"])
    # Reactivated: the email is due again (nothing was cancelled while suspended)
    assert mid in _due_ids()
    assert sql("SELECT status FROM email_messages WHERE message_id = :m", m=mid) == "SCHEDULED"
