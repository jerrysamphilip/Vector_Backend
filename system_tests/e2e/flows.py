"""Reusable business flows for the journeys: everything here goes through the same HTTP API the UI uses.

DB access is limited to (a) read-only verification and (b) the explicit, labelled test clocks
(`travel_*`) that move scheduled_at inside the test's own tenant instead of waiting days.
"""
import json
import time
from datetime import datetime, timedelta

from .helpers import (FakeImap, HOST_FROM_CONTAINERS, q, req, rows, scalar, ses_captured, ses_verify, sql,
                      upload_csv, wait_until)


# ───────────────────────── mailboxes ─────────────────────────

def mk_inbox(ws, local, domain, imap: FakeImap = None, password="Pw-e2e-1", delay=3, daily=500, **kw):
    """POST /inboxes the way the 'connect mailbox' screen does. With `imap`, IMAP settings point at the fake server."""
    addr = f"{local}@{domain}"
    body = {"email_address": addr, "smtp_host": "smtp.invalid", "smtp_username": local, "smtp_password": password,
            "warmup_enabled": False, "delay_between_emails": delay, "daily_limit": daily, "max_emails_per_day": daily}
    if imap is not None:
        imap.add_account(addr, password)
        body.update(imap_host=HOST_FROM_CONTAINERS, imap_port=imap.port, imap_username=addr, imap_password=password)
    body.update(kw)
    s, b = req("POST", "/inboxes", body, ws.token)
    assert s == 201, ("inbox", s, b)
    ses_verify(addr)            # the harness verifies the identity in the SES stand-in (as an operator would in SES)
    ses_verify(domain)
    return b["inbox_id"], addr, b


def detach_imap(ws):
    """Stop the worker polling the (about to disappear) fake IMAP server."""
    sql(f"UPDATE sending_inboxes SET imap_host=NULL WHERE tenant_id={q(ws.tenant_id)} AND imap_host={q(HOST_FROM_CONTAINERS)}")


# ───────────────────────── contacts ─────────────────────────

CSV_HEAD = "First Name,Last Name,Company Name,email,Designation"


def upload_list(ws, csv_text, title):
    """Validate (dry run) then confirm accepted rows - the two UI calls. Returns (validation, confirmation)."""
    s, v = upload_csv(ws.token, f"{title}.csv", csv_text)
    assert s == 200, ("validate", s, v)
    accepted = [r for r in v["records"] if r["status"] == "ACCEPTED"]
    s, c = req("POST", f"/uploads/{v['upload_id']}/confirmations",
               {"upload_id": v["upload_id"], "title": title, "records": accepted}, ws.token)
    assert s == 200, ("confirm", s, c)
    return v, c


def contact_by_email(ws_token, email_addr):
    s, b = req("GET", f"/contacts?q={email_addr}&page_size=50", token=ws_token)
    for it in (b.get("items") or []) if isinstance(b, dict) else []:
        if it["email"].lower() == email_addr.lower():
            return it
    return None


# ───────────────────────── campaigns ─────────────────────────

def create_campaign(ws, name, inbox_ids, waits=(0, 2, 3), sender_name=None, sender_title=None, **kw):
    """POST /campaigns + POST /campaigns/{id}/sequences per step (what the wizard's steps 1-2 call)."""
    body = {"campaign_name": name, "inbox_ids": inbox_ids, "respect_timezone": False, "campaign_timezone": "UTC",
            "send_window_start": "00:00:00", "send_window_end": "23:59:00", "min_gap_minutes": 2}
    if sender_name:
        body["sender_name"] = sender_name
    if sender_title:
        body["sender_title"] = sender_title
    body.update(kw)
    s, b = req("POST", "/campaigns", body, ws.token)
    assert s == 201, ("campaign", s, b)
    cid = b["campaign_id"]
    for i, w in enumerate(waits, 1):
        s, sb = req("POST", f"/campaigns/{cid}/sequences", {"step_number": i, "wait_days": w}, ws.token)
        assert s == 201, ("sequence", s, sb)
    return cid, b


def generate_content(ws, cid, lid, waits, product="Vector CRM", company="Systest", description="CRM for outbound teams"):
    """The wizard's 'process list' call (LLM unavailable -> template fallback)."""
    steps = [{"step_number": i, "wait_days": w} for i, w in enumerate(waits, 1)]
    return req("POST", f"/campaigns/{cid}/generations",
               {"campaign_id": cid, "list_id": lid, "product_name": product, "company_name": company,
                "campaign_description": description, "sequence_steps": steps}, ws.token)


def sequences(ws, cid):
    s, b = req("GET", f"/campaigns/{cid}/sequences", token=ws.token)
    assert s == 200, (s, b)
    return sorted(b["steps"], key=lambda x: x["step_number"])


def add_missing_step_templates(ws, cid, subjects=None):
    """WORKAROUND for the LLM-fallback gap (only step 1 gets a template): create the missing
    follow-up templates through POST /templates, once per persona already present. Returns count created."""
    s, t = req("GET", f"/campaigns/{cid}/templates", token=ws.token)
    have = {(x["sequence_id"], x.get("designation")) for x in t.get("templates", [])}
    personas = sorted({x.get("designation") for x in t.get("templates", [])}) or [None]
    made = 0
    for st in sequences(ws, cid):
        if st["step_number"] == 1:
            continue
        for des in personas:
            if (st["sequence_id"], des) in have:
                continue
            subj = (subjects or {}).get(st["step_number"], "Following up, {{first_name}}")
            body = (f"<p>Hi {{{{first_name}}}},</p><p>Following up (step {st['step_number']}) on my note about "
                    f"{{{{company_name}}}}.</p>")
            s, r = req("POST", "/templates", {"campaign_id": cid, "sequence_id": st["sequence_id"], "subject": subj,
                                              "body": body, "designation": des}, ws.token)
            assert s == 201, ("template", s, r)
            made += 1
    return made


def enroll(ws, cid, lid, **kw):
    body = {"campaign_id": cid, "list_id": lid, "persona_types": None, "exclude_personal_emails": True,
            "cool_off_days": 0, "excluded_prospect_ids": None}
    body.update(kw)
    return req("POST", f"/campaigns/{cid}/enrollments", body, ws.token)


def launch(ws, cid):
    return req("POST", f"/campaigns/{cid}/launch", None, ws.token)


# ───────────────────────── message state (read-only DB views) ─────────────────────────

MSG_COLS = ("m.message_id, p.email, s.step_number, m.status, IFNULL(m.scheduled_at,''), IFNULL(m.sent_at,''), "
            "IFNULL(m.inbox_id,''), IFNULL(m.ses_message_id,''), IFNULL(m.provider_message_id,''), "
            "IFNULL(m.failure_reason,''), IFNULL(m.final_status,'')")


def msgs(cid, step=None, status=None, email_addr=None):
    where = [f"m.campaign_id={q(cid)}", "m.direction='OUTBOUND'"]
    if step is not None:
        where.append(f"s.step_number={int(step)}")
    if status:
        where.append("m.status IN (" + ",".join(q(x) for x in (status if isinstance(status, (list, tuple)) else [status])) + ")")
    if email_addr:
        where.append(f"LOWER(p.email)={q(email_addr.lower())}")
    out = []
    for r in rows(f"SELECT {MSG_COLS} FROM email_messages m JOIN email_sequences s ON s.sequence_id=m.sequence_id "
                  f"JOIN prospects p ON p.prospect_id=m.prospect_id WHERE {' AND '.join(where)} ORDER BY s.step_number, p.email"):
        out.append(dict(zip(("message_id", "email", "step", "status", "scheduled_at", "sent_at", "inbox_id",
                             "ses_id", "pm_id", "failure", "final"), r)))
    return out


def dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None


def utc_now():
    return datetime.strptime(scalar("SELECT DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%d %H:%i:%s')"), "%Y-%m-%d %H:%M:%S")


def travel_due(cid, step, emails=None, seconds_ago=60):
    """TEST CLOCK: make a step's pending messages due now (instead of waiting for the planned day).
    Scoped to one campaign (= our tenant) and optionally to specific recipients."""
    cond = f"AND LOWER(p.email) IN ({','.join(q(e.lower()) for e in emails)})" if emails else ""
    sql(f"UPDATE email_messages m JOIN email_sequences s ON s.sequence_id=m.sequence_id "
        f"JOIN prospects p ON p.prospect_id=m.prospect_id "
        f"SET m.scheduled_at=UTC_TIMESTAMP() - INTERVAL {int(seconds_ago)} SECOND "
        f"WHERE m.campaign_id={q(cid)} AND s.step_number={int(step)} AND m.status IN ('SCHEDULED','QUEUED') {cond}")


def captured_for(emails=None, domain=None, since=0):
    """Captured SES messages for these recipients (the stand-in is shared with other test agents)."""
    want = {e.lower() for e in emails} if emails else None
    out = []
    for c in ses_captured(since=since):
        if want is not None and c.to not in want:
            continue
        if domain and not c.source.rstrip(">").lower().endswith("@" + domain.lower()):
            continue
        out.append(c)
    return out


def wait_status(cid, step, n, status="SENT", timeout=300):
    """Wait until n messages of the step reach the status."""
    return wait_until(lambda: len(msgs(cid, step, status)) >= n, timeout=timeout, interval=4)


def sub_aliases(base_email_local, rid):
    return f"{base_email_local}-{rid}"


def quick_campaign(ws, tag, inbox_ids, n, rid, waits=(0,), email_domain=None, prefix="c", sender="Dana Test", wait_secs=420):
    """Upload n contacts, create + enrol + launch a campaign (API only), put step 1 due (test clock) and wait for the
    worker to send all n through the SES stand-in. Returns dict(cid, lid, emails, sent=[msgs])."""
    ed = email_domain or f"{tag}-{rid}.example"
    emails = [f"{prefix}{i:02d}@{ed}" for i in range(n)]
    lines = [f"Pat{i},Test{i},{tag.title()} Co {i},{e},Director" for i, e in enumerate(emails)]
    v, c = upload_list(ws, CSV_HEAD + "\n" + "\n".join(lines) + "\n", f"{tag} list {rid}")
    assert v["accepted"] == n, v
    cid, _ = create_campaign(ws, f"{tag} {rid}", inbox_ids, waits=waits, sender_name=sender)
    s, g = generate_content(ws, cid, c["list_id"], waits)
    assert s == 200, (s, g)
    if len(waits) > 1:
        add_missing_step_templates(ws, cid)
    s, e = enroll(ws, cid, c["list_id"])
    assert s == 200 and e["enrolled_count"] == n, (s, e)
    s, l = launch(ws, cid)
    assert s == 200, (s, l)
    travel_due(cid, 1)
    wait_status(cid, 1, n, "SENT", timeout=wait_secs)
    sent = msgs(cid, 1, "SENT")
    return {"cid": cid, "lid": c["list_id"], "emails": emails, "sent": sent}
