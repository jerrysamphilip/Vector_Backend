"""System cases (live stack) for the behaviour-fix round.

  BF-001..004  deal won / lost / stage-change notifications (owner + manager, never the actor)
  BF-005..006  campaign auto-pause notifications (owner + admins, with the reason)
  BF-007..009  D1: starter templates for every step when AI generation is unavailable
  BF-010..012  re-invite after a revoked invitation / deactivated invited account
  BF-013..015  Send Now in capped batches with a cursor
  BF-016       S10: nullable response fields in the OpenAPI
  BF-017..019  @mention picker limited to people who can see the record

Runs against the live stack; creates its own workspaces and never calls the LLM (the wizard
case uses a persona with no blueprint, which never reaches the AI). python3 run_behaviour_fixes.py
"""
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import Results, Workspace, req, sql  # noqa: E402

R = Results("behaviour_fixes", os.path.dirname(os.path.abspath(__file__)))
W = Workspace("bf")
OWNER = W.token
rid = W.rid
WEBHOOK_TOKEN = os.environ.get("SES_WEBHOOK_TOKEN", "local-dev-webhook-token")
FUTURE = (date.today() + timedelta(days=20)).isoformat()


def utcnow():
    """Naive UTC now, like app.core.clock.utcnow (the app package is not importable here)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def q(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def scalar(query):
    out = sql(query)
    return out.splitlines()[0] if out else None


def wait_until(fn, timeout=60, interval=2):
    deadline = time.time() + timeout
    val = fn()
    while not val and time.time() < deadline:
        time.sleep(interval)
        val = fn()
    return val


def mk_contact(tok, local, **kw):
    s, b = req("POST", "/contacts", {"email": f"{local}.{rid}@bf-{rid}.example", **kw}, tok)
    assert s == 201, ("contact", s, b)
    return b["prospect_id"]


def notes(user_id, kind, link=None):
    extra = f" AND link={q(link)}" if link else ""
    out = sql(f"SELECT CONCAT(title, ' || ', IFNULL(body,'')) FROM notifications WHERE user_id={q(user_id)} "
              f"AND kind={q(kind)}{extra}")
    return out.splitlines() if out else []


def mk_campaign(name, steps=1, **kw):
    s, b = req("POST", "/campaigns", {"campaign_name": f"{name} {rid}", "respect_timezone": False,
                                      "campaign_timezone": "UTC", "min_gap_minutes": 0, "start_date": FUTURE, **kw}, OWNER)
    assert s == 201, ("campaign", s, b)
    cid = b["campaign_id"]
    for i in range(1, steps + 1):
        s, b = req("POST", f"/campaigns/{cid}/sequences", {"step_number": i, "wait_days": 0 if i == 1 else 2}, OWNER)
        assert s == 201, ("step", s, b)
    return cid


def mk_inbox(local, domain):
    """Connect a sending mailbox through POST /inboxes, as the "connect mailbox" screen does. A campaign
    needs an ACTIVE one to launch or Send Now (app/services/send_guards.py). Its SMTP host is
    smtp.invalid (RFC 2606, never resolves), so no mail can leave even if a send were attempted."""
    s, b = req("POST", "/inboxes", {"email_address": f"{local}-{rid}@{domain}", "smtp_host": "smtp.invalid",
                                    "smtp_username": local, "smtp_password": f"Smtp-{rid}", "warmup_enabled": False,
                                    "daily_limit": 500, "max_emails_per_day": 500}, OWNER)
    assert s == 201 and b.get("status") == "ACTIVE", ("inbox", s, b)
    return b["inbox_id"]


def safe(fn):
    try:
        fn()
    except Exception as exc:  # one broken setup never hides the other cases
        R.record(f"BF-{fn.__name__}", f"{fn.__name__} setup", False, repr(exc)[:400], "test-issue")


# ── BF-001..004 deal stage notifications ─────────────────────

def deals():
    mgr = W.add_user("MANAGER", "Mona")
    rep = W.add_user("AGENT", "Remy", manager_id=mgr["user_id"])
    s, stages = req("GET", "/sales/stages", None, OWNER)
    opens = [x for x in stages if x["status"] == "OPEN"]
    won = next(x for x in stages if x["status"] == "WON")
    lost = next(x for x in stages if x["status"] == "LOST")
    s, d = req("POST", "/opportunities", {"name": f"BF deal {rid}", "owner_id": rep["user_id"],
                                          "stage_id": opens[0]["stage_id"]}, OWNER)
    assert s == 201, ("deal", s, d)
    did, link = d["opportunity_id"], f"/app/deals/{d['opportunity_id']}"

    s, _ = req("PATCH", f"/opportunities/{did}", {"stage_id": opens[1]["stage_id"]}, OWNER)
    r1, m1, a1 = notes(rep["user_id"], "DEAL_STAGE", link), notes(mgr["user_id"], "DEAL_STAGE", link), \
        notes(W.user_id, "DEAL_STAGE", link)
    R.check("BF-001", "Stage change notifies the deal owner and their manager, linking to the deal, not the actor",
            s == 200 and len(r1) == 1 and len(m1) == 1 and not a1 and opens[1]["name"] in r1[0], (s, r1, m1, a1))

    s, _ = req("PATCH", f"/opportunities/{did}", {"stage_id": won["stage_id"], "closed_reason": "Great fit"}, OWNER)
    r2, m2 = notes(rep["user_id"], "DEAL_WON", link), notes(mgr["user_id"], "DEAL_WON", link)
    R.check("BF-002", "Deal won notifies owner and manager with the reason", s == 200 and len(r2) == 1 and len(m2) == 1
            and "Great fit" in r2[0], (s, r2, m2))

    s, _ = req("PATCH", f"/opportunities/{did}", {"stage_id": lost["stage_id"], "closed_reason": "No budget"}, rep["token"])
    r3, m3 = notes(rep["user_id"], "DEAL_LOST", link), notes(mgr["user_id"], "DEAL_LOST", link)
    R.check("BF-003", "Owner marking their own deal lost notifies only their manager", s == 200 and not r3 and len(m3) == 1,
            (s, r3, m3))

    s, b = req("GET", "/notifications", None, rep["token"])
    items = (b or {}).get("items", []) if isinstance(b, dict) else []
    R.check("BF-004", "Deal notifications appear in the owner's in-app notifications",
            s == 200 and any(n.get("kind") == "DEAL_WON" and n.get("link") == link for n in items), (s, str(b)[:300]))


# ── BF-005..006 auto-pause notifications ─────────────────────

def autopause():
    W.set_sender_identity()
    admin = W.add_user("ADMIN", "Ada")
    agent = W.add_user("AGENT", "Abe")
    dom = f"bf-cmp-{rid}.example"
    cid = mk_campaign("BF complaints", inbox_ids=[mk_inbox("cmp", dom)], send_window_start="09:00:00",
                      send_window_end="17:00:00")
    pids = [mk_contact(OWNER, f"cmp{i}") for i in range(3)]
    s, b = req("POST", f"/campaigns/{cid}/prospects", {"prospect_ids": pids}, OWNER)
    assert s == 200, ("enroll", s, b)
    assert req("POST", f"/campaigns/{cid}/content/stub", None, OWNER)[0] == 200
    s, b = req("POST", f"/campaigns/{cid}/launch", None, OWNER)
    assert s == 200, ("launch", s, b)
    for i, pid in enumerate(pids[:2]):
        mid = scalar(f"SELECT message_id FROM email_messages WHERE campaign_id={q(cid)} AND prospect_id={q(pid)} LIMIT 1")
        ses_id = f"ses-{uuid.uuid4().hex}"
        sql(f"UPDATE email_messages SET status='SENT', sent_at=UTC_TIMESTAMP(), ses_message_id={q(ses_id)} "
            f"WHERE message_id={q(mid)}")
        email = f"cmp{i}.{rid}@bf-{rid}.example"
        event = {"notificationType": "Complaint", "eventType": "Complaint",
                 "mail": {"timestamp": utcnow().isoformat() + "Z", "messageId": ses_id,
                          "source": f"cmp-{rid}@{dom}", "destination": [email], "tags": {"message_id": [mid]}},
                 "complaint": {"complaintFeedbackType": "abuse", "timestamp": utcnow().isoformat() + "Z",
                               "complainedRecipients": [{"emailAddress": email}]}}
        req("POST", f"/webhooks/ses/notifications?token={WEBHOOK_TOKEN}", event)
    paused = wait_until(lambda: scalar(f"SELECT status FROM campaigns WHERE campaign_id={q(cid)}") == "PAUSED")
    link = f"/app/campaigns/{cid}"
    o, a, g = (notes(W.user_id, "CAMPAIGN_AUTO_PAUSED", link), notes(admin["user_id"], "CAMPAIGN_AUTO_PAUSED", link),
               notes(agent["user_id"], "CAMPAIGN_AUTO_PAUSED", link))
    R.check("BF-005", "Auto-paused campaign notifies its owner and the workspace admins (not agents)",
            paused and len(o) == 1 and len(a) == 1 and not g, (paused, o, a, g))
    R.check("BF-006", "The auto-pause notification gives the reason", bool(o) and "complaint" in o[0].lower(), o)


# ── BF-007..009 D1 starter templates ─────────────────────────

def wizard():
    cid = mk_campaign("BF wizard", steps=3)
    owner_id = W.user_id
    list_id = str(uuid.uuid4())
    sql(f"INSERT INTO prospect_lists (list_id, tenant_id, list_name, source_type, list_type, uploaded_by, uploaded_at) "
        f"VALUES ({q(list_id)}, {q(W.tenant_id)}, {q('BF list ' + rid)}, 'MANUAL', 'STATIC', {q(owner_id)}, NOW())")
    persona = f"BF_NO_BLUEPRINT_{rid}".upper()  # no blueprint: the AI is never called
    s, b = req("POST", f"/campaigns/{cid}/personas/{persona}/generations",
               {"list_id": list_id, "sequence_steps": [{"wait_days": 0}, {"wait_days": 2}, {"wait_days": 2}]}, OWNER)
    emails = (b or {}).get("all_emails", []) if isinstance(b, dict) else []
    R.check("BF-007", "Without AI the wizard creates a template for every one of the 3 steps",
            s == 200 and [e.get("step_number") for e in emails] == [1, 2, 3]
            and scalar(f"SELECT COUNT(*) FROM email_templates WHERE campaign_id={q(cid)}") == "3", (s, str(b)[:300]))
    R.check("BF-008", "The response tells the UI starter templates were used",
            isinstance(b, dict) and b.get("fallback_used") is True
            and b.get("notice") == "AI unavailable - starter templates were used, please review", str(b)[:300])
    subjects = {e.get("subject") for e in emails}
    ai_flags = scalar(f"SELECT SUM(is_ai_generated) FROM email_templates WHERE campaign_id={q(cid)}")
    R.check("BF-009", "Starter templates differ per step and are marked not AI-generated",
            len(subjects) == 3 and ai_flags == "0" and "{{first_name}}" in (emails[0].get("body") if emails else ""),
            (subjects, ai_flags))


# ── BF-010..012 re-invite ────────────────────────────────────

def reinvite():
    email = f"reinvite-{rid}@systest.example"
    s, b = req("POST", "/api/users/invite", {"email": email, "role": "AGENT"}, OWNER)
    assert s == 201, ("invite", s, b)
    inv1, uid = b["invitation_id"], b["user_id"]
    s1, _ = req("DELETE", f"/api/users/invitations/{inv1}", None, OWNER)
    s2, b2 = req("POST", "/api/users/invite", {"email": email, "role": "AGENT"}, OWNER)
    R.check("BF-010", "Re-inviting an email whose invitation was revoked reissues it (201, same account)",
            s1 == 200 and s2 == 201 and b2.get("user_id") == uid
            and scalar(f"SELECT status FROM users WHERE user_id={q(uid)}") == "INVITED", (s1, s2, b2))
    s3, _ = req("DELETE", f"/api/users/{uid}", None, OWNER)
    s4, b4 = req("POST", "/api/users/invite", {"email": email, "role": "MANAGER"}, OWNER)
    R.check("BF-011", "Re-inviting an invited account that was deactivated works too",
            s3 == 200 and s4 == 201 and scalar(f"SELECT role FROM users WHERE user_id={q(uid)}") == "MANAGER", (s3, s4, b4))
    member = W.add_user("AGENT", "Ivy")
    s5, b5 = req("POST", "/api/users/invite", {"email": member["email"], "role": "AGENT"}, OWNER)
    R.check("BF-012", "Inviting someone who already joined is still 409", s5 == 409, (s5, b5))


# ── BF-013..015 Send Now batches ─────────────────────────────

def send_now():
    W.set_sender_identity()
    cid = mk_campaign("BF send now", inbox_ids=[mk_inbox("sn", f"bf-sn-{rid}.example")])
    assert req("POST", f"/campaigns/{cid}/content/stub", None, OWNER)[0] == 200
    seq = scalar(f"SELECT sequence_id FROM email_sequences WHERE campaign_id={q(cid)} AND step_number=1")
    tpl = scalar(f"SELECT template_id FROM email_templates WHERE campaign_id={q(cid)} LIMIT 1")
    # Unsubscribed contacts: every message is stopped by the send-time guard, so nothing reaches SES
    pids = [mk_contact(OWNER, f"sn{i}", consent_status="UNSUBSCRIBED") for i in range(30)]
    values = ",".join(f"({q(uuid.uuid4())}, {q(cid)}, {q(p)}, {q(W.tenant_id)}, {q(seq)}, {q(tpl)}, 'OUTBOUND', 'QUEUED', "
                      f"{q(f'sn{i}.{rid}@bf-{rid}.example')}, 'BF', UTC_TIMESTAMP())" for i, p in enumerate(pids))
    sql("INSERT INTO email_messages (message_id, campaign_id, prospect_id, tenant_id, sequence_id, template_id, direction, "
        f"status, to_email, subject, scheduled_at) VALUES {values}")
    sql(f"UPDATE campaigns SET status='ACTIVE' WHERE campaign_id={q(cid)}")

    s, b = req("POST", f"/campaigns/{cid}/send-now", None, OWNER)
    R.check("BF-013", "Send Now handles at most 25 messages per request and says how many remain",
            s == 200 and b.get("total") == 25 and b.get("has_more") is True and b.get("remaining") == 5
            and "sent" in b and "failed" in b, (s, b))
    s2, b2 = req("POST", f"/campaigns/{cid}/send-now?cursor={b.get('cursor')}&since={b.get('started_at')}", None, OWNER)
    R.check("BF-014", "Continuing with the cursor finishes the rest and stops",
            s2 == 200 and b2.get("total") == 5 and b2.get("has_more") is False and b2.get("remaining") == 0, (s2, b2))
    left = scalar(f"SELECT COUNT(*) FROM email_messages WHERE campaign_id={q(cid)} AND status IN ('QUEUED','SCHEDULED','SENDING')")
    s3, b3 = req("POST", f"/campaigns/{cid}/send-now?cursor=not-a-cursor", None, OWNER)
    R.check("BF-015", "Every queued message was handled exactly once; a malformed cursor is 400",
            left == "0" and (b.get("skipped", 0) + b2.get("skipped", 0)) == 30 and s3 == 400, (left, s3, b3))


# ── BF-016 S10 ───────────────────────────────────────────────

def s10():
    s, spec = req("GET", "/openapi.json")
    if s != 200:
        R.record("BF-016", "Response fields backed by nullable columns are nullable in the OpenAPI", None,
                 f"/openapi.json not served ({s}); set ENABLE_DOCS=true", "test-issue")
        return
    schemas = (spec or {}).get("components", {}).get("schemas", {}) if isinstance(spec, dict) else {}

    def nullable(model, field):
        p = schemas.get(model, {}).get("properties", {}).get(field, {})
        return bool(p.get("nullable") or any(o.get("type") == "null" for o in p.get("anyOf", [])))

    pairs = [("CompanyProfileResponse", "is_default"), ("SendingInboxResponse", "status"),
             ("SendingInboxResponse", "daily_limit"), ("ProspectListResponse", "uploaded_at"),
             ("ProspectInListResponse", "consent_status"), ("UserOut", "status")]
    bad = [p for p in pairs if not nullable(*p)]
    R.check("BF-016", "Response fields backed by nullable columns are nullable in the OpenAPI", s == 200 and not bad, bad)


# ── BF-017..019 mention picker ───────────────────────────────

def mentions():
    a1 = W.add_user("AGENT", "Mia")
    a2 = W.add_user("AGENT", "Max")
    pid = mk_contact(OWNER, "scoped", owner_id=a1["user_id"])
    s, allu = req("GET", "/contacts/mentionable-users?limit=50", None, OWNER)
    s2, scoped = req("GET", f"/contacts/mentionable-users?limit=50&prospect_id={pid}", None, OWNER)
    ids_all = {u["user_id"] for u in allu or []}
    ids_scoped = {u["user_id"] for u in scoped or []}
    R.check("BF-017", "Picker with prospect_id lists only people who can see the contact",
            s == 200 and s2 == 200 and a2["user_id"] in ids_all and a1["user_id"] in ids_scoped
            and a2["user_id"] not in ids_scoped and W.user_id in ids_scoped, (ids_all, ids_scoped))
    s3, b3 = req("GET", f"/contacts/mentionable-users?prospect_id={pid}", None, a2["token"])
    R.check("BF-018", "Someone who cannot see the contact gets 404 from the scoped picker", s3 == 404, (s3, b3))
    s, stages = req("GET", "/sales/stages", None, OWNER)
    st = next(x for x in stages if x["status"] == "OPEN")
    s, d = req("POST", "/opportunities", {"name": f"BF mention deal {rid}", "owner_id": a1["user_id"],
                                          "stage_id": st["stage_id"]}, OWNER)
    s4, dl = req("GET", f"/contacts/mentionable-users?limit=50&opportunity_id={d['opportunity_id']}", None, OWNER)
    ids_deal = {u["user_id"] for u in dl or []}
    R.check("BF-019", "Deal note picker lists only people who can see the deal",
            s4 == 200 and a1["user_id"] in ids_deal and a2["user_id"] not in ids_deal, (s4, ids_deal))


for case in (deals, autopause, wizard, reinvite, send_now, s10, mentions):
    safe(case)

R.write()
print("COUNTS", {k: sum(1 for r in R.rows if r["status"] == k) for k in ("PASS", "FAIL", "BLOCKED")})
