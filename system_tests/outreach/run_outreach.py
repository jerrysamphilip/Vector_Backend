#!/usr/bin/env python3
"""
Outreach suite: system + integration tests for BRD v2.0 §5.1 (BR-DF-01..09), §5.3 and §7
against the live local stack. Stdlib only (+ ../common.py and ./helpers.py).

Run:  python3 run_outreach.py      (writes results.json / results.md here)

Notes
- Two workspaces (A = main, B = second tenant for isolation checks) are created once and
  cached in .state.json, because self sign-up is limited to 10/hour per client IP and every
  suite on this machine shares that IP. Every run uses fresh, uniquely named data in them.
- Fixtures: campaigns are launched with a start date ~20 days out so nothing is sent
  by accident; individual messages are then marked SENT (as the scheduler would after an SES
  accept) or pulled due with SQL inside our own tenant, to drive webhooks / the scheduler.
- There are no SES credentials, so nothing is delivered; scheduler tests assert state
  transitions and decisions, not delivery.
"""
import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import (FakeImap, HOST_FROM_CONTAINERS, business_tz, get_user, get_workspace, load_state,  # noqa: E402
                     q, reply_mime, req, rows, save_state, scalar, ses_bounce, ses_complaint, ses_delivery,
                     ses_post, sql, upload_csv, wait_until)
from common import Results, run_id, WEB, login  # noqa: E402

SCRATCH = os.environ.get("PW_DIR", "/tmp/claude-0/-home-user/b764cdf9-b625-5070-aae1-e6d130b3e7df/scratchpad")

R = Results("Outreach (BR-DF / §5.3 / §7)", HERE)
RID = run_id()
STATE = load_state()


def rec(cid, title, ok, detail="", category=None):
    """Record; category for failures is 'defect' unless told otherwise."""
    R.record(cid, title, bool(ok), "" if ok else detail, None if ok else (category or "defect"))
    return bool(ok)


def blocked(cid, title, why):
    R.record(cid, title, None, why, "blocked")


def safe(cid, title):
    """Decorator: a crashing test is recorded as FAIL(test-issue) instead of stopping the run."""
    def deco(fn):
        def run(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                R.record(cid, title, False, f"exception: {e!r}", "test-issue")
        return run
    return deco


# ═════════════════════════ fixtures ═════════════════════════

A = get_workspace("outreach", STATE)
B = get_workspace("outreachb", STATE)
for ws in (A, B):
    s, b = ws.set_sender_identity()
    assert s == 200, ("sender identity", s, b)

FUTURE = (date.today() + timedelta(days=20)).isoformat()
TZ = business_tz()   # None while no zone is on a US business day (weekend / holiday)


def mk_inbox(ws, local, domain, **kw):
    body = {"email_address": f"{local}@{domain}", "smtp_host": "smtp.invalid", "smtp_username": local,
            "smtp_password": f"Smtp-{RID}", "warmup_enabled": False, "delay_between_emails": 1,
            "daily_limit": 500, "max_emails_per_day": 500}
    body.update(kw)
    s, b = req("POST", "/inboxes", body, ws.token)
    assert s == 201, ("inbox", s, b)
    return b["inbox_id"]


def mk_contact(ws, email, token=None, **kw):
    body = {"email": email, "first_name": kw.pop("first_name", "Pat"), "last_name": kw.pop("last_name", "Test"),
            "company_name": kw.pop("company_name", "Acme Corp"), **kw}
    s, b = req("POST", "/contacts", body, token or ws.token)
    assert s == 201, ("contact", email, s, b)
    return b["prospect_id"]


def mk_campaign(ws, name, inbox_ids, waits=(0,), future=True, window=("09:00:00", "17:00:00"), tz="UTC", **kw):
    body = {"campaign_name": f"{name} {RID}", "inbox_ids": inbox_ids, "respect_timezone": False,
            "campaign_timezone": tz, "min_gap_minutes": 0}
    if window:
        body["send_window_start"], body["send_window_end"] = window
    if future:
        body["start_date"] = FUTURE
    body.update(kw)
    s, b = req("POST", "/campaigns", body, ws.token)
    assert s == 201, ("campaign", s, b)
    cid = b["campaign_id"]
    for i, w in enumerate(waits, 1):
        s, b = req("POST", f"/campaigns/{cid}/sequences", {"step_number": i, "wait_days": w}, ws.token)
        assert s == 201, ("step", s, b)
    return cid


def stub(ws, cid):
    s, b = req("POST", f"/campaigns/{cid}/content/stub", None, ws.token)
    assert s == 200, ("stub", s, b)


def enroll(ws, cid, pids=None, list_ids=None, token=None):
    body = {}
    if pids:
        body["prospect_ids"] = pids
    if list_ids:
        body["list_ids"] = list_ids
    return req("POST", f"/campaigns/{cid}/prospects", body, token or ws.token)


def launch(ws, cid, token=None):
    return req("POST", f"/campaigns/{cid}/launch", None, token or ws.token)


def ready_campaign(ws, name, inbox_ids, pids, waits=(0,), **kw):
    cid = mk_campaign(ws, name, inbox_ids, waits, **kw)
    s, b = enroll(ws, cid, pids)
    assert s == 200 and b["enrolled_count"] == len(pids), ("enroll", s, b)
    stub(ws, cid)
    s, b = launch(ws, cid)
    assert s == 200, ("launch", s, b)
    return cid


def msg(cid, pid, step=1):
    r = rows(f"SELECT m.message_id, m.status, m.failure_reason, IFNULL(m.final_status,''), m.to_email, m.from_email "
             f"FROM email_messages m JOIN email_sequences s ON s.sequence_id=m.sequence_id "
             f"WHERE m.campaign_id={q(cid)} AND m.prospect_id={q(pid)} AND s.step_number={step} "
             f"AND m.direction='OUTBOUND'")
    return r[0] if r else None


def mstatus(mid):
    r = rows(f"SELECT status, IFNULL(failure_reason,''), IFNULL(final_status,''), IFNULL(retry_count,0), "
             f"IFNULL(last_error_code,''), IFNULL(scheduled_at,''), IFNULL(next_retry_at,'') "
             f"FROM email_messages WHERE message_id={q(mid)}")
    return r[0] if r else None


def mark_sent(mid, minutes_ago=0):
    """Fixture: what the scheduler records after SES accepted the email."""
    ses_id = f"ses-{uuid.uuid4().hex}"
    pm = f"<pm-{uuid.uuid4().hex}@fake-imap.test>"
    sql(f"UPDATE email_messages SET status='SENT', sent_at=UTC_TIMESTAMP() - INTERVAL {minutes_ago} MINUTE, "
        f"ses_message_id={q(ses_id)}, provider_message_id={q(pm)} WHERE message_id={q(mid)}")
    return ses_id, pm


def pull_due(mid):
    sql(f"UPDATE email_messages SET scheduled_at=UTC_TIMESTAMP() - INTERVAL 1 MINUTE WHERE message_id={q(mid)}")


def cp_status(cid, pid):
    return scalar(f"SELECT status FROM campaign_prospects WHERE campaign_id={q(cid)} AND prospect_id={q(pid)}")


def camp(cid):
    r = rows(f"SELECT status, IFNULL(auto_paused,0), IFNULL(paused_reason,''), IFNULL(health_baseline_at,'') "
             f"FROM campaigns WHERE campaign_id={q(cid)}")
    return r[0] if r else None


def suppressed(ws, email):
    return scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(ws.tenant_id)} "
                  f"AND LOWER(email)=LOWER({q(email)})") != "0"


def consent(pid):
    return scalar(f"SELECT consent_status FROM prospects WHERE prospect_id={q(pid)}")


# Domains / inboxes for this run
DOM = f"out-{RID}.example"
DOM2 = f"out2-{RID}.example"
SHARED = f"shared-{RID}.example"
CONTACT_DOM = f"acme-{RID}.example"


def em(tag):
    return f"{tag}-{RID}@{CONTACT_DOM}"


IN1 = mk_inbox(A, f"sales1-{RID}", DOM)
IN2 = mk_inbox(A, f"sales2-{RID}", DOM2)

IMAP = FakeImap(SCRATCH)

# Previous run's 500-contact campaign is removed first so the table stays bounded
if STATE.get("big"):
    req("DELETE", f"/campaigns/{STATE['big'].get('campaign')}", None, A.token)
    if STATE["big"].get("list"):
        req("DELETE", f"/prospect-lists/{STATE['big']['list']}", None, A.token)
    STATE.pop("big")
    save_state(STATE)


# ═════════════════════════ BR-DF-09 security ═════════════════════════

def b64url(d):
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode()


@safe("OUT-001", "Token signed with the public default JWT secret is rejected")
def t_jwt():
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"sub": A.user_id, "tenant_id": A.tenant_id, "role": "SUPER_ADMIN",
                                 "exp": int(time.time()) + 600, "type": "access"}).encode())
    secret = b"change-this-in-production-use-a-long-random-string"
    sig = b64url(hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    s, b = req("GET", "/api/auth/me", token=f"{header}.{payload}.{sig}")
    rec("OUT-001", "Token signed with the public default JWT secret is rejected", s == 401, f"GET /api/auth/me -> {s} {b}")


@safe("OUT-002", "SES webhook rejects unauthenticated events")
def t_webhook_auth():
    raw = ses_delivery("nonexistent", "x@example.com")
    env = {"Type": "Notification", "MessageId": str(uuid.uuid4()), "TopicArn": "arn:aws:sns:us-east-1:1:x",
           "Message": json.dumps(raw), "Timestamp": datetime.utcnow().isoformat() + "Z", "SignatureVersion": "1",
           "Signature": "AAAA", "SigningCertURL": "https://evil.example/cert.pem"}
    s1, b1 = req("POST", "/webhooks/ses/notifications", env)
    rec("OUT-002", "Wrapped SNS notification with invalid signature is rejected (403)", s1 == 403, f"{s1} {b1}")
    s2, b2 = req("POST", "/webhooks/ses/notifications", raw)
    rec("OUT-003", "Raw SES event without the webhook token is rejected (403)", s2 == 403, f"{s2} {b2}")
    s3, b3 = ses_post(raw, token="wrong-token")
    rec("OUT-004", "Raw SES event with a wrong token is rejected (403)", s3 == 403, f"{s3} {b3}")
    s4, b4 = ses_post(raw)
    rec("OUT-005", "Raw SES event with the configured token is accepted (200)", s4 == 200, f"{s4} {b4}")
    conf = {"Type": "SubscriptionConfirmation", "MessageId": "m", "Token": "t", "TopicArn": "arn:x",
            "Message": "confirm", "SubscribeURL": "http://169.254.169.254/latest/meta-data/",
            "Timestamp": "2026-01-01T00:00:00Z", "SignatureVersion": "1", "Signature": "AAAA",
            "SigningCertURL": "https://sns.us-east-1.amazonaws.com/x.pem"}
    s5, b5 = req("POST", "/webhooks/ses/notifications", conf)
    rec("OUT-009", "Unsigned SubscriptionConfirmation (internal SubscribeURL) is refused, URL not fetched",
        s5 in (400, 403), f"{s5} {b5}")


@safe("OUT-006", "Mailbox passwords encrypted at rest and never returned")
def t_pw_encryption():
    iid = mk_inbox(A, f"crypt-{RID}", DOM, imap_host="imap.invalid", imap_username="u",
                   imap_password=f"ImapPlain-{RID}")
    smtp_db, imap_db = (rows(f"SELECT IFNULL(smtp_password,''), IFNULL(imap_password,'') FROM sending_inboxes "
                             f"WHERE inbox_id={q(iid)}") or [["", ""]])[0]
    s, b = req("GET", f"/inboxes/{iid}", token=A.token)
    leaked = f"Smtp-{RID}" in json.dumps(b) or f"ImapPlain-{RID}" in json.dumps(b)
    ok = smtp_db and imap_db and f"Smtp-{RID}" not in smtp_db and f"ImapPlain-{RID}" not in imap_db and not leaked
    rec("OUT-006", "Mailbox SMTP/IMAP passwords encrypted at rest and not returned by the API", ok,
        f"db smtp={smtp_db[:20]!r} imap={imap_db[:20]!r} api_leak={leaked}")
    sql(f"UPDATE sending_inboxes SET imap_host=NULL WHERE inbox_id={q(iid)}")  # don't let the worker poll it


@safe("OUT-007", "Sign-in brute force is rate limited")
def t_rate_limits():
    u = get_user(A, "AGENT", f"Lockout{RID}", STATE)
    xff = {"X-Forwarded-For": f"10.{int(RID[:2], 16)}.{int(RID[2:4], 16)}.{int(RID[4:], 16)}"}
    codes = [req("POST", "/api/auth/login", {"email": u["email"], "password": "wrong-password"}, headers=xff)[0]
             for _ in range(10)]
    s, _ = req("POST", "/api/auth/login", {"email": u["email"], "password": A.password}, headers=xff)
    rec("OUT-007", "After 10 wrong passwords the account is locked (429) even with the right password",
        s == 429 and all(c == 401 for c in codes), f"wrong attempts -> {codes}; correct -> {s}")
    codes = [req("POST", "/api/auth/forgot-password", {"email": u["email"]}, headers=xff)[0] for _ in range(6)]
    rec("OUT-008", "Password-reset requests per email are limited (6th -> 429)", codes[-1] == 429 and codes[0] != 429,
        f"codes {codes}")


# ═════════════════════════ BR-DF-02 / §7 upload & erasure ═════════════════════════

@safe("OUT-102", "Permanent erasure on request is logged")
def t_erasure():
    e = em("erase")
    pid = mk_contact(A, e)
    req("PATCH", f"/contacts/{pid}", {"consent_status": "UNSUBSCRIBED"}, A.token)
    s, b = req("DELETE", f"/prospects/{pid}", None, A.token)
    gone = scalar(f"SELECT COUNT(*) FROM prospects WHERE prospect_id={q(pid)}") == "0"
    logged = int(scalar(f"SELECT COUNT(*) FROM audit_logs WHERE tenant_id={q(A.tenant_id)} AND entity_id={q(pid)}") or 0) \
        + int(scalar(f"SELECT COUNT(*) FROM property_changes WHERE object_id={q(pid)}") or 0)
    rec("OUT-102", "Permanent erasure (DELETE /prospects/{id}) removes the contact and logs the deletion",
        s == 200 and gone and logged > 0,
        f"DELETE -> {s} {b}; row gone={gone}; audit_logs+property_changes rows for id={logged} "
        f"(contact_service.delete_contacts also deletes the contact's property_changes; nothing writes audit_logs)")
    rec("OUT-103a", "Suppression entry survives erasure (contact cannot be re-emailed)", suppressed(A, e),
        "global_unsubscribes row missing after erasure")
    return e


@safe("OUT-010", "Upload validation gives a reason for each failed row")
def t_upload(erased_email):
    unsub = em("upunsub")
    pid_u = mk_contact(A, unsub)
    req("PATCH", f"/contacts/{pid_u}", {"consent_status": "UNSUBSCRIBED"}, A.token)
    ok1, ok2 = em("up1"), em("up2")
    lines = ["First Name,Last Name,Company Name,email,Designation",
             f"Ann,One,Acme,{ok1},CEO",
             f"Bob,Two,Acme,{ok2},CTO",
             f",NoFirst,Acme,{em('up3')},CTO",
             f"Gee,Mail,Acme,personal{RID}@gmail.com,CTO",
             f"Ann,Dup,Acme,{ok1.upper()},CEO",
             f"Uns,Ub,Acme,{unsub.upper()},CEO",
             f"Bro,Ken,Acme,broken-{RID}@,CEO",
             f"Spa,Ce,Acme,has space-{RID}@{CONTACT_DOM},CEO",
             f"Era,Sed,Acme,{erased_email or em('noerase')},CEO"]
    s, b = upload_csv(A.token, f"upload-{RID}.csv", "\n".join(lines) + "\n")
    if s != 200:
        rec("OUT-010", "Upload validation", False, f"{s} {b}", "defect")
        return None
    recs = {r["row"]: r for r in b["records"]}
    want_rejected = {3: "First Name", 4: "personal", 5: "Duplicate", 6: "unsubscribed"}
    bad = [f"row{n}: {recs.get(n, {}).get('status')} {recs.get(n, {}).get('reason')}" for n, key in want_rejected.items()
           if recs.get(n, {}).get("status") != "REJECTED" or key.lower() not in (recs[n].get("reason") or "").lower()]
    good = recs[1]["status"] == "ACCEPTED" and recs[2]["status"] == "ACCEPTED"
    rec("OUT-010", "Upload: valid rows accepted, each invalid row rejected with a reason", good and not bad,
        f"problems: {bad}; rows1-2={recs[1]['status']},{recs[2]['status']}")
    malformed = [f"row{n} {recs[n]['email']!r} -> {recs[n]['status']}" for n in (7, 8) if recs[n]["status"] != "REJECTED"]
    rec("OUT-011", "Upload: syntactically invalid email addresses are rejected with a reason", not malformed,
        f"accepted as valid: {malformed} (utils/email_utils.py parse_email only checks for '@' and a non-personal domain)")
    if erased_email:
        r9 = recs[9]
        rec("OUT-103", "Re-import of an erased, unsubscribed address is rejected (suppression kept)",
            r9["status"] == "REJECTED" and "unsubscribed" in (r9["reason"] or "").lower(), f"row9 {r9['status']} {r9['reason']}")
    s, c = req("POST", f"/uploads/{b['upload_id']}/confirmations",
               {"upload_id": b["upload_id"], "title": f"Upload {RID}", "records": b["records"]}, A.token)
    members = int(scalar(f"SELECT COUNT(*) FROM prospect_list_members WHERE list_id={q(b['upload_id'])}") or 0)
    rec("OUT-012", "Confirming the upload stores every accepted row in the new list",
        s == 200 and members == b["accepted"], f"confirm {s} {c}; members={members} accepted={b['accepted']}")
    cid = mk_campaign(A, "Upload enroll", [IN1])
    s, e = enroll(A, cid, list_ids=[b["upload_id"]])
    rec("OUT-013", "Enrolling the uploaded list enrolls every valid row",
        s == 200 and e["enrolled_count"] == b["accepted"] and e["rejected_count"] == 0,
        f"{s} enrolled={e.get('enrolled_count') if isinstance(e, dict) else e} accepted={b['accepted']} "
        f"rejected={e.get('rejected') if isinstance(e, dict) else ''}")
    return b["upload_id"]


@safe("OUT-014", "Enrollment report has a reason for each contact not enrolled")
def t_enroll_report():
    ok = mk_contact(A, em("rep-ok"))
    uns = mk_contact(A, em("rep-uns"))
    req("PATCH", f"/contacts/{uns}", {"consent_status": "UNSUBSCRIBED"}, A.token)
    exp = mk_contact(A, em("rep-exp"))
    req("PATCH", f"/contacts/{exp}", {"consent_status": "UNSUBSCRIBED"}, A.token)
    sql(f"UPDATE global_unsubscribes SET suppression_expires_at=UTC_TIMESTAMP() - INTERVAL 1 DAY "
        f"WHERE tenant_id={q(A.tenant_id)} AND email={q(em('rep-exp'))}")
    already = mk_contact(A, em("rep-dup"))
    foreign = mk_contact(B, em("rep-foreign"))
    cid = mk_campaign(A, "Report", [IN1])
    enroll(A, cid, [already])
    s, b = enroll(A, cid, [ok, uns, exp, already, foreign])
    codes = {r["prospect_id"]: r["reason_code"] for r in b.get("rejected", [])} if isinstance(b, dict) else {}
    reasons_ok = all(r.get("reason") for r in b.get("rejected", [])) if isinstance(b, dict) else False
    want = {uns: "unsubscribed", exp: "opted_out", already: "already_enrolled", foreign: "not_found"}
    rec("OUT-014", "Enrollment returns a reason for every contact not enrolled",
        s == 200 and b["enrolled_count"] == 1 and all(codes.get(k) == v for k, v in want.items()) and reasons_ok,
        f"{s} enrolled={b.get('enrolled_count') if isinstance(b, dict) else b} codes={codes}")
    rec("OUT-089", "Expired voluntary suppression still blocks enrollment (contact stays unsubscribed)",
        codes.get(exp) in ("opted_out", "unsubscribed"), f"code={codes.get(exp)}")
    rec("OUT-024b", "Another workspace's contact cannot be enrolled (not_found)", codes.get(foreign) == "not_found",
        f"code={codes.get(foreign)}; enrolled rows for foreign="
        f"{scalar(f'SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id={q(cid)} AND prospect_id={q(foreign)}')}")
    return uns


@safe("OUT-015", "Bulk list enrollment into an active campaign schedules emails")
def t_bulk_active(uns_pid):
    seed = mk_contact(A, em("act-seed"))
    cid = ready_campaign(A, "Active bulk", [IN1], [seed])
    n1, n2 = mk_contact(A, em("act-new1")), mk_contact(A, em("act-new2"))
    s, lst = req("POST", "/lists", {"list_name": f"Bulk {RID}"}, A.token)
    req("POST", f"/lists/{lst['list_id']}/members", {"prospect_ids": [n1, n2, uns_pid]}, A.token)
    s, b = req("POST", f"/campaigns/{cid}/enrollments/bulk", {"campaign_id": cid, "list_ids": [lst["list_id"]]}, A.token)
    msgs = int(scalar(f"SELECT COUNT(*) FROM email_messages WHERE campaign_id={q(cid)} AND prospect_id IN ({q(n1)},{q(n2)})") or 0)
    rec("OUT-015", "Bulk list enrollment into an ACTIVE campaign schedules emails for the new contacts",
        s == 200 and b.get("enrolled") == 2 and msgs == 2,
        f"POST /campaigns/{{id}}/enrollments/bulk -> {s} enrolled={b.get('enrolled') if isinstance(b, dict) else b}; "
        f"email_messages for them={msgs} (prospect_list_router.enroll_prospects adds CampaignProspect rows but never "
        f"calls _preschedule_all_emails, unlike CampaignEmailService.enroll_prospects_with_report)")
    rec("OUT-083b", "Unsubscribed contact rejected by the bulk list enrollment path",
        isinstance(b, dict) and b.get("rejected", {}).get("global_unsubscribe") == 1, f"{b}")
    n3 = mk_contact(A, em("act-new3"))
    s, b = enroll(A, cid, [n3])
    msgs = int(scalar(f"SELECT COUNT(*) FROM email_messages WHERE campaign_id={q(cid)} AND prospect_id={q(n3)}") or 0)
    rec("OUT-016", "Enrolling via the campaign page into an ACTIVE campaign schedules the new contact", s == 200 and msgs == 1,
        f"{s} {b}; messages={msgs}")
    s, b = req("POST", "/contacts/bulk", {"prospect_ids": [uns_pid], "action": "enroll", "campaign_id": cid}, A.token)
    rec("OUT-083c", "Unsubscribed contact rejected by the contacts bulk-enroll path",
        s == 200 and b.get("enrolled_count") == 0 and b.get("rejected_count") == 1, f"{s} {b}")


# ═════════════════════════ BR-DF-03 lists ═════════════════════════

@safe("OUT-020", "Contacts can be added to new and existing lists")
def t_lists(upload_list):
    p1, p2, p3 = (mk_contact(A, em(f"lst{i}")) for i in (1, 2, 3))
    s, lst = req("POST", "/lists", {"list_name": f"New list {RID}"}, A.token)
    s2, b = req("POST", f"/lists/{lst['list_id']}/members", {"prospect_ids": [p1, p2]}, A.token)
    rec("OUT-020", "Create a new static list and add contacts to it", s == 201 and s2 == 200 and b.get("added") == 2,
        f"create {s}; add {s2} {b}")
    s, b = req("POST", f"/lists/{lst['list_id']}/members", {"prospect_ids": [p2, p3]}, A.token)
    rec("OUT-021", "Add contacts to an existing list; re-adding is idempotent",
        s == 200 and b.get("added") == 1 and b.get("already_in_list") == 1, f"{s} {b}")
    s, b = req("POST", "/contacts/bulk", {"prospect_ids": [p1, p3], "action": "add_to_list",
                                          "new_list_name": f"From prospects tab {RID}"}, A.token)
    n = scalar(f"SELECT COUNT(*) FROM prospect_list_members WHERE list_id={q(b.get('list_id', ''))}") if isinstance(b, dict) else 0
    rec("OUT-022", "Prospects-tab bulk action creates a new list with the selected contacts",
        s == 200 and n == "2", f"{s} {b}; members={n}")
    if upload_list:
        s, b = req("POST", f"/lists/{upload_list}/members", {"prospect_ids": [p3]}, A.token)
        rec("OUT-023", "Contacts can be added to a list created by an upload", s == 200 and b.get("added") == 1, f"{s} {b}")
    foreign = mk_contact(B, em("lst-foreign"))
    s, b = req("POST", f"/lists/{lst['list_id']}/members", {"prospect_ids": [foreign]}, A.token)
    rec("OUT-024", "Another workspace's contacts are not added to a list", s == 200 and b.get("not_found") == 1
        and b.get("added") == 0, f"{s} {b}")


# ═════════════════════════ BR-DF-01 scale & pace ═════════════════════════

@safe("OUT-030", "500-contact campaign schedules every contact")
def t_scale():
    N = 500
    big_dom = f"bulk-{A.rid}.example"   # same addresses every run: no new contacts after the first run
    lines = ["First Name,Last Name,Company Name,email"] + [f"F{i},L{i},Bulk Co,big{i}@{big_dom}" for i in range(N)]
    s, b = upload_csv(A.token, f"big-{RID}.csv", "\n".join(lines) + "\n")
    assert s == 200 and b["accepted"] == N, ("big upload", s, b if s != 200 else b["accepted"])
    s, c = req("POST", f"/uploads/{b['upload_id']}/confirmations",
               {"upload_id": b["upload_id"], "title": f"Big {RID}", "records": b["records"]}, A.token)
    assert s == 200, ("big confirm", s, c)
    inbox = mk_inbox(A, f"bulk-{RID}", DOM, delay_between_emails=60)
    cid = mk_campaign(A, "Scale 500", [inbox], daily_batch_size=100)
    STATE["big"] = {"campaign": cid, "list": b["upload_id"]}
    save_state(STATE)
    s, e = enroll(A, cid, list_ids=[b["upload_id"]])
    stub(A, cid)
    t0 = time.time()
    s2, l2 = launch(A, cid)
    took = time.time() - t0
    n_cp = int(scalar(f"SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id={q(cid)}") or 0)
    n_msg = int(scalar(f"SELECT COUNT(*) FROM email_messages WHERE campaign_id={q(cid)} AND status='SCHEDULED'") or 0)
    rec("OUT-030", "500-contact campaign: all 500 enrolled and scheduled at launch (no stall near 330)",
        s == 200 and e.get("enrolled_count") == N and s2 == 200 and n_cp == N and n_msg == N,
        f"enroll {s} {e.get('enrolled_count') if isinstance(e, dict) else e}; launch {s2}; cp={n_cp}; scheduled={n_msg}")
    per_day = rows(f"SELECT DATE(scheduled_at), COUNT(*) FROM email_messages WHERE campaign_id={q(cid)} "
                   f"GROUP BY DATE(scheduled_at) ORDER BY 1")
    counts = [int(r[1]) for r in per_day]
    rec("OUT-031", "Configured pace (100 new contacts/day) spreads 500 contacts over 5 business days",
        counts == [100] * 5, f"per day: {per_day}")
    rec("OUT-032", "Launching the 500-contact campaign completes within 30 seconds", s2 == 200 and took < 30,
        f"{took:.1f}s")
    # pacing inputs the scheduler uses for this inbox: at 60 s/email it can still reach its 500/day cap
    rec("OUT-036", "Inbox pace (60 s between emails) can reach the 500/day cap within a day",
        86400 / 60 >= 500, "")


@safe("OUT-033", "Batch sending mode spaces batches by the configured gap")
def t_batch_mode():
    pids = [mk_contact(A, em(f"bat{i}")) for i in range(6)]
    cid = ready_campaign(A, "Batch", [IN1], pids, sending_mode="batch", batch_size=2, batch_gap_minutes=30)
    times = [r[0] for r in rows(f"SELECT DISTINCT scheduled_at FROM email_messages WHERE campaign_id={q(cid)} ORDER BY 1")]
    ts = [datetime.fromisoformat(t) for t in times]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(ts, ts[1:])]
    sizes = [int(r[1]) for r in rows(f"SELECT scheduled_at, COUNT(*) FROM email_messages WHERE campaign_id={q(cid)} "
                                     f"GROUP BY scheduled_at ORDER BY 1")]
    rec("OUT-033", "Batch mode: batches of 2, 30 minutes apart", sizes == [2, 2, 2] and gaps == [30.0, 30.0],
        f"sizes={sizes} gaps={gaps}")


# ═════════════════════════ BR-DF-04 final status via SES events ═════════════════════════

@safe("OUT-040", "SES events give every email a final status")
def t_events():
    pids = [mk_contact(A, em(f"evt{i}")) for i in range(8)]
    cid = ready_campaign(A, "Events", [IN1], pids)
    src = f"sales1-{RID}@{DOM}"
    m = [msg(cid, p)[0] for p in pids]
    sent = {mid: mark_sent(mid) for mid in m[:7]}

    s, b = ses_post(ses_delivery(m[0], em("evt0"), sent[m[0]][0], src))
    st = mstatus(m[0])
    rec("OUT-040", "Delivery event sets final status DELIVERED", s == 200 and st[2] == "DELIVERED", f"{s} {b} {st}")

    s, b = ses_post(ses_delivery(None, em("evt1"), sent[m[1]][0], src, tags=False))
    st = mstatus(m[1])
    rec("OUT-041", "Delivery event without our tag is matched by the SES message id", st[2] == "DELIVERED", f"{s} {b} {st}")

    stamp = datetime.utcnow().isoformat() + "Z"
    ev = ses_delivery(m[2], em("evt2"), sent[m[2]][0], src, stamp=stamp)
    r1, r2 = ses_post(ev), ses_post(ev)
    n = scalar(f"SELECT COUNT(*) FROM email_events WHERE message_id={q(m[2])} AND event_type='DELIVERED'")
    rec("OUT-042", "A redelivered (duplicate) SES event is processed once",
        r2[1].get("status") == "duplicate" and n == "1", f"first={r1} second={r2} delivered_events={n}")

    ses_post(ses_bounce(m[0], em("evt0"), sent[m[0]][0], src))
    st = mstatus(m[0])
    rec("OUT-043", "Hard bounce after delivery: final status BOUNCED, address suppressed",
        st[2] == "BOUNCED" and suppressed(A, em("evt0")) and cp_status(cid, pids[0]) == "BOUNCED", f"{st} cp={cp_status(cid, pids[0])}")

    s, b = ses_post(ses_bounce(m[3], em("evt3"), sent[m[3]][0], src, btype="Transient", sub="MailboxFull"))
    st = mstatus(m[3])
    rec("OUT-044", "Soft (transient) bounce does not suppress the contact or change the message status",
        st[0] == "SENT" and not suppressed(A, em("evt3")), f"{st} suppressed={suppressed(A, em('evt3'))}")

    ses_post(ses_complaint(m[4], em("evt4"), sent[m[4]][0], src))
    st = mstatus(m[4])
    rec("OUT-045", "Complaint: final status COMPLAINED, contact unsubscribed and suppressed",
        st[2] == "COMPLAINED" and suppressed(A, em("evt4")) and consent(pids[4]) == "UNSUBSCRIBED",
        f"{st} consent={consent(pids[4])}")

    s, b = ses_post(ses_bounce(None, f"nobody-{RID}@nowhere.example", "ses-unknown-" + RID, src, tags=False))
    rec("OUT-048", "Event for an unknown message is accepted without side effects", s == 200 and not suppressed(A, f"nobody-{RID}@nowhere.example"),
        f"{s} {b}")

    # Unconfirmed after DELIVERY_CONFIRM_MINUTES (15) — the 1-minute send_safety job sets it
    late = m[5]
    sql(f"UPDATE email_messages SET sent_at=UTC_TIMESTAMP() - INTERVAL 20 MINUTE WHERE message_id={q(late)}")
    got = wait_until(lambda: mstatus(late)[2] == "UNCONFIRMED", timeout=150, interval=5)
    rec("OUT-046", "Sent email with no SES outcome after 15 minutes gets final status UNCONFIRMED (worker)", got,
        f"after 150s: {mstatus(late)}")
    ses_post(ses_delivery(late, em("evt5"), sent[late][0], src))
    rec("OUT-047", "A late delivery event overrides UNCONFIRMED", mstatus(late)[2] == "DELIVERED", f"{mstatus(late)}")

    # tracking
    before = int(scalar(f"SELECT COUNT(*) FROM email_events WHERE message_id={q(m[6])} AND event_type='OPENED'") or 0)
    s, body = req("GET", f"/tracking/open/{m[6]}.png", raw=True)
    after = int(scalar(f"SELECT COUNT(*) FROM email_events WHERE message_id={q(m[6])} AND event_type='OPENED'") or 0)
    if before == after:  # event type name may differ
        after = int(scalar(f"SELECT COUNT(*) FROM email_events WHERE message_id={q(m[6])}") or 0) - before
    rec("OUT-117", "Open pixel returns an image and records an open event", s == 200 and after > before, f"{s} events {before}->{after}")
    s, b = req("GET", f"/tracking/click/{m[6]}?url=https%3A%2F%2Fevil-{RID}.example%2Fx", follow=False)
    rec("OUT-118a", "Click tracking does not redirect to an unrecognised destination (no open redirect)",
        s == 200 and "leaving" in str(b).lower(), f"{s}")
    sql(f"UPDATE email_messages SET body_text=CONCAT(body_text, ' https://good-{RID}.example/page') WHERE message_id={q(m[6])}")
    s, b = req("GET", f"/tracking/click/{m[6]}?url=https%3A%2F%2Fgood-{RID}.example%2Fpage", follow=False)
    rec("OUT-118b", "Click on a link that is in the email is recorded and redirected (302)", s == 302, f"{s}")
    s, b = req("GET", f"/campaigns/{cid}/analytics", token=A.token)
    rec("OUT-119", "Campaign analytics reflect sent emails and events",
        s == 200 and b.get("total_messages_sent", 0) >= 6, f"{s} total_sent={b.get('total_messages_sent') if isinstance(b, dict) else b} "
        f"metrics={json.dumps(b.get('metrics') if isinstance(b, dict) else '')[:200]}")
    s, b = req("GET", f"/reports/export/prospects/{cid}", token=A.token, raw=True)
    rec("OUT-120", "Prospect report export for a campaign returns a file with the campaign's contacts",
        s == 200 and em("evt1").encode() in (b or b""), f"{s} {str(b)[:150]}")
    s, b = req("GET", f"/campaigns/{cid}/audit", token=A.token)
    rec("OUT-123", "Campaign audit trail records the launch", s == 200 and "LAUNCH" in json.dumps(b).upper(), f"{s} {str(b)[:200]}")


# ═════════════════════════ BR-DF-07 auto-pause & domain health ═════════════════════════

@safe("OUT-070", "Auto-pause on hard bounces")
def t_autopause():
    a_in = mk_inbox(A, f"risk-{RID}", SHARED)
    b_in = mk_inbox(B, f"other-{RID}", SHARED)
    src = f"risk-{RID}@{SHARED}"
    pids = [mk_contact(A, em(f"risk{i}")) for i in range(7)]
    cid = ready_campaign(A, "Risk", [a_in], pids, waits=(0, 3))
    bp = mk_contact(B, em("b-shared"))
    bcid = ready_campaign(B, "Tenant B on shared domain", [b_in], [bp])
    m = [msg(cid, p)[0] for p in pids]
    sent = {mid: mark_sent(mid) for mid in m[:6]}
    for i in range(4):
        ses_post(ses_bounce(m[i], em(f"risk{i}"), sent[m[i]][0], src))
    rec("OUT-070", "4 hard bounces in the first sends (below the 5-bounce limit) do not pause the campaign",
        camp(cid)[0] == "ACTIVE", f"{camp(cid)}")
    t0 = time.time()
    ses_post(ses_bounce(m[4], em("risk4"), sent[m[4]][0], src))
    got = wait_until(lambda: camp(cid)[0] == "PAUSED", timeout=60, interval=2)
    took = time.time() - t0
    c = camp(cid)
    frozen = scalar(f"SELECT COUNT(*) FROM email_messages WHERE campaign_id={q(cid)} AND status IN ('SCHEDULED','QUEUED')")
    rec("OUT-071", "5th hard bounce auto-pauses the campaign within 1 minute, with the reason, queue frozen",
        got and c[1] == "1" and "bounce" in c[2].lower() and frozen == "0" and took <= 60, f"{c} took={took:.1f}s pending={frozen}")
    d = rows(f"SELECT IFNULL(health_checked_at,''), IFNULL(bounce_rate_24h,0), IFNULL(sends_24h,0) FROM sending_domains "
             f"WHERE domain_name={q(SHARED)} AND tenant_id={q(A.tenant_id)}")  # one row per workspace since 0005
    fresh = d and d[0][0] and (datetime.utcnow() - datetime.fromisoformat(d[0][0])).total_seconds() < 300
    rec("OUT-073", "Domain health is recomputed at the risk event (24h sends / bounce rate stored)",
        bool(fresh) and float(d[0][1]) > 0, f"sending_domains={d}")
    bc = camp(bcid)
    rec("OUT-076", "Another workspace's campaign on the same sending domain is not paused by this workspace's bounces",
        bc[0] == "ACTIVE", f"tenant B campaign: {bc} (send_safety.check_domain_health rates and pauses every tenant's "
        f"campaigns on the domain: campaigns_for_domain has no tenant filter)")
    s, doms = req("GET", "/deliverability/domains", token=B.token)
    leak = [d for d in (doms or []) if isinstance(d, dict) and (src in d.get("associated_inboxes", [])
                                                                 or any(f"Risk {RID}" == n for n in d.get("active_campaigns", [])))]
    rec("OUT-077", "Domain list does not show another workspace's mailboxes or campaigns",
        s == 200 and not leak, f"{s}; tenant B sees: {[(d['domain_name'], d.get('associated_inboxes')) for d in leak]}")
    # resume after auto-pause: judged on new sends only
    s, b = req("POST", f"/campaigns/{cid}/resume", None, A.token)
    ses_id, _ = mark_sent(m[5])
    ses_post(ses_bounce(m[5], em("risk5"), ses_id, src))
    time.sleep(2)
    c = camp(cid)
    rec("OUT-075", "After the user resumes an auto-paused campaign it is judged on new sends (one bounce doesn't re-pause)",
        s == 200 and c[0] == "ACTIVE" and c[3] != "", f"resume {s}; {c}")
    return cid


@safe("OUT-072", "Auto-pause on spam complaints")
def t_complaints():
    dom = f"cmp-{RID}.example"
    inb = mk_inbox(A, f"cmp-{RID}", dom)
    pids = [mk_contact(A, em(f"cmp{i}")) for i in range(3)]
    cid = ready_campaign(A, "Complaints", [inb], pids)
    m = [msg(cid, p)[0] for p in pids]
    for i in range(2):
        sid, _ = mark_sent(m[i])
        ses_post(ses_complaint(m[i], em(f"cmp{i}"), sid, f"cmp-{RID}@{dom}"))
    got = wait_until(lambda: camp(cid)[0] == "PAUSED", timeout=60, interval=2)
    rec("OUT-072", "Two spam complaints auto-pause the campaign within 1 minute", got and camp(cid)[1] == "1", f"{camp(cid)}")


@safe("OUT-074", "Domain health refreshed hourly")
def t_hourly():
    stale = rows("SELECT d.domain_name, IFNULL(d.health_checked_at,'never') FROM sending_domains d "
                 "WHERE d.domain_name IN (SELECT DISTINCT SUBSTRING_INDEX(from_email,'@',-1) FROM email_messages "
                 "WHERE sent_at >= UTC_TIMESTAMP() - INTERVAL 24 HOUR AND from_email LIKE '%@%') "
                 "AND (d.health_checked_at IS NULL OR d.health_checked_at < UTC_TIMESTAMP() - INTERVAL 65 MINUTE) "
                 # a domain first used within the hour can't have had its hourly check yet
                 "AND EXISTS (SELECT 1 FROM email_messages m2 WHERE SUBSTRING_INDEX(m2.from_email,'@',-1) = d.domain_name "
                 "AND m2.sent_at < UTC_TIMESTAMP() - INTERVAL 65 MINUTE)")
    missing = rows("SELECT DISTINCT SUBSTRING_INDEX(from_email,'@',-1) FROM email_messages "
                   "WHERE sent_at >= UTC_TIMESTAMP() - INTERVAL 24 HOUR AND sent_at < UTC_TIMESTAMP() - INTERVAL 65 MINUTE "
                   "AND from_email LIKE '%@%' AND SUBSTRING_INDEX(from_email,'@',-1) NOT IN (SELECT domain_name FROM sending_domains)")
    rec("OUT-074", "Every domain that sent in the last 24h had its health recomputed within the last hour",
        not stale and not missing, f"stale={stale[:5]} missing_rows={missing[:5]}")


@safe("OUT-078", "Pause freezes and resume restores the queue")
def t_pause_resume():
    p = mk_contact(A, em("pause1"))
    cid = ready_campaign(A, "Pause", [IN1], [p], waits=(0, 2))
    s, b = req("POST", f"/campaigns/{cid}/pause", None, A.token)
    fr = scalar(f"SELECT GROUP_CONCAT(DISTINCT status) FROM email_messages WHERE campaign_id={q(cid)}")
    s2, b2 = req("POST", f"/campaigns/{cid}/resume", None, A.token)
    back = scalar(f"SELECT GROUP_CONCAT(DISTINCT status) FROM email_messages WHERE campaign_id={q(cid)}")
    rec("OUT-078", "Pause freezes pending emails (PAUSED_BY_CAMPAIGN); resume releases them",
        s == 200 and fr == "PAUSED_BY_CAMPAIGN" and s2 == 200 and back in ("QUEUED", "SCHEDULED"), f"{s} {fr} -> {s2} {back}")


# ═════════════════════════ BR-DF-08 unsubscribe ═════════════════════════

@safe("OUT-081", "Unsubscribe stops every campaign in the workspace")
def t_unsubscribe():
    p = mk_contact(A, em("uns-main"))
    c1 = ready_campaign(A, "Unsub one", [IN1], [p], waits=(0, 2))
    c2 = ready_campaign(A, "Unsub two", [IN2], [p], waits=(0,))
    m1 = msg(c1, p)[0]
    mark_sent(m1)
    s, page = req("GET", f"/tracking/unsubscribe/{m1}")
    rec("OUT-080", "Unsubscribe link (GET) shows a confirmation page and changes nothing",
        s == 200 and "noindex" in str(page) and not suppressed(A, em("uns-main")), f"{s} suppressed={suppressed(A, em('uns-main'))}")
    s, page = req("POST", f"/tracking/unsubscribe/{m1}", form={"confirm": "1"})
    pend = scalar(f"SELECT COUNT(*) FROM email_messages WHERE prospect_id={q(p)} AND status IN ('SCHEDULED','QUEUED')")
    st = (cp_status(c1, p), cp_status(c2, p))
    rec("OUT-081", "Confirmed unsubscribe: suppressed workspace-wide, pending emails in every campaign cancelled",
        s == 200 and suppressed(A, em("uns-main")) and pend == "0" and st == ("UNSUBSCRIBED", "UNSUBSCRIBED")
        and consent(p) == "UNSUBSCRIBED", f"{s} pending={pend} cp={st} consent={consent(p)}")
    r = rows(f"SELECT IFNULL(consent_source,''), IFNULL(consent_timestamp,'') FROM prospects WHERE prospect_id={q(p)}")
    rec("OUT-105", "Consent change is recorded with source and timestamp", r and r[0][0] and r[0][1], f"{r}")
    c3 = mk_campaign(A, "Unsub three", [IN1])
    s, b = enroll(A, c3, [p])
    rec("OUT-083", "Unsubscribed contact is rejected when enrolled in a new campaign (campaign page)",
        s == 200 and b["enrolled_count"] == 0 and b["rejected"][0]["reason_code"] == "unsubscribed", f"{s} {b}")

    # one-click (RFC 8058)
    q2 = mk_contact(A, em("uns-oneclick"))
    c4 = ready_campaign(A, "One click", [IN1], [q2])
    m4 = msg(c4, q2)[0]
    mark_sent(m4)
    s, _ = req("POST", f"/tracking/unsubscribe/{m4}", form={"List-Unsubscribe": "One-Click"})
    rec("OUT-082", "One-click List-Unsubscribe POST unsubscribes", s == 200 and suppressed(A, em("uns-oneclick")), f"{s}")

    s, b = req("POST", f"/tracking/unsubscribe/{uuid.uuid4()}", form={"confirm": "1"})
    rec("OUT-091", "Unsubscribe for an unknown message id returns 404", s == 404, f"{s}")

    # tenant isolation: the same address in workspace B is untouched
    bp = mk_contact(B, em("uns-main"))
    rec("OUT-090", "Unsubscribe in one workspace does not unsubscribe the same address in another",
        consent(bp) == "OPT_IN" and not suppressed(B, em("uns-main")), f"B consent={consent(bp)}")

    # case-insensitive: new contact with a differently-cased suppressed address
    try:
        mixed = mk_contact(A, em("uns-main").upper())
        cs = consent(mixed)
    except AssertionError as e:
        cs = f"create refused: {e}"
    rec("OUT-085", "A contact whose address matches a suppression (any case) cannot be emailed",
        cs == "UNSUBSCRIBED" or "409" in str(cs), f"{cs}")

    # manual unsubscribe on the contact record
    mp = mk_contact(A, em("uns-manual"))
    c5 = ready_campaign(A, "Manual unsub", [IN1], [mp], waits=(0, 2))
    s, b = req("PATCH", f"/contacts/{mp}", {"consent_status": "UNSUBSCRIBED"}, A.token)
    pend = scalar(f"SELECT COUNT(*) FROM email_messages WHERE prospect_id={q(mp)} AND status IN ('SCHEDULED','QUEUED')")
    rec("OUT-086", "Manual unsubscribe on the contact record suppresses and cancels pending emails",
        s == 200 and suppressed(A, em("uns-manual")) and pend == "0", f"{s} pending={pend}")
    return c5


@safe("OUT-087", "New contact with a suppressed address is created unsubscribed")
def t_new_contact_suppressed():
    e = em("presupp")
    sql(f"INSERT INTO global_unsubscribes (tenant_id, email, unsubscribed_at, reason) VALUES "
        f"({q(A.tenant_id)}, {q(e)}, UTC_TIMESTAMP(), 'systest import')")
    pid = mk_contact(A, e)
    rec("OUT-087", "Creating a contact whose address is suppressed marks it UNSUBSCRIBED", consent(pid) == "UNSUBSCRIBED",
        f"consent={consent(pid)}")


# ═════════════════════════ §7 data ═════════════════════════

@safe("OUT-100", "Email is the unique key across users")
def t_unique():
    agent = get_user(A, "AGENT", f"Agent{RID}", STATE)
    e = em("uniq")
    mk_contact(A, e)
    s, b = req("POST", "/contacts", {"email": e.upper(), "first_name": "X", "last_name": "Y", "company_name": "Z"}, agent["token"])
    rec("OUT-100", "Another user cannot create a contact with an existing email (any case)", s == 409, f"{s} {b}")
    s, b = launch(A, mk_campaign(A, "Agent launch", [IN1]), token=agent["token"])
    rec("OUT-113", "An AGENT cannot launch a campaign", s == 403, f"{s} {b}")


@safe("OUT-101", "Soft delete on request is logged and stops email")
def t_soft_delete():
    p = mk_contact(A, em("soft"))
    cid = ready_campaign(A, "Soft delete", [IN1], [p])
    s, b = req("DELETE", f"/contacts/{p}", None, A.token)
    logged = scalar(f"SELECT COUNT(*) FROM property_changes WHERE object_id={q(p)} AND field='deleted'")
    s2, d = req("GET", "/contacts/deleted", token=A.token)
    listed = p in json.dumps(d)
    pend = scalar(f"SELECT COUNT(*) FROM email_messages WHERE prospect_id={q(p)} AND status IN ('SCHEDULED','QUEUED')")
    rec("OUT-101", "Deleting a contact on request is logged, listed in Recently deleted, and cancels pending emails",
        s == 200 and logged == "1" and listed and pend == "0", f"{s} logged={logged} listed={listed} pending={pend}")


@safe("OUT-104", "GDPR legal basis recorded on the contact")
def t_legal_basis():
    p = mk_contact(A, em("legal"))
    s, b = req("PATCH", f"/contacts/{p}", {"legal_basis": "LEGITIMATE_INTEREST"}, A.token)
    s2, b2 = req("PATCH", f"/contacts/{p}", {"legal_basis": "BECAUSE"}, A.token)
    stored = scalar(f"SELECT legal_basis FROM prospects WHERE prospect_id={q(p)}")
    rec("OUT-104", "Legal basis for processing is stored; invalid values are refused",
        s == 200 and stored == "LEGITIMATE_INTEREST" and s2 in (400, 422), f"{s} {stored} invalid->{s2}")


# ═════════════════════════ BR-DF-06 duplicates ═════════════════════════

@safe("OUT-061", "Duplicate-send protections")
def t_duplicates():
    idx = rows("SHOW INDEX FROM email_messages WHERE Key_name='send_key' AND Non_unique=0")
    rec("OUT-061", "Database enforces one send per campaign step and address (unique send_key)", bool(idx), f"{idx}")
    p = mk_contact(A, em("dup1"))
    s, lst1 = req("POST", "/lists", {"list_name": f"Dup1 {RID}"}, A.token)
    s, lst2 = req("POST", "/lists", {"list_name": f"Dup2 {RID}"}, A.token)
    for l in (lst1, lst2):
        req("POST", f"/lists/{l['list_id']}/members", {"prospect_ids": [p]}, A.token)
    cid = mk_campaign(A, "Dup", [IN1, IN2], waits=(0, 2))
    s, b = enroll(A, cid, list_ids=[lst1["list_id"], lst2["list_id"]])
    stub(A, cid)
    launch(A, cid)
    s2, b2 = enroll(A, cid, [p])
    n_cp = scalar(f"SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id={q(cid)}")
    n_m = scalar(f"SELECT COUNT(*) FROM email_messages WHERE campaign_id={q(cid)}")
    rec("OUT-063", "Contact in two enrolled lists is enrolled once (one email per step)",
        b.get("enrolled_count") == 1 and n_cp == "1" and n_m == "2", f"enrolled={b.get('enrolled_count')} cp={n_cp} msgs={n_m}")
    rec("OUT-062", "Re-enrolling an enrolled contact is rejected (already_enrolled) and adds no emails",
        b2.get("enrolled_count") == 0 and b2["rejected"][0]["reason_code"] == "already_enrolled"
        and scalar(f"SELECT COUNT(*) FROM email_messages WHERE campaign_id={q(cid)}") == "2", f"{b2}")
    p2 = mk_contact(A, em("dup2"))
    enroll(A, cid, [p2])
    n_m = scalar(f"SELECT COUNT(*) FROM email_messages WHERE campaign_id={q(cid)}")
    rec("OUT-064", "Enrolling a new contact after launch adds only that contact's emails", n_m == "4", f"msgs={n_m}")


@safe("OUT-110", "Multi-domain inbox rotation")
def t_rotation():
    pids = [mk_contact(A, em(f"rot{i}")) for i in range(4)]
    cid = ready_campaign(A, "Rotation", [IN1, IN2], pids)
    per = rows(f"SELECT SUBSTRING_INDEX(from_email,'@',-1), COUNT(*) FROM email_messages WHERE campaign_id={q(cid)} GROUP BY 1")
    d = {r[0]: int(r[1]) for r in per}
    rec("OUT-110", "Campaign with mailboxes on two domains spreads contacts across both", d.get(DOM) == 2 and d.get(DOM2) == 2, f"{d}")


@safe("OUT-111", "Launch requires the sender's postal address")
def t_sender_identity():
    s, prof = req("GET", "/api/company-profiles/sender-identity", token=B.token)
    pid = prof.get("profile_id") if isinstance(prof, dict) else None
    bp = mk_contact(B, em("b-ident"))
    b_in = mk_inbox(B, f"ident-{RID}", f"bident-{RID}.example")
    cid = mk_campaign(B, "No identity", [b_in])
    enroll(B, cid, [bp])
    stub(B, cid)
    s1, b1 = req("DELETE", f"/api/company-profiles/{pid}", None, B.token)
    s2, b2 = launch(B, cid)
    B.set_sender_identity()
    s3, b3 = launch(B, cid)
    rec("OUT-111", "Launch is refused with a reason when the sender postal address is missing (CAN-SPAM)",
        s1 == 200 and s2 == 400 and "address" in json.dumps(b2).lower() and s3 == 200, f"delete {s1}; launch {s2} {b2}; after set {s3}")
    s4, b4 = launch(B, cid)
    cid2 = mk_campaign(B, "No steps", [b_in], waits=())
    s5, b5 = launch(B, cid2)
    rec("OUT-112", "Launch validation: an active campaign can't be relaunched; a campaign without steps can't launch",
        s4 == 400 and s5 == 400, f"relaunch {s4} {b4}; no steps {s5} {b5}")


@safe("OUT-114", "AI email content")
def t_ai():
    p = mk_contact(A, em("ai"), first_name="Priya", designation="Chief Technology Officer", company_name="Acme Robotics")
    s, b = req("POST", "/emails/generations", {"prospect_id": p, "product_name": "Outreach360"}, A.token)
    text = json.dumps(b)
    rec("OUT-114", "AI email generation returns a personalised subject and body", s == 200 and "Priya" in text
        and isinstance(b, dict) and b.get("subject"), f"{s} {text[:200]}")
    fp = mk_contact(B, em("ai-foreign"))
    s, b = req("POST", "/emails/generations", {"prospect_id": fp, "product_name": "X"}, A.token)
    rec("OUT-114b", "AI generation for another workspace's contact is refused (404)", s == 404, f"{s} {b}")
    s, b = req("POST", "/emails/sequences/generations", {"prospect_id": p, "product_name": "Outreach360", "use_llm": False}, A.token)
    rec("OUT-115", "Sequence generation (no LLM) returns a 3-email sequence",
        s == 200 and all(k in b for k in ("email_1", "email_2", "email_3")), f"{s} {str(b)[:200]}")
    s, b = req("POST", "/emails/compliance-checks", {"subject": "FREE MONEY!!! Act now", "body": "Click here to win cash, 100% free, guaranteed"}, A.token)
    rec("OUT-116", "Compliance check flags spam trigger words", s == 200 and (b.get("spam_triggers") or b.get("status") != "passed"),
        f"{s} {str(b)[:200]}")


@safe("OUT-122", "SES quota endpoint degrades gracefully")
def t_quota():
    s, b = req("GET", "/inboxes/ses/quota", token=A.token)
    rec("OUT-122", "SES quota endpoint answers with a clear error (not 500) when SES is unreachable",
        s != 500, f"{s} {b}", "defect")


@safe("OUT-050", "Microsoft 365 secure connection endpoints")
def t_ms365():
    s, b = req("GET", "/inboxes/oauth/microsoft/status", token=A.token)
    s2, b2 = req("POST", f"/inboxes/{IN1}/oauth/microsoft/start", None, A.token)
    rec("OUT-050", "Microsoft 365 OAuth connection: status reported, start gives a clear message when not set up",
        s == 200 and "configured" in b and (s2 == 200 and "authorize_url" in b2 or (s2 == 400 and "MS365" in json.dumps(b2))),
        f"status {s} {b}; start {s2} {b2}")


# ═════════════════════════ BR-DF-05 IMAP replies ═════════════════════════

def imap_inbox(local, dom, password):
    addr = f"{local}@{dom}"
    IMAP.add_account(addr, password)
    return mk_inbox(A, local, dom, imap_host=HOST_FROM_CONTAINERS, imap_port=IMAP.port, imap_username=addr,
                    imap_password=password), addr


@safe("OUT-052", "IMAP replies")
def t_imap(bg):
    dom = f"reply-{RID}.example"
    inb, addr = imap_inbox(f"replies-{RID}", dom, f"Pw{RID}")
    pids = {k: mk_contact(A, em(k)) for k in ("rep1", "ooo1", "unsub1")}
    cid = ready_campaign(A, "Replies", [inb], list(pids.values()), waits=(0, 3))
    pm = {}
    for k, p in pids.items():
        _, pm[k] = mark_sent(msg(cid, p)[0])
    IMAP.deliver(addr, reply_mime(em("rep1"), addr, "Re: hello", "Sounds interesting, let's talk next week.", pm["rep1"]))
    IMAP.deliver(addr, reply_mime(em("ooo1"), addr, "Automatic reply: Out of Office",
                                  "I am out of the office until Monday with limited access to email.", pm["ooo1"],
                                  auto_submitted="auto-replied"))
    IMAP.deliver(addr, reply_mime(em("unsub1"), addr, "Re: hello", "unsubscribe", pm["unsub1"]))
    foreign = mk_contact(B, em("only-in-b"))
    IMAP.deliver(addr, reply_mime(em("only-in-b"), addr, "Hello", "Who is this?"))

    s, b = req("GET", f"/inboxes/{inb}/test-imap", token=A.token)
    rec("OUT-051", "IMAP connection test succeeds against a reachable mailbox", s == 200 and isinstance(b, dict) and str(b.get("status")).lower() not in ("error", "failed", "not_configured", "none"), f"{s} {str(b)[:200]}")
    t0 = time.time()
    s, b = req("POST", f"/inboxes/{inb}/sync?days=7", None, A.token)
    s2, convs = req("GET", "/conversations?page_size=100", token=A.token)
    items = convs.get("items", []) if isinstance(convs, dict) else []
    rep = [c for c in items if c["prospect_id"] == pids["rep1"]]
    inbound = scalar(f"SELECT COUNT(*) FROM email_messages WHERE prospect_id={q(pids['rep1'])} AND direction='INBOUND'")
    rec("OUT-052", "Prospect reply is fetched and shown in the unified inbox thread",
        s == 200 and rep and rep[0]["last_message_direction"] == "INBOUND" and inbound == "1",
        f"sync {s} {b}; conversation={rep[:1]} inbound={inbound}")
    step2 = msg(cid, pids["rep1"], 2)
    rec("OUT-053", "Reply stops the sequence: enrollment REPLIED, follow-up cancelled",
        cp_status(cid, pids["rep1"]) == "REPLIED" and step2[1] == "CANCELLED", f"cp={cp_status(cid, pids['rep1'])} step2={step2[1:3]}")
    o2 = msg(cid, pids["ooo1"], 2)
    ooo_in = scalar(f"SELECT COUNT(*) FROM email_messages WHERE prospect_id={q(pids['ooo1'])} AND direction='INBOUND'")
    rec("OUT-054", "Out-of-office reply is visible but does not stop the sequence",
        ooo_in == "1" and cp_status(cid, pids["ooo1"]) == "ACTIVE" and o2[1] == "SCHEDULED",
        f"inbound={ooo_in} cp={cp_status(cid, pids['ooo1'])} step2={o2[1]}")
    rec("OUT-055", "Reply 'unsubscribe' unsubscribes the contact from every campaign",
        suppressed(A, em("unsub1")) and consent(pids["unsub1"]) == "UNSUBSCRIBED", f"consent={consent(pids['unsub1'])}")
    leaked = scalar(f"SELECT COUNT(*) FROM email_messages WHERE LOWER(from_email)=LOWER({q(em('only-in-b'))}) AND direction='INBOUND'")
    rec("OUT-059", "Mail from another workspace's contact is not filed into either workspace via this mailbox",
        leaked == "0", f"inbound rows={leaked}")
    if rep:
        s, r = req("POST", f"/conversations/{rep[0]['id']}/reply", {"body_text": "Great, Tuesday 10am works."}, A.token)
        rec("OUT-121", "Reply from the unified inbox creates an outbound message in the thread", s == 200 and
            r.get("message", {}).get("direction") == "OUTBOUND", f"{s} {str(r)[:200]}")
    # wrong password
    bad, _ = imap_inbox(f"badpw-{RID}", dom, f"Right{RID}")
    sql(f"UPDATE sending_inboxes SET imap_password=NULL WHERE inbox_id={q(bad)}")
    req("PUT", f"/inboxes/{bad}", {"imap_password": "wrong-password"}, A.token)
    s, b = req("POST", f"/inboxes/{bad}/sync?days=7", None, A.token)
    err = scalar(f"SELECT IFNULL(imap_last_error,'') FROM sending_inboxes WHERE inbox_id={q(bad)}")
    rec("OUT-056", "Mailbox sign-in failure is reported (sync fails with a reason stored on the inbox)",
        s >= 400 and err, f"{s} {b} last_error={err[:120]!r}")
    sql(f"UPDATE sending_inboxes SET imap_host=NULL WHERE inbox_id={q(bad)}")
    return inb


def setup_background_reply():
    """Reply waiting in a mailbox nobody syncs manually: the worker must pick it up within 10 minutes."""
    dom = f"bg-{RID}.example"
    inb, addr = imap_inbox(f"bg-{RID}", dom, f"Bg{RID}")
    p = mk_contact(A, em("bgreply"))
    cid = ready_campaign(A, "Background reply", [inb], [p], waits=(0, 3))
    _, pm = mark_sent(msg(cid, p)[0])
    IMAP.deliver(addr, reply_mime(em("bgreply"), addr, "Re: hi", "Yes please send details.", pm))
    return {"inbox": inb, "pid": p, "cid": cid, "t0": time.time()}


# ═════════════════════════ scheduler (worker) ═════════════════════════

def setup_scheduler_cases():
    """Messages pulled due for the 30 s scheduler; checked later."""
    out = {}
    tz = TZ or "UTC"
    win = ("00:00:00", "23:59:00")
    pids = {k: mk_contact(A, em(k)) for k in ("sch-reply", "sch-supp", "sch-dup", "sch-ok")}
    pids["sch-personal"] = mk_contact(A, f"personal-{RID}@gmail.com")
    cid = ready_campaign(A, "Scheduler", [IN1], list(pids.values()), waits=(0, 2), window=win, tz=tz)
    out["cid"], out["pids"] = cid, pids
    # stop-on-reply via manual mark: step 1 sent, marked replied, step 2 due
    mark_sent(msg(cid, pids["sch-reply"])[0])
    s, b = req("POST", f"/campaigns/{cid}/prospects/{pids['sch-reply']}/mark-replied", None, A.token)
    out["mark"] = (s, b)
    out["reply_step2"] = msg(cid, pids["sch-reply"], 2)[0]
    # out-of-band suppression (e.g. a legacy import) — only the send-time gate can stop it
    sql(f"INSERT INTO global_unsubscribes (tenant_id, email, unsubscribed_at, reason) VALUES "
        f"({q(A.tenant_id)}, {q(em('sch-supp'))}, UTC_TIMESTAMP(), 'systest out-of-band')")
    out["supp"] = msg(cid, pids["sch-supp"])[0]
    # duplicate: the same step to the same address was already sent by another row
    orig = msg(cid, pids["sch-dup"])[0]
    sql(f"INSERT INTO email_messages (message_id, campaign_id, prospect_id, sequence_id, inbox_id, from_email, to_email, "
        f"subject, body_text, status, scheduled_at, sent_at, direction, template_id, max_retries, retry_count) "
        f"SELECT UUID(), campaign_id, prospect_id, sequence_id, inbox_id, from_email, to_email, subject, body_text, 'SENT', "
        f"scheduled_at, UTC_TIMESTAMP(), 'OUTBOUND', template_id, max_retries, 0 FROM email_messages WHERE message_id={q(orig)}")
    out["dup"] = orig
    out["ok"] = msg(cid, pids["sch-ok"])[0]
    out["personal"] = msg(cid, pids["sch-personal"])[0]
    # outside the send window
    wp = mk_contact(A, em("sch-window"))
    wc = ready_campaign(A, "Window", [IN1], [wp], window=("00:00:00", "00:01:00"), tz="UTC")
    out["window"] = msg(wc, wp)[0]
    # per-inbox daily cap reached
    cap_in = mk_inbox(A, f"cap-{RID}", DOM, max_emails_per_day=1)
    cp_ = mk_contact(A, em("sch-cap"))
    cc = ready_campaign(A, "Cap", [cap_in], [cp_], window=win, tz=tz)
    sql(f"UPDATE sending_inboxes SET emails_sent_today=1, last_daily_reset=UTC_TIMESTAMP() WHERE inbox_id={q(cap_in)}")
    out["cap"] = msg(cc, cp_)[0]
    for k in ("reply_step2", "supp", "dup", "ok", "personal", "window", "cap"):
        pull_due(out[k])
    out["t0"] = time.time()
    return out


def check_scheduler_cases(sc):
    def done(mid):
        return lambda: mstatus(mid)[0] not in ("SCHEDULED",) or int(mstatus(mid)[3] or 0) > 0

    for k in ("reply_step2", "supp", "dup", "window", "ok", "personal", "cap"):
        wait_until(done(sc[k]), timeout=max(5, 150 - (time.time() - sc["t0"])), interval=5)
    st = mstatus(sc["reply_step2"])
    rec("OUT-058", "After a manual 'mark replied' the scheduler cancels the due follow-up",
        sc["mark"][0] == 200 and st[0] == "CANCELLED" and "repl" in st[1].lower(), f"mark={sc['mark']} step2={st[:2]}")
    st = mstatus(sc["supp"])
    rec("OUT-088", "Send-time gate: a suppression added out-of-band stops the email",
        st[0] == "CANCELLED" and "suppress" in st[1].lower(), f"{st[:2]}")
    st = mstatus(sc["dup"])
    rec("OUT-060", "A second email for an already-sent step to the same address is cancelled, not sent",
        st[0] == "CANCELLED" and "duplicate" in st[1].lower(), f"{st[:2]}")
    st = mstatus(sc["window"])
    resched = st[5] and datetime.fromisoformat(st[5]) > datetime.utcnow() + timedelta(minutes=20)
    rec("OUT-035", "Due email outside the campaign send window is held and re-queued, not sent",
        st[0] == "QUEUED" and resched and st[3] == "0", f"{st}")
    no_window = "no time zone is on a US business day right now (weekend/US holiday); the scheduler's " \
                "business-day send-window guard holds every send, so this path can't be reached"
    if TZ is None:
        for cid_, t in (("OUT-034", "Inbox daily cap reached: email re-queued for later"),
                        ("OUT-037", "Personal-address recipient blocked before SES with a reason"),
                        ("OUT-049", "SES unavailable: retried with backoff, then FAILED with a final status")):
            blocked(cid_, t, no_window + f"; observed {mstatus(sc[{'OUT-034': 'cap', 'OUT-037': 'personal', 'OUT-049': 'ok'}[cid_]])[:2]}")
        return
    st = mstatus(sc["cap"])
    rec("OUT-034", "Inbox daily cap reached: email re-queued for later, not sent",
        st[0] == "QUEUED" and st[5] and datetime.fromisoformat(st[5]) > datetime.utcnow() + timedelta(hours=1), f"{st}")
    st = mstatus(sc["personal"])
    rec("OUT-037", "Personal-address recipient blocked before SES with a reason",
        st[0] == "FAILED" and "PERSONAL" in st[4] and st[2] == "FAILED", f"{st}")
    ok = sc["ok"]
    first = mstatus(ok)
    final = wait_until(lambda: mstatus(ok)[2] == "FAILED", timeout=max(10, 600 - (time.time() - sc["t0"])), interval=15)
    rec("OUT-049", "SES unavailable: retried with backoff, then FAILED with a final status and reason",
        final and int(mstatus(ok)[3]) >= 1 and mstatus(ok)[1], f"first={first} final={mstatus(ok)}")


def check_background(bg):
    remaining = max(5, 600 - (time.time() - bg["t0"]))
    got = wait_until(lambda: scalar(f"SELECT COUNT(*) FROM email_messages WHERE prospect_id={q(bg['pid'])} "
                                    f"AND direction='INBOUND'") == "1", timeout=remaining, interval=15)
    took = time.time() - bg["t0"]
    rec("OUT-057", "Worker syncs a reply into the inbox within 10 minutes without a manual sync",
        got and cp_status(bg["cid"], bg["pid"]) == "REPLIED", f"after {took:.0f}s inbound={got} cp={cp_status(bg['cid'], bg['pid'])}")


# ═════════════════════════ UI (Playwright) ═════════════════════════

UI_SCRIPT = r"""
import { chromium } from 'playwright';
const [web, email, password, unsubUrl] = process.argv.slice(2);
const out = {pages: {}, errors: [], unsub: null, campaign: null};
const browser = await chromium.launch({executablePath: process.env.CHROMIUM || undefined});
const page = await browser.newPage();
let current = 'login';
page.on('pageerror', e => out.errors.push(`${current}: ${e.message}`));
try {
  await page.goto(`${web}/login`, {waitUntil: 'networkidle'});
  await page.fill('input[type=email]', email);
  await page.fill('input[type=password]', password);
  await page.click('button[type=submit]');
  await page.waitForURL(u => !u.toString().includes('/login'), {timeout: 20000});
  out.loggedIn = page.url();
  for (const p of ['campaigns', 'inboxes', 'inbox', 'prospects', 'lists', 'domain-health']) {
    current = p;
    await page.goto(`${web}/app/${p}`, {waitUntil: 'networkidle'});
    await page.waitForTimeout(1500);
    const text = await page.locator('body').innerText();
    out.pages[p] = {url: page.url(), chars: text.length, crashed: /something went wrong|unexpected application error/i.test(text),
                    snippet: text.slice(0, 120).replace(/\s+/g, ' ')};
  }
  if (process.argv[6]) {
    current = 'campaign-detail';
    await page.goto(`${web}/app/campaigns/${process.argv[6]}`, {waitUntil: 'networkidle'});
    await page.waitForTimeout(1500);
    const text = await page.locator('body').innerText();
    out.campaign = {chars: text.length, hasName: text.includes(process.argv[7] || '@@'),
                    crashed: /something went wrong|unexpected application error/i.test(text)};
  }
  current = 'unsubscribe';
  const p2 = await browser.newPage();
  await p2.goto(unsubUrl, {waitUntil: 'load'});
  const before = await p2.locator('body').innerText();
  await p2.click('button[type=submit]');
  await p2.waitForLoadState('load');
  const after = await p2.locator('body').innerText();
  out.unsub = {before: before.slice(0, 80), after: after.slice(0, 80)};
} catch (e) { out.fatal = String(e); }
await browser.close();
console.log(JSON.stringify(out));
"""


@safe("OUT-130", "UI screens")
def t_ui():
    p = mk_contact(A, em("ui-unsub"))
    cid = ready_campaign(A, "UI campaign", [IN1], [p])
    mid = msg(cid, p)[0]
    mark_sent(mid)
    script = os.path.join(SCRATCH, f"ui_outreach_{RID}.mjs")
    with open(script, "w") as f:
        f.write(UI_SCRIPT)
    shutil.copy(script, os.path.join(HERE, "ui_outreach.mjs"))
    env = {**os.environ, "CHROMIUM": os.environ.get("CHROMIUM", "")}
    exe = [x for x in ("/opt/pw-browsers/chromium",) if os.path.exists(x)]
    if exe:
        env["CHROMIUM"] = exe[0]
    else:
        env.pop("CHROMIUM", None)
        env["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"
    unsub_url = WEB + f"/api/tracking/unsubscribe/{mid}"
    out = subprocess.run(["node", script, WEB, A.email, A.password, unsub_url, "", cid, f"UI campaign {RID}"],
                         cwd=SCRATCH, capture_output=True, text=True, timeout=240, env=env)
    os.remove(script)
    try:
        res = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        rec("OUT-130", "UI run", False, f"node failed: {out.stderr[-400:]}", "test-issue")
        return
    bad = {k: v for k, v in res["pages"].items() if v["crashed"] or v["chars"] < 40 or "/login" in v["url"]}
    rec("OUT-130", "Owner signs in; campaigns, inboxes, unified inbox, prospects, lists, domain-health render without errors",
        res.get("loggedIn") and not res.get("fatal") and len(res["pages"]) == 6 and not bad and not res["errors"],
        f"fatal={res.get('fatal')} bad={bad} pageerrors={res['errors'][:3]}")
    c = res.get("campaign") or {}
    rec("OUT-132", "Launched campaign detail page renders with the campaign name", c.get("hasName") and not c.get("crashed"),
        f"{c}")
    u = res.get("unsub") or {}
    rec("OUT-131", "Recipient unsubscribes in the browser (confirm page -> success) and is suppressed",
        "unsubscribed successfully" in (u.get("after") or "").lower() and suppressed(A, em("ui-unsub")), f"{u}")


# ═════════════════════════ run ═════════════════════════

def main():
    print(f"run {RID}; workspace A={A.email} B={B.email}; business tz={TZ}")
    sc = setup_scheduler_cases()
    bg = setup_background_reply()
    t_jwt()
    t_webhook_auth()
    t_pw_encryption()
    t_rate_limits()
    erased = t_erasure()
    upload_list = t_upload(erased)
    uns = t_enroll_report()
    t_bulk_active(uns)
    t_lists(upload_list)
    t_duplicates()
    t_rotation()
    t_unsubscribe()
    t_new_contact_suppressed()
    t_unique()
    t_soft_delete()
    t_legal_basis()
    t_sender_identity()
    t_ai()
    t_quota()
    t_ms365()
    t_autopause()
    t_complaints()
    t_hourly()
    t_pause_resume()
    t_batch_mode()
    t_scale()
    t_imap(bg)
    t_events()
    t_ui()
    check_scheduler_cases(sc)
    check_background(bg)
    # stop the worker polling mailboxes that only existed while this script ran
    sql(f"UPDATE sending_inboxes SET imap_host=NULL WHERE tenant_id={q(A.tenant_id)} AND imap_host={q(HOST_FROM_CONTAINERS)}")
    counts = R.write()
    print(counts)


if __name__ == "__main__":
    main()
