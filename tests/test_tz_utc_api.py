"""UAT defect 2 (agent tz): API datetimes carry their zone, and SES bounces keep SES's own time.

The database holds naive UTC (DATETIME(6)); the API must say so (``...Z``) or every browser west of
UTC shows the time hours late. A hard bounce is stamped with ``bounce.timestamp`` (not the webhook's
processing time) and its "Delivery Failed" note is a system notice, not a reply from the contact.
"""
import re
from datetime import datetime

import pytest
from sqlalchemy import text

import app.repositories.email_events as events_mod
import app.services.send_safety as safety_mod
import app.services.ses_event_service as ses_mod
from app.models.conversation import Conversation
from app.models.email_message import EmailMessage
from app.services.ses_event_service import SesEventService
from tests.conftest import RUN, SessionLocal, engine, register_tenant, sql

SENT = datetime(2026, 10, 26, 17, 54, 3, 123456)  # 1:54 PM EDT / 10:54 AM PDT
BOUNCED_AT = datetime(2026, 10, 26, 17, 54, 5)  # SES bounce.timestamp
PROCESSED = datetime(2026, 10, 26, 17, 59, 9, 654321)  # the webhook ran 5 minutes later
NAIVE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")


def _utc(value: str) -> datetime:
    """An API datetime that must name UTC: ``...Z`` (or +00:00), returned naive for comparison."""
    assert value.endswith("Z") or value.endswith("+00:00"), f"no zone on {value!r}"
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _naive_datetimes(obj, path="$"):
    """Every string in a JSON body that looks like a datetime without a zone."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _naive_datetimes(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _naive_datetimes(v, f"{path}[{i}]")
    elif isinstance(obj, str) and NAIVE_ISO.match(obj):
        yield path, obj


@pytest.fixture(scope="module")
def bounced(client):
    """A sent email, then its hard bounce processed by the SES handler with a frozen clock."""
    t = register_tenant(client, "tz")
    h = t["headers"]
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"amy.parks@tz{RUN}.example.com", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "s", "smtp_password": "fixture-smtp-pass", "warmup_enabled": False})
    assert r.status_code == 201, r.text
    inbox_id = r.json()["inbox_id"]
    campaign_id = client.post("/campaigns", headers=h, json={"campaign_name": f"TZ {RUN}"}).json()["campaign_id"]
    contact_email = f"vincent.{RUN}@sharecare.example.com"
    r = client.post("/contacts", headers=h, json={"email": contact_email, "first_name": "Vincent", "last_name": "D"})
    assert r.status_code == 201, r.text
    pid = r.json()["prospect_id"]
    with SessionLocal() as db:
        conv = Conversation(tenant_id=t["tenant_id"], prospect_id=pid, inbox_id=inbox_id,
                            subject="Vincent, AccessOS demo", last_message_at=SENT)
        db.add(conv)
        db.flush()
        m = EmailMessage(conversation_id=conv.id, prospect_id=pid, inbox_id=inbox_id, campaign_id=campaign_id,
                         direction="OUTBOUND", status="SENT", to_email=contact_email,
                         from_email=f"amy.parks@tz{RUN}.example.com", subject="Vincent, AccessOS demo",
                         body_text="Hi", sent_at=SENT, provider_message_id=f"ses-tz-{RUN}")
        db.add(m)
        db.commit()
        conv_id, msg_id = conv.id, m.message_id
    sql("UPDATE sending_inboxes SET last_sync_at = :t WHERE inbox_id = :i", t=SENT, i=inbox_id)

    mp = pytest.MonkeyPatch()
    for mod in (ses_mod, events_mod, safety_mod):
        mp.setattr(mod, "utcnow", lambda: PROCESSED)
    event = {
        "notificationType": "Bounce",
        "bounce": {"bounceType": "Permanent", "bounceSubType": "General", "timestamp": "2026-10-26T17:54:05.000Z",
                   "bouncedRecipients": [{"emailAddress": contact_email,
                                          "diagnosticCode": "smtp; 550 5.4.1 Recipient address rejected"}]},
        "mail": {"messageId": f"ses-tz-{RUN}", "timestamp": "2026-10-26T17:54:03.000Z",
                 "source": f"amy.parks@tz{RUN}.example.com", "tags": {"message_id": [msg_id]}},
    }
    try:
        with SessionLocal() as db:
            assert SesEventService.for_session(db).process_event(event, "sns-tz")["status"] == "processed"
    finally:
        mp.undo()
    # Opening the thread marks it read; that UPDATE no longer moves last_message_at
    # (tests/test_follow_system_notices.py::test_opening_a_thread_keeps_last_message_at)
    last_at = sql("SELECT last_message_at FROM conversations WHERE id = :c", c=conv_id)
    return {"last_at": last_at, "h": h, "conv_id": conv_id, "msg_id": msg_id, "pid": pid, "inbox_id": inbox_id,
            "campaign_id": campaign_id, "contact_email": contact_email}


# ── Every datetime names UTC ─────────────────────────────────────────

def test_conversation_list_last_message_at_is_utc(client, bounced):
    items = client.get("/conversations", headers=bounced["h"], params={"search": "Vincent"}).json()["items"]
    item = next(i for i in items if i["id"] == bounced["conv_id"])
    assert _utc(item["last_message_at"]) == BOUNCED_AT


def test_thread_datetimes_are_utc(client, bounced):
    thread = client.get(f"/conversations/{bounced['conv_id']}", headers=bounced["h"]).json()
    sent = next(m for m in thread["messages"] if m["message_id"] == bounced["msg_id"])
    assert _utc(sent["sent_at"]) == SENT
    steps = {a["type"]: _utc(a["at"]) for a in sent["activity"]}
    assert steps == {"SENT": SENT, "BOUNCED": BOUNCED_AT}
    assert not list(_naive_datetimes(thread))


def test_contact_timeline_datetimes_are_utc(client, bounced):
    body = client.get(f"/contacts/{bounced['pid']}/timeline", headers=bounced["h"]).json()
    at = {i["kind"]: _utc(i["at"]) for i in body["items"] if i["kind"].startswith("EMAIL_")}
    assert at["EMAIL_SENT"] == SENT
    assert at["EMAIL_BOUNCED"] == BOUNCED_AT
    assert not list(_naive_datetimes(body))


def test_inbox_list_last_sync_is_utc(client, bounced):
    r = client.get("/inboxes", headers=bounced["h"])
    assert r.status_code == 200, r.text
    rows = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
    row = next(i for i in rows if i["inbox_id"] == bounced["inbox_id"])
    assert _utc(row["last_sync_at"]) == SENT
    assert not list(_naive_datetimes(r.json()))


def test_get_endpoints_emit_no_zone_less_datetimes(client, bounced):
    """Every parameter-free GET route, and the detail routes of the data made above."""
    from app.main import app

    ids = {"conversation_id": bounced["conv_id"], "prospect_id": bounced["pid"], "contact_id": bounced["pid"],
           "inbox_id": bounced["inbox_id"], "campaign_id": bounced["campaign_id"], "message_id": bounced["msg_id"]}
    paths = []
    for path, ops in app.openapi()["paths"].items():
        if "get" not in ops:
            continue
        names = re.findall(r"{(\w+)}", path)
        if any(n not in ids for n in names):
            continue
        if path.startswith("/tracking") or any(w in path for w in (
                "export", "/pdf", "/docx", "/oauth", "/callback", "/test-imap", "/download")):
            continue
        paths.append(path.format(**ids))
    bad = {}
    for path in paths:
        r = client.get(path, headers=bounced["h"])
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            continue
        found = list(_naive_datetimes(r.json()))
        if found:
            bad[path] = found[:3]
    assert len(paths) > 50
    assert not bad, "\n".join(f"{p}: {v}" for p, v in bad.items())


# ── The bounce keeps SES's time and is not a reply ─────────────────────

def test_bounce_is_stamped_with_ses_bounce_timestamp(bounced):
    with engine.connect() as c:
        status_at = c.execute(text("SELECT final_status_at FROM email_messages WHERE message_id = :m"),
                              {"m": bounced["msg_id"]}).scalar()
        note_at = c.execute(text("SELECT sent_at FROM email_messages WHERE conversation_id = :c "
                                 "AND direction = 'INBOUND'"), {"c": bounced["conv_id"]}).scalar()
        event_times = {r[0] for r in c.execute(text("SELECT event_time FROM email_events WHERE message_id = :m"),
                                               {"m": bounced["msg_id"]})}
    assert status_at == BOUNCED_AT
    assert note_at == BOUNCED_AT
    assert event_times == {BOUNCED_AT}
    assert bounced["last_at"] == BOUNCED_AT


def test_bounce_note_is_a_system_notice_not_a_reply(client, bounced):
    h = bounced["h"]
    thread = client.get(f"/conversations/{bounced['conv_id']}", headers=h).json()
    note = next(m for m in thread["messages"] if m["direction"] == "INBOUND")
    assert note["is_system"] is True
    assert note["from_email"] != bounced["contact_email"]
    items = client.get("/conversations", headers=h, params={"search": "Vincent"}).json()["items"]
    item = next(i for i in items if i["id"] == bounced["conv_id"])
    assert item["last_message_is_system"] is True
    timeline = client.get(f"/contacts/{bounced['pid']}/timeline", headers=h).json()["items"]
    assert "EMAIL_RECEIVED" not in {i["kind"] for i in timeline}
    # The reply classifier never picks it up
    assert sql("SELECT reply_classified_at IS NOT NULL FROM email_messages WHERE message_id = :m",
               m=note["message_id"]) == 1
