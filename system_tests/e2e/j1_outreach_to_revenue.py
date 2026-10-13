"""J1 - Outreach-to-revenue chain (sign-up -> mailbox -> upload -> campaign -> worker send via SES stand-in ->
SES events -> unsubscribe -> IMAP replies -> lead -> SQL -> deal -> won -> reports -> follow-ups -> pause/resume -> GDPR).

Phases are functions taking the shared context C; run_journeys can resume at a phase (state in .state/j1.json).
"""
import json
import re
import time
import uuid
from datetime import date, datetime, timedelta

from .flows import (CSV_HEAD, add_missing_step_templates, captured_for, contact_by_email, create_campaign, detach_imap,
                    dt, enroll, generate_content, launch, mk_inbox, msgs, sequences, travel_due, upload_list, utc_now,
                    wait_status)
from .helpers import (timed, ev_bounce, ev_click, ev_delivery, ev_open, iso, FakeImap, HOST_FROM_CONTAINERS, SCRATCH, add_user, follow, new_workspace, q, reply_mime, req,
                      rows, run_job, scalar, ses_bounce, ses_captured, ses_count, ses_delivery, ses_post, sql, user_token,
                      wait_until)

NAMES = ["Ann Adler", "Bo Brandt", "Cy Cole", "Di Dunn", "Ed Egan", "Flo Fry", "Gus Gray", "Hal Hart", "Ida Ives",
         "Jo Jain", "Kit Kerr", "Lea Lund", "Max Moss", "Ned Nash", "Oli Orr", "Pam Pike"]
DESIGNATIONS = ["CEO", "CTO", "VP Sales", "Head of Marketing", "CFO", "Director of Engineering", "COO", "Founder"]
# Roles in the story (index into the 16 valid prospects)
ROLE = {"unsub_link": 2, "unsub_oneclick": 3, "replier": 4, "ooo": 5, "reply_unsub": 6, "bounce": 7, "bg_reply": 8,
        "held": 9}


class Fail(Exception):
    pass


def em_of(C, i):
    return C["emails"][i]


def phase_setup(R, C):
    """J1-01..J1-08: sign-up, team, sender identity, mailboxes."""
    ws = new_workspace("j1")
    rid = ws.rid
    C["w"] = ws
    C.update(rid=rid, ws={"email": ws.email, "password": ws.password, "tenant_id": ws.tenant_id,
                          "user_id": ws.user_id, "token": ws.token, "rid": rid})
    s, me = req("GET", "/api/auth/me", token=ws.token)
    R.check("E2E-J1-01", "Self sign-up creates a workspace; the owner is SUPER_ADMIN of a fresh tenant",
            s == 200 and me.get("role") == "SUPER_ADMIN" and me.get("tenant_id") == ws.tenant_id
            and scalar(f"SELECT COUNT(*) FROM users WHERE tenant_id={q(ws.tenant_id)}") == "1",
            f"{s} {str(me)[:200]}")

    mgr = add_user(ws, "MANAGER", f"Mia{rid}")
    a1 = add_user(ws, "AGENT", f"Ana{rid}")
    a2 = add_user(ws, "AGENT", f"Ben{rid}")
    C["users"] = {"owner": {"email": ws.email, "user_id": ws.user_id, "token": ws.token},
                  "mgr": mgr, "a1": a1, "a2": a2}
    errs = []
    for key, level, boss in (("owner", 1, None), ("mgr", 2, "owner"), ("a1", 3, "mgr"), ("a2", 3, "mgr")):
        body = {"sales_level": level, "manager_id": C["users"][boss]["user_id"] if boss else None}
        s, b = req("PUT", f"/team/hierarchy/{C['users'][key]['user_id']}", body, ws.token)
        if s != 200:
            errs.append((key, s, b))
    s, h = req("GET", "/team/hierarchy", token=ws.token)
    by = {u["user_id"]: u for u in h.get("users", [])} if s == 200 else {}
    R.check("E2E-J1-02", "Owner builds the hierarchy (owner L1 -> manager L2 -> two agents L3)",
            not errs and by.get(a1["user_id"], {}).get("manager_id") == mgr["user_id"]
            and by.get(a2["user_id"], {}).get("manager_id") == mgr["user_id"]
            and by.get(mgr["user_id"], {}).get("direct_reports") == 2,
            f"{errs} {[(u['email'][:8], u['sales_level'], u['manager_id'] and u['manager_id'][:6]) for u in by.values()]}")

    s, b = ws.set_sender_identity()
    s2, g = req("GET", "/api/company-profiles/sender-identity", token=ws.token)
    C["company"], C["postal"] = b.get("company_name"), b.get("postal_address")
    R.check("E2E-J1-03", "Sender identity (company + postal address) saved and read back complete",
            s == 200 and s2 == 200 and g.get("is_complete") is True and g.get("postal_address") == C["postal"]
            and g.get("company_name") == f"Systest Co {rid}", f"{s} {b} / {s2} {g}")

    imap = FakeImap(SCRATCH)
    C["_imap"] = imap
    dom = f"outreach-{rid}.example"
    C["dom"] = dom
    in1, addr1, b1 = mk_inbox(ws, f"priya.{rid}", dom, imap=imap, delay=4)
    in2, addr2, b2 = mk_inbox(ws, f"sam.{rid}", dom, imap=imap, delay=4)
    C.update(inbox1=in1, inbox2=in2, addr1=addr1, addr2=addr2)
    leak = json.dumps(b1)
    R.check("E2E-J1-04", "Two mailboxes connected with IMAP settings; secrets encrypted at rest and never returned",
            b1.get("has_imap_password") and b1.get("imap_host") == HOST_FROM_CONTAINERS
            and "Pw-e2e-1" not in leak
            and "Pw-e2e-1" not in (scalar(f"SELECT smtp_password FROM sending_inboxes WHERE inbox_id={q(in1)}") or ""),
            f"{leak[:200]}")
    s, t = req("GET", f"/inboxes/{in1}/test-imap", token=ws.token)
    logged = [x for x in imap.logins if x == (addr1.lower(), True)]
    R.check("E2E-J1-05", "IMAP connection test: the API container really logs in to the (fake) mailbox",
            s == 200 and logged, f"{s} {str(t)[:200]} logins={imap.logins[:3]}")
    R.check("E2E-J1-06", "Mailbox domain was registered as an SES identity (domain-verification flow reached SES)",
            scalar(f"SELECT COUNT(*) FROM sending_domains WHERE tenant_id={q(ws.tenant_id)} AND domain_name={q(dom)}") == "1",
            "sending_domains row missing for the tenant")


def phase_upload(R, C):
    """J1-07..J1-12: CSV with rejects -> list -> companies."""
    ws = C["w"]
    rid = C["rid"]
    emails, lines = [], []
    for i, name in enumerate(NAMES):
        first, last = name.split()
        company = ["Alpha", "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet",
                   "Kilo", "Lima", "Mike", "November", "Oscar"][i]
        dom = f"{company.lower()}-{rid}.example"
        e = f"{first.lower()}.{last.lower()}@{dom}"
        emails.append(e)
        lines.append(f"{first},{last},{company} Corp,{e},{DESIGNATIONS[i % len(DESIGNATIONS)]}")
    # contact that is already unsubscribed in this workspace
    pre_unsub = f"pre.unsub@zulu-{rid}.example"
    s, pc = req("POST", "/contacts", {"email": pre_unsub, "first_name": "Pre", "last_name": "Unsub",
                                      "company_name": "Zulu Corp"}, ws.token)
    assert s == 201, (s, pc)
    req("PATCH", f"/contacts/{pc['prospect_id']}", {"consent_status": "UNSUBSCRIBED"}, ws.token)
    bad_rows = [
        f"Gee,Mail,Gmailco,gee.mail{rid}@gmail.com,CEO",             # 17 personal domain
        f"Bro,Ken,Brokenco,broken-{rid}@,CEO",                       # 18 invalid address
        f",Nofirst,Nofirstco,nofirst@nofirst-{rid}.example,CTO",     # 19 missing first name
        f"Ann,Adler,Alpha Corp,{emails[0].upper()},CEO",             # 20 duplicate (case-insensitive) of row 1
        f"Pre,Unsub,Zulu Corp,{pre_unsub},CEO",                      # 21 unsubscribed contact
    ]
    csv = CSV_HEAD + "\n" + "\n".join(lines + bad_rows) + "\n"
    v, c = upload_list(ws, csv, f"J1 prospects {rid}")
    C.update(emails=emails, pre_unsub=pre_unsub, list_id=c["list_id"], upload_id=v["upload_id"])
    recs = {r["row"]: r for r in v["records"]}
    want = {17: "personal", 18: "email", 19: "first name", 20: "duplicate", 21: "unsubscribed"}
    bad = {n: (recs.get(n, {}).get("status"), recs.get(n, {}).get("reason")) for n, key in want.items()
           if recs.get(n, {}).get("status") != "REJECTED" or key not in (recs[n].get("reason") or "").lower()}
    R.check("E2E-J1-07", "CSV validation: 21 rows -> 16 accepted, 5 rejected, each with its specific reason",
            v["total_rows"] == 21 and v["accepted"] == 16 and v["rejected"] == 5 and not bad,
            f"total={v['total_rows']} acc={v['accepted']} rej={v['rejected']} wrong={bad}")
    R.check("E2E-J1-08", "Confirming the upload creates the list with exactly the 16 accepted contacts",
            c["inserted"] == 16 and c["new_prospects"] == 16 and c["skipped_unsubscribed"] == 0
            and scalar(f"SELECT COUNT(*) FROM prospect_list_members WHERE list_id={q(c['list_id'])}") == "16",
            f"{c}")
    for bad_e in (f"gee.mail{rid}@gmail.com", f"nofirst@nofirst-{rid}.example"):
        assert contact_by_email(ws.token, bad_e) is None
    s, accs = req("GET", "/accounts?page_size=100", token=ws.token)
    names = {a.get("name") for a in (accs.get("items") if isinstance(accs, dict) else [])}
    R.check("E2E-J1-09", "Companies from the CSV exist as accounts (15 distinct names; Ann+Bo share 'Alpha Corp')",
            s == 200 and {"Alpha Corp", "Bravo Corp", "Oscar Corp"} <= names
            and scalar(f"SELECT COUNT(*) FROM prospects WHERE tenant_id={q(ws.tenant_id)} AND account_id IS NOT NULL "
                       f"AND LOWER(email) IN ({q(emails[0].lower())},{q(emails[1].lower())}) GROUP BY account_id") == "2",
            f"{s} accounts={sorted(n for n in names if n)[:20]}")
    ids = {}
    for e in emails:
        it = contact_by_email(ws.token, e)
        ids[e] = it["prospect_id"] if it else None
    C["pid"] = ids
    R.check("E2E-J1-10", "All 16 contacts are readable via the contacts API with names/companies from the CSV",
            all(ids.values()), f"missing={[e for e, i in ids.items() if not i]}")
    # hand contacts to the agents so the hierarchy has something to scope (owner = agent for the reply path)
    u = C["users"]
    for i in (ROLE["replier"], ROLE["bg_reply"]):
        s, b = req("PATCH", f"/contacts/{ids[emails[i]]}", {"owner_id": u["a1"]["user_id"]}, ws.token)
        assert s == 200, (s, b)


def phase_campaign(R, C):
    """J1-11..J1-20: campaign wizard calls, LLM fallback, enrolment, launch, pre-scheduling."""
    ws = C["w"]
    rid = C["rid"]
    waits = (0, 2, 3)
    C["waits"] = waits
    cid, cb = create_campaign(ws, f"J1 Outreach {rid}", [C["inbox1"], C["inbox2"]], waits=waits,
                              sender_name="Priya Sharma", sender_title="Account Executive")
    C["cid"] = cid
    s, g = req("GET", f"/campaigns/{cid}", token=ws.token)
    R.check("E2E-J1-11", "Campaign created in DRAFT with both mailboxes, 24h UTC window, sender name/title",
            g.get("status") == "DRAFT" and len(g.get("sending_inboxes", [])) == 2 and g.get("send_window_start") == "00:00:00"
            and g.get("campaign_timezone") == "UTC" and g.get("sender_name") == "Priya Sharma"
            and g.get("sender_title") == "Account Executive", f"{str(g)[:300]}")
    seq = sequences(ws, cid)
    R.check("E2E-J1-12", "3-step sequence saved with wait days 0 / 2 / 3",
            [(x["step_number"], x["wait_days"]) for x in seq] == [(1, 0), (2, 2), (3, 3)], f"{seq}")

    s, gen = generate_content(ws, cid, C["list_id"], waits)
    tmpl = {}
    if s == 200:
        for t in gen["templates_generated"]:
            tmpl[t["persona_type"]] = t
    R.check("E2E-J1-13", "Wizard 'generate templates' works without an LLM (fallback template per persona)",
            s == 200 and tmpl and all(t["template"]["subject"] and t["template"]["body"] for t in tmpl.values()),
            f"{s} {str(gen)[:300]}", "defect")
    st1 = list(tmpl.values())[0]["template"] if tmpl else {}
    R.check("E2E-J1-14", "Fallback step-1 template carries personalisation tokens and no unresolved internal placeholders",
            "{{first_name}}" in (st1.get("subject", "") + st1.get("body", "")) and "<<" not in st1.get("body", "")
            and "{{company_name}}" in st1.get("body", ""), f"{st1}")
    s, ts = req("GET", f"/campaigns/{cid}/templates", token=ws.token)
    per_step = {}
    for t in ts.get("templates", []):
        per_step.setdefault(t["step_number"], []).append(t)
    R.check("E2E-J1-15", "Fallback gives a usable 3-step sequence (a template for EVERY step)",
            sorted(per_step) == [1, 2, 3],
            f"templates exist only for steps {sorted(per_step)}; GET /campaigns/{{id}}/sequences/2/emails -> "
            f"{req('GET', f'/campaigns/{cid}/sequences/2/emails', token=ws.token)[1].get('template')}", "defect")
    s, e = enroll(ws, cid, C["list_id"])
    R.check("E2E-J1-16", "Enrolment: 16 enrolled, 0 rejected (wizard step 'launch' part 1)",
            s == 200 and e.get("enrolled_count") == 16 and e.get("rejected_count") == 0, f"{s} {str(e)[:300]}")
    s, e2 = enroll(ws, cid, C["list_id"])
    R.check("E2E-J1-17", "Enrolling the same list again is idempotent: nobody enrolled twice",
            s == 200 and e2.get("enrolled_count") == 0
            and scalar(f"SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id={q(cid)}") == "16", f"{s} {str(e2)[:300]}")
    if sorted(per_step) == [1, 2, 3]:
        # the no-LLM fallback (starter templates) covers every step: nothing to add by hand, launch goes straight through
        R.check("E2E-J1-18", "Every step already has a template, so no follow-up template has to be added by hand",
                add_missing_step_templates(ws, cid) == 0
                and scalar(f"SELECT COUNT(*) FROM email_messages WHERE campaign_id={q(cid)}") == "0", f"per_step={sorted(per_step)}")
        C["workaround_templates"] = 0
        t0 = utc_now()
        s, lr = launch(ws, cid)
        R.check("E2E-J1-19", "Launch with the fallback templates (none added by hand) -> ACTIVE",
                s == 200 and lr.get("new_status") == "ACTIVE", f"{s} {lr}")
    else:
        s, l0 = launch(ws, cid)
        R.check("E2E-J1-18", "Launch is refused while a step has no template (nothing scheduled)",
                s == 400 and "Missing email templates" in str(l0)
                and scalar(f"SELECT COUNT(*) FROM email_messages WHERE campaign_id={q(cid)}") == "0",
                f"{s} {l0}", "defect")
        made = add_missing_step_templates(ws, cid)
        C["workaround_templates"] = made
        t0 = utc_now()
        s, lr = launch(ws, cid)
        R.check("E2E-J1-19", "Launch (after adding the missing follow-up templates by hand) -> ACTIVE",
                s == 200 and lr.get("new_status") == "ACTIVE", f"{s} {lr}")
    C["launched_at"] = t0.strftime("%Y-%m-%d %H:%M:%S")
    allm = msgs(cid)
    by_step = {k: [m for m in allm if m["step"] == str(k)] for k in (1, 2, 3)}
    inbox_split = {i: sum(1 for m in by_step[1] if m["inbox_id"] == i) for i in (C["inbox1"], C["inbox2"])}
    R.check("E2E-J1-20", "48 messages pre-scheduled (16 contacts x 3 steps), all SCHEDULED, inboxes alternate 8 / 8",
            len(allm) == 48 and all(m["status"] == "SCHEDULED" for m in allm) and sorted(inbox_split.values()) == [8, 8]
            and {m["email"] for m in by_step[1]} == {e.lower() for e in C["emails"]}, f"n={len(allm)} split={inbox_split}")
    # resolved content per recipient (subject/body rendered at pre-schedule time)
    rendered = rows(f"SELECT p.first_name, p.company_name, m.subject, m.body_text FROM email_messages m "
                    f"JOIN prospects p ON p.prospect_id=m.prospect_id JOIN email_sequences s ON s.sequence_id=m.sequence_id "
                    f"WHERE m.campaign_id={q(cid)} AND s.step_number=1")
    bad = [r for r in rendered if "{{" in r[2] + r[3] or r[0] not in r[2] + r[3] or r[1] not in r[3]]
    R.check("E2E-J1-21", "Step-1 subject/body are rendered per contact: first name + company substituted, no '{{' left",
            len(rendered) == 16 and not bad, f"{len(rendered)} rows; bad={bad[:2]}")
    # scheduling rules: valid business day, never before launch
    t1 = [dt(m["scheduled_at"]) for m in by_step[1]]
    wk = {d.weekday() for d in t1}
    C["step1_planned_min"] = min(t1).strftime("%Y-%m-%d %H:%M:%S")
    R.check("E2E-J1-22", "Pre-scheduling places step 1 on a business day (not weekend) and not before launch",
            all(d.weekday() < 5 for d in t1) and min(t1) >= t0 - timedelta(minutes=1),
            f"launch={t0} step1 range {min(t1)} .. {max(t1)} weekdays={sorted(wk)}")
    ok_chain = True
    detail = []
    for e in C["emails"]:
        ms = {int(m["step"]): dt(m["scheduled_at"]) for m in allm if m["email"] == e.lower()}
        if not (ms[2] >= ms[1] + timedelta(days=2) - timedelta(minutes=1) and ms[3] >= ms[2] + timedelta(days=3) - timedelta(minutes=1)):
            ok_chain = False
            detail.append((e, ms))
    R.check("E2E-J1-23", "Planned follow-ups are spaced by the configured wait days (step2 >= step1+2d, step3 >= step2+3d)",
            ok_chain, f"{detail[:2]}")


def phase_send(R, C):
    """J1-24..J1-32: WORKER sends step 1 through the SES stand-in; assert real MIME."""
    ws = C["w"]
    cid = C["cid"]
    emails = C["emails"]
    n0 = ses_count()
    C["ses_n0"] = n0
    planned = dt(C["step1_planned_min"])
    # TEST CLOCK: on a weekend/holiday (or before the planned slot) the planner puts step 1 on the next business day;
    # the scheduler itself is real. Move step 1 to "now" for this tenant only.
    travelled = planned > utc_now()
    if travelled:
        travel_due(cid, 1)
    C["clock_travel_step1"] = travelled
    T_START = time.time()
    ok = wait_status(cid, 1, 16, "SENT", timeout=420)
    C["send_secs"] = round(time.time() - T_START, 1)
    sent = msgs(cid, 1, "SENT")
    got = captured_for(emails=emails, domain=C["dom"])
    C["captured1"] = {c.to: c for c in got}
    R.check("E2E-J1-24", "The worker sends all 16 step-1 emails through SES (captured by the stand-in), none to rejected rows",
            ok and len(sent) == 16 and len(got) == 16 and {c.to for c in got} == {e.lower() for e in emails}
            and not captured_for(emails=[f"gee.mail{C['rid']}@gmail.com", C["pre_unsub"]]),
            f"sent={len(sent)} captured={len(got)} secs={C['send_secs']}")
    per_to = {}
    for c in got:
        per_to[c.to] = per_to.get(c.to, 0) + 1
    R.check("E2E-J1-25", "Exactly one email per eligible contact: no duplicates, count == eligible prospects (16)",
            all(v == 1 for v in per_to.values()) and len(per_to) == 16, f"{per_to}")
    by_id = {m["ses_id"]: m for m in sent}
    corr = [c for c in got if c.id in by_id and by_id[c.id]["email"] == c.to and by_id[c.id]["pm_id"] == c.header("Message-ID")]
    R.check("E2E-J1-26", "DB <-> SES correlation: ses_message_id == SES id, provider_message_id == MIME Message-ID, per recipient",
            len(corr) == 16, f"matched {len(corr)}/16")
    # pacing per inbox
    pace = []
    for ib in (C["inbox1"], C["inbox2"]):
        ts = sorted(dt(m["sent_at"]) for m in sent if m["inbox_id"] == ib)
        gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
        pace.append((len(ts), min(gaps) if gaps else None))
    R.check("E2E-J1-27", "Per-inbox pacing: 8 per inbox, consecutive sends >= ~delay_between_emails (4s, jitter <= 25%)",
            all(n == 8 and g is not None and g >= 2 for n, g in pace), f"(count, min gap s) per inbox={pace}")
    cnt = rows(f"SELECT inbox_id, emails_sent_today FROM sending_inboxes WHERE inbox_id IN ({q(C['inbox1'])},{q(C['inbox2'])})")
    R.check("E2E-J1-28", "Daily-limit counters on the mailboxes reflect the sends (8 + 8)",
            sorted(int(x[1]) for x in cnt) == [8, 8], f"{cnt}")

    bad = []
    for c in got:
        m = by_id.get(c.id)
        from_addr = re.search(r"<([^>]+)>", c.header("From") or "")
        inbox_addr = C["addr1"] if m and m["inbox_id"] == C["inbox1"] else C["addr2"]
        lu = c.header("List-Unsubscribe") or ""
        mid = m["message_id"] if m else "?"
        if not (from_addr and from_addr.group(1).lower() == inbox_addr.lower()):
            bad.append((c.to, "From", c.header("From")))
        if (c.header("Reply-To") or "").lower().find(inbox_addr.lower()) < 0:
            bad.append((c.to, "Reply-To", c.header("Reply-To")))
        if f"/tracking/unsubscribe/{mid}>" not in lu or f"mailto:{inbox_addr}" not in lu:
            bad.append((c.to, "List-Unsubscribe", lu))
        if c.header("List-Unsubscribe-Post") != "List-Unsubscribe=One-Click":
            bad.append((c.to, "List-Unsubscribe-Post", c.header("List-Unsubscribe-Post")))
        if not c.header("Date") or not (c.header("Message-ID") or "").endswith("@" + C["dom"] + ">"):
            bad.append((c.to, "Date/Message-ID", c.header("Message-ID")))
    R.check("E2E-J1-29", "MIME headers: From=mailbox, Reply-To, List-Unsubscribe (URL with message id + mailto), "
            "List-Unsubscribe-Post one-click, Message-ID on the sender domain", not bad, f"{bad[:3]}")
    bad = []
    for c in got:
        first = c.to.split(".")[0].capitalize()
        body = c.text
        if C["postal"] not in body or body.count(C["postal"]) != 1:
            bad.append((c.to, "postal address count", body.count(C["postal"])))
        if "{{" in body or "{{" in c.subject or "<<" in body:
            bad.append((c.to, "unresolved placeholder"))
        if first not in body:
            bad.append((c.to, "first name missing", first))
        if "Corp" not in body:
            bad.append((c.to, "company missing"))
    R.check("E2E-J1-30", "Body: tenant's postal address exactly once (CAN-SPAM footer), contact first name + company resolved, "
            "no placeholders", not bad, f"{bad[:3]}")
    sig_runs = [len(re.findall(r"(?mi)^regards,\s*$", c.text)) for c in got]
    signer = [("Priya Sharma" in c.text) for c in got]
    R.check("E2E-J1-31", "Signature: single sign-off using the campaign's sender name; footer not followed by a second signature",
            all(n == 1 for n in sig_runs) and all(signer),
            f"'Regards,' count per email={sorted(set(sig_runs))}; campaign sender 'Priya Sharma' present={sum(signer)}/16; "
            f"sample:\n{got[0].text if got else ''}", "defect")
    unsub_links = [c.unsubscribe_url for c in got]
    R.check("E2E-J1-32", "Every email carries a unique unsubscribe URL built from BASE_URL (/api/tracking/unsubscribe/<message id>)",
            all(u and "/api/tracking/unsubscribe/" in u for u in unsub_links) and len(set(unsub_links)) == 16,
            f"{unsub_links[:2]}")
    # unsubscribed footer mode: plain => reply keyword text, no link in body
    R.check("E2E-J1-33", "Unsubscribe_mode 'plain': body explains how to unsubscribe (reply) and the link lives in List-Unsubscribe",
            all("reply 'unsubscribe'" in c.text.lower() for c in got), f"{got[0].text[-300:] if got else ''}")
    s, ev = req("GET", f"/campaigns/{cid}", token=ws.token)
    R.check("E2E-J1-34", "Campaign counters after the send: sent_count == 16",
            ev["metrics"]["sent_count"] == 16, f"{ev['metrics']}")


OPENERS = [0, 1, 2, 4, 5, 8, 9, 10, 11, 12]       # 10 distinct contacts open
CLICKERS = [0, 4, 10, 11]                         # 4 distinct contacts click (contact 0 two links)


def _post_twice(first, ev):
    a = ses_post(ev)
    b = ses_post(ev)
    return a, b


def phase_events(R, C):
    """J1-35..J1-46: SES Delivery/Open/Click/Bounce events through the webhook (+ replays), analytics must agree."""
    ws, cid = C["w"], C["cid"]
    emails = C["emails"]
    sent = {m["email"]: m for m in msgs(cid, 1, "SENT")}
    src = {m["email"]: (C["addr1"] if m["inbox_id"] == C["inbox1"] else C["addr2"]) for m in sent.values()}
    C["step1"] = {e: {"message_id": m["message_id"], "ses_id": m["ses_id"], "pm_id": m["pm_id"], "inbox_id": m["inbox_id"],
                      "src": src[m["email"]]} for e, m in sent.items()}
    resp = {"deliv": [], "dup": []}
    bounce_i = ROLE["bounce"]
    for i, e in enumerate(emails):
        m = sent[e.lower()]
        if i == bounce_i:
            continue
        a, b = _post_twice(1, ev_delivery(m["message_id"], m["ses_id"], src[e.lower()], e.lower()))
        resp["deliv"].append(a[1].get("status"))
        resp["dup"].append(b[1].get("status"))
    R.check("E2E-J1-35", "Delivery events for 15 messages accepted ('processed'); an immediate replay of each is 'duplicate'",
            resp["deliv"] == ["processed"] * 15 and resp["dup"] == ["duplicate"] * 15, f"{set(resp['deliv'])} {set(resp['dup'])}")
    opens = []
    for k, i in enumerate(OPENERS):
        e = emails[i].lower()
        m = sent[e]
        ev = ev_open(m["message_id"], m["ses_id"], src[e], e)
        a, b = _post_twice(1, ev)
        opens.append((a[1].get("status"), b[1].get("status")))
    m0 = sent[emails[0].lower()]
    ses_post(ev_open(m0["message_id"], m0["ses_id"], src[emails[0].lower()], emails[0].lower(), stamp=iso(-3)))   # re-open later
    clicks = []
    for i in CLICKERS:
        e = emails[i].lower()
        m = sent[e]
        links = ["https://example.com/pricing"] + (["https://example.com/demo"] if i == 0 else [])
        for ln in links:
            ev = ev_click(m["message_id"], m["ses_id"], src[e], e, link=ln)
            a, b = _post_twice(1, ev)
            clicks.append((a[1].get("status"), b[1].get("status")))
    R.check("E2E-J1-36", "Open (10 contacts) and Click (4 contacts, 5 links) events processed once; replays are 'duplicate'",
            all(o == ("processed", "duplicate") for o in opens) and all(c == ("processed", "duplicate") for c in clicks),
            f"opens={set(opens)} clicks={set(clicks)}")
    # hard bounce for one contact
    be = emails[bounce_i].lower()
    mb = sent[be]
    a, b = _post_twice(1, ev_bounce(mb["message_id"], mb["ses_id"], src[be], be))
    R.check("E2E-J1-37", "Hard bounce event processed; replay ignored", a[1].get("status") == "processed" and b[1].get("status") == "duplicate",
            f"{a} {b}")
    ev_counts = dict(rows(f"SELECT e.event_type, COUNT(*) FROM email_events e JOIN email_messages m ON m.message_id=e.message_id "
                          f"WHERE m.campaign_id={q(cid)} GROUP BY e.event_type"))
    name_of = {r[0]: r[1] for r in rows("SELECT 1, 'x' WHERE 1=0")}
    C["ev_counts"] = ev_counts
    # event rows by name
    byname = {}
    EVN = {"1": "SENT", "2": "OPEN", "3": "CLICK", "4": "REPLY", "5": "BOUNCE", "6": "UNSUBSCRIBE", "7": "SENDER_BOUNCE",
           "8": "POSITIVE_REPLY", "9": "REPLY_OOO", "10": "DELIVERED"}   # email_events.event_type is an integer column
    for t, n in rows(f"SELECT e.event_type, COUNT(*) FROM email_events e JOIN email_messages m ON m.message_id=e.message_id "
                     f"WHERE m.campaign_id={q(cid)} GROUP BY e.event_type"):
        byname[EVN.get(t, t)] = int(n)
    R.check("E2E-J1-38", "Event rows exactly match the posts: 15 delivered, 11 open rows (10 contacts + 1 re-open), 5 click rows, 1 bounce",
            byname.get("DELIVERED") == 15 and byname.get("OPEN") == 11 and byname.get("CLICK") == 5 and byname.get("BOUNCE") == 1,
            f"{byname}")
    final = dict(rows(f"SELECT IFNULL(final_status,'NULL'), COUNT(*) FROM email_messages WHERE campaign_id={q(cid)} "
                      f"AND direction='OUTBOUND' AND status IN ('SENT','BOUNCED') GROUP BY final_status"))
    R.check("E2E-J1-39", "Message final_status: 15 DELIVERED, 1 BOUNCED (BR-DF-04)",
            final.get("DELIVERED") == "15" and final.get("BOUNCED") == "1", f"{final}")
    be_c = scalar(f"SELECT status FROM campaign_prospects WHERE campaign_id={q(cid)} AND prospect_id={q(C['pid'][emails[bounce_i]])}")
    pend = msgs(cid, status=["SCHEDULED", "QUEUED"], email_addr=be)
    supp = scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(ws.tenant_id)} AND LOWER(email)={q(be)}")
    s, camp = req("GET", f"/campaigns/{cid}", token=ws.token)
    R.check("E2E-J1-40", "Hard bounce: contact suppressed, enrolment BOUNCED, steps 2+3 not sendable; campaign (1/16) NOT auto-paused",
            supp == "1" and be_c == "BOUNCED" and not pend and camp["status"] == "ACTIVE" and not camp.get("auto_paused"),
            f"supp={supp} enrol={be_c} pending={[(m['step'], m['status']) for m in pend]} camp={camp['status']}/{camp.get('auto_paused')}")
    time.sleep(1)
    s, camp = req("GET", f"/campaigns/{cid}", token=ws.token)
    cm = camp["metrics"]
    s, mm = req("GET", f"/campaigns/{cid}/metrics", token=ws.token)
    s, an = req("GET", f"/campaigns/{cid}/analytics", token=ws.token)
    s, ds = req("GET", "/reports/dashboard-summary", token=ws.token)
    s, ga = req("GET", "/reports/global-analytics", token=ws.token)
    s, cc = req("GET", "/reports/campaign-comparison", token=ws.token)
    k = ds["kpis"]
    seq1 = an["sequences"][0]
    ci = [i for i in cc["items"] if i["campaign_id"] == cid][0]
    R.check("E2E-J1-41", "Campaign detail metrics: sent 16, opened 10 (unique contacts), bounced 1",
            cm["sent_count"] == 16 and cm["opened_count"] == 10 and cm["bounced_count"] == 1, f"{cm}")
    R.check("E2E-J1-42", "GET /campaigns/{id}/metrics agrees with the campaign detail (opened 10, bounced 1, open rate 10/16)",
            mm["sent_count"] == 16 and mm["opened_count"] == 10 and mm["bounced_count"] == 1
            and abs(mm["open_rate"] - 62.5) < 0.1, f"{mm} (campaign detail says {cm})")
    R.check("E2E-J1-43", "Analytics step 1: sent 16, opened 10, bounced 1, open rate 62.5, click rate 40.0 (4 of 10 openers)",
            seq1["sent"] == 16 and seq1["opened_count"] == 10 and seq1["bounced_count"] == 1
            and abs(seq1["open_rate"] - 62.5) < 0.1 and abs(seq1["click_rate"] - 40.0) < 0.1, f"{seq1}")
    R.check("E2E-J1-44", "Dashboard summary (tenant total) matches: 16 sent, 10 opened, 4 clicked, 1 bounced, 16 contacted",
            k["total_emails_sent"] == 16 and k["total_opened"] == 10 and k["total_clicked"] == 4 and k["total_bounced"] == 1
            and k["total_leads_contacted"] == 16, f"{k}")
    R.check("E2E-J1-45", "Global analytics KPIs equal the dashboard summary", ga["kpis"] == k, f"{ga['kpis']} vs {k}")
    R.check("E2E-J1-46", "Campaign comparison row agrees (sent 16, opened 10, bounced 1)",
            ci["sent_count"] == 16 and ci["opened_count"] == 10 and ci["bounced_count"] == 1, f"{ci}")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    bar = [b for b in an["bar_data"] if b["date"] == today]
    R.check("E2E-J1-47", "Analytics time-series for today: sent 16 / opened 10 / bounced 1",
            bar and bar[0]["sent"] == 16 and bar[0]["opened"] == 10 and bar[0]["bounced"] == 1, f"{bar}")


def _form_fields(html_text):
    """Hidden inputs of the first POST form on a page (what a browser would submit)."""
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', html_text))


def phase_unsub(R, C):
    """J1-48..J1-57: follow unsubscribe links from REAL captured emails."""
    ws, cid = C["w"], C["cid"]
    emails = C["emails"]
    cap = captured_for(emails=emails, domain=C["dom"])
    by_to = {c.to: c for c in cap}
    u1 = emails[ROLE["unsub_link"]].lower()
    u2 = emails[ROLE["unsub_oneclick"]].lower()
    url = by_to[u1].unsubscribe_url
    pid1 = C["pid"][emails[ROLE["unsub_link"]]]
    s, page = follow("GET", url)
    consent_after_get = scalar(f"SELECT consent_status FROM prospects WHERE prospect_id={q(pid1)}")
    supp_get = scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(ws.tenant_id)} AND LOWER(email)={q(u1)}")
    R.check("E2E-J1-48", "GET on the real unsubscribe link shows a confirmation page and changes nothing (link scanners are safe)",
            s == 200 and "<form" in page and "Unsubscribe" in page and supp_get == "0" and consent_after_get != "UNSUBSCRIBED",
            f"{s} supp={supp_get} consent={consent_after_get} {page[:150]!r}")
    fields = _form_fields(page)
    s, done = follow("POST", url, form=fields)
    supp = scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(ws.tenant_id)} AND LOWER(email)={q(u1)}")
    consent = scalar(f"SELECT consent_status FROM prospects WHERE prospect_id={q(pid1)}")
    enrol = scalar(f"SELECT status FROM campaign_prospects WHERE campaign_id={q(cid)} AND prospect_id={q(pid1)}")
    R.check("E2E-J1-49", "POST (the page's button) unsubscribes: suppression row, consent UNSUBSCRIBED, enrolment UNSUBSCRIBED",
            s == 200 and "Unsubscribed successfully" in done and supp == "1" and consent == "UNSUBSCRIBED" and enrol == "UNSUBSCRIBED",
            f"{s} supp={supp} consent={consent} enrol={enrol}")
    # RFC 8058 one-click: exactly what a mail client POSTs
    url2 = by_to[u2].unsubscribe_url
    s, done2 = follow("POST", url2, form={"List-Unsubscribe": "One-Click"})
    pid2 = C["pid"][emails[ROLE["unsub_oneclick"]]]
    supp2 = scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(ws.tenant_id)} AND LOWER(email)={q(u2)}")
    src2 = scalar(f"SELECT reason FROM global_unsubscribes WHERE tenant_id={q(ws.tenant_id)} AND LOWER(email)={q(u2)}")
    R.check("E2E-J1-50", "RFC 8058 one-click POST ('List-Unsubscribe=One-Click') unsubscribes without a confirmation step",
            s == 200 and supp2 == "1" and "One-click" in (src2 or ""), f"{s} supp={supp2} reason={src2}")
    for key, e in (("unsub_link", u1), ("unsub_oneclick", u2)):
        pend = msgs(cid, status=["SCHEDULED", "QUEUED"], email_addr=e)
        canc = msgs(cid, status="CANCELLED", email_addr=e)
        R.check(f"E2E-J1-5{1 if key == 'unsub_link' else 2}",
                f"Unsubscribed contact ({key}): steps 2 and 3 are CANCELLED, nothing left pending",
                not pend and sorted(m["step"] for m in canc) == ["2", "3"], f"pending={len(pend)} cancelled={[m['step'] for m in canc]} {[m['failure'] for m in canc]}")
    ev = scalar(f"SELECT COUNT(*) FROM email_events WHERE message_id={q(C['step1'][u1]['message_id'])} AND event_type=6")
    s, camp = req("GET", f"/campaigns/{cid}", token=ws.token)
    R.check("E2E-J1-53", "Unsubscribe is an event on the original message and counted in the campaign metrics (2)",
            ev == "1" and camp["metrics"]["unsubscribed_count"] == 2, f"event rows={ev} metrics={camp['metrics']}")
    # suppressed contact is rejected everywhere
    s, rej = req("POST", f"/campaigns/{cid}/prospects", {"prospect_ids": [pid1]}, ws.token)
    R.check("E2E-J1-54", "Re-enrolling an unsubscribed contact is refused with an 'unsubscribed' reason",
            s == 200 and rej.get("enrolled_count", 0) == 0 and "unsub" in json.dumps(rej).lower(), f"{s} {str(rej)[:300]}")
    v, c = upload_list_dry(ws, f"First Name,Last Name,Company Name,email,Designation\nCy,Cole,Bravo Corp,{u1.upper()},CEO\n")
    r0 = v["records"][0]
    R.check("E2E-J1-55", "Uploading the unsubscribed address again is rejected as unsubscribed",
            r0["status"] == "REJECTED" and "unsubscribed" in (r0["reason"] or "").lower(), f"{r0['status']} {r0['reason']}")
    # the scheduler never sends to them: pull their remaining steps due anyway
    travel_due(cid, 2, emails=[u1, u2])
    time.sleep(70)
    leaked = captured_for(emails=[u1, u2], domain=C["dom"])
    R.check("E2E-J1-56", "Worker sends nothing further to unsubscribed contacts even when their follow-up is forced due (only the step-1 email exists)",
            len(leaked) == 2, f"captured={[(c.to, c.subject) for c in leaked]}")
    s, tl = req("GET", f"/contacts/{pid1}/timeline", token=ws.token)
    R.check("E2E-J1-57", "Contact timeline records the unsubscribe",
            s == 200 and "unsub" in json.dumps(tl).lower(), f"{s} {str(tl)[:300]}")


def upload_list_dry(ws, csv):
    from .helpers import upload_csv
    s, v = upload_csv(ws.token, "again.csv", csv)
    assert s == 200, (s, v)
    return v, None


def _inbound_for(pid):
    r = rows(f"SELECT message_id, IFNULL(conversation_id,''), subject, body_text, from_email, to_email FROM email_messages "
             f"WHERE prospect_id={q(pid)} AND direction='INBOUND' ORDER BY sent_at")
    return [dict(zip(("message_id", "conversation_id", "subject", "body", "from", "to"), x)) for x in r]


def phase_replies(R, C):
    """J1-58..J1-72: replies arrive in the fake IMAP mailbox; the real IMAP sync ingests them."""
    ws, cid = C["w"], C["cid"]
    emails = C["emails"]
    imap = C["_imap"]
    addr_of = {C["inbox1"]: C["addr1"], C["inbox2"]: C["addr2"]}

    def reply(i, subject, body, **kw):
        e = emails[i].lower()
        st = C["step1"][e]
        imap.deliver(addr_of[st["inbox_id"]], reply_mime(e, addr_of[st["inbox_id"]], subject, body, st["pm_id"], **kw))
        return e

    r_e = reply(ROLE["replier"], "Re: Question for Ed", "Hi Priya, this looks interesting. Can we talk on Tuesday about pricing for 40 seats?")
    o_e = reply(ROLE["ooo"], "Automatic reply: Out of Office", "I am out of the office until Monday with limited access to email.",
                auto_submitted="auto-replied")
    v_e = reply(ROLE["reply_unsub"], "Re: Question for Gus", "unsubscribe")
    foreign = f"nobody-{C['rid']}@unknown-{C['rid']}.example"
    imap.deliver(C["addr1"], reply_mime(foreign, C["addr1"], "Hello", "Who is this?"))
    n_conv0 = scalar(f"SELECT COUNT(*) FROM conversations WHERE tenant_id={q(ws.tenant_id)}")
    for ib in (C["inbox1"], C["inbox2"]):
        s, b = req("POST", f"/inboxes/{ib}/sync?days=7", None, ws.token)
        assert s == 200, (s, b)
    pid = C["pid"]
    rin = _inbound_for(pid[emails[ROLE["replier"]]])
    out1 = C["step1"][r_e]
    out_conv = scalar(f"SELECT IFNULL(conversation_id,'') FROM email_messages WHERE message_id={q(out1['message_id'])}")
    R.check("E2E-J1-58", "IMAP sync ingests the reply: one INBOUND message with the exact body, from the contact to the mailbox",
            len(rin) == 1 and "pricing for 40 seats" in rin[0]["body"] and rin[0]["from"].lower() == r_e
            and rin[0]["to"].lower() == addr_of[out1["inbox_id"]].lower(), f"{rin}")
    R.check("E2E-J1-59", "Reply is threaded into the same conversation as the outbound step-1 email (matched by In-Reply-To)",
            rin and out_conv and rin[0]["conversation_id"] == out_conv,
            f"inbound conv={rin[0]['conversation_id'] if rin else None} outbound conv={out_conv}")
    s, convs = req("GET", "/conversations?page_size=100", token=ws.token)
    mine = [c for c in convs.get("items", []) if c.get("prospect_id") == pid[emails[ROLE["replier"]]]]
    R.check("E2E-J1-60", "Unified inbox lists the conversation, unread, last message INBOUND",
            mine and mine[0].get("last_message_direction") == "INBOUND" and mine[0].get("is_unread") in (True, 1),
            f"{mine[:1]}")
    enrol = scalar(f"SELECT status FROM campaign_prospects WHERE campaign_id={q(cid)} AND prospect_id={q(pid[emails[ROLE['replier']]])}")
    pend = msgs(cid, status=["SCHEDULED", "QUEUED"], email_addr=r_e)
    canc = msgs(cid, status="CANCELLED", email_addr=r_e)
    R.check("E2E-J1-61", "Reply stops that contact's sequence: enrolment REPLIED, steps 2+3 CANCELLED ('contact replied')",
            enrol == "REPLIED" and not pend and sorted(m["step"] for m in canc) == ["2", "3"]
            and all("replied" in m["failure"].lower() for m in canc), f"enrol={enrol} pend={len(pend)} canc={[(m['step'], m['failure']) for m in canc]}")
    ooo_enrol = scalar(f"SELECT status FROM campaign_prospects WHERE campaign_id={q(cid)} AND prospect_id={q(pid[emails[ROLE['ooo']]])}")
    ooo_pend = msgs(cid, status=["SCHEDULED", "QUEUED"], email_addr=o_e)
    R.check("E2E-J1-62", "Out-of-office auto-reply is stored but does NOT stop the sequence (still ACTIVE, steps 2+3 pending)",
            len(_inbound_for(pid[emails[ROLE['ooo']]])) == 1 and ooo_enrol == "ACTIVE" and len(ooo_pend) == 2,
            f"inbound={len(_inbound_for(pid[emails[ROLE['ooo']]]))} enrol={ooo_enrol} pending={len(ooo_pend)}")
    supp = scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(ws.tenant_id)} AND LOWER(email)={q(v_e)}")
    consent = scalar(f"SELECT consent_status FROM prospects WHERE prospect_id={q(pid[emails[ROLE['reply_unsub']]])}")
    v_pend = msgs(cid, status=["SCHEDULED", "QUEUED"], email_addr=v_e)
    R.check("E2E-J1-63", "Reply 'unsubscribe' suppresses the contact workspace-wide and stops the sequence",
            supp == "1" and consent == "UNSUBSCRIBED" and not v_pend, f"supp={supp} consent={consent} pending={len(v_pend)}")
    R.check("E2E-J1-64", "Mail from an unknown sender is not turned into a prospect/conversation of this workspace",
            scalar(f"SELECT COUNT(*) FROM email_messages WHERE LOWER(from_email)={q(foreign)}") == "0"
            or scalar(f"SELECT COUNT(*) FROM conversations WHERE tenant_id={q(ws.tenant_id)}") == str(int(n_conv0) + 3),
            f"inbound rows from stranger={scalar(f'SELECT COUNT(*) FROM email_messages WHERE LOWER(from_email)={q(foreign)}')}")
    ev = dict(rows(f"SELECT e.event_type, COUNT(*) FROM email_events e JOIN email_messages m ON m.message_id=e.message_id "
                   f"WHERE m.campaign_id={q(cid)} AND e.event_type IN (4,8,9) GROUP BY e.event_type"))
    s, camp = req("GET", f"/campaigns/{cid}", token=ws.token)
    cm = camp["metrics"]
    R.check("E2E-J1-65", "Reply analytics: 3 reply events (1 genuine, 1 out-of-office, 1 unsubscribe), 1 OOO; campaign counters match",
            cm["replied_count"] == 3 and cm["ooo_count"] == 1, f"events={ev} metrics={cm}")
    s, an = req("GET", f"/campaigns/{cid}/analytics", token=ws.token)
    s2, ds = req("GET", "/reports/dashboard-summary", token=ws.token)
    R.check("E2E-J1-66", "Step-1 analytics and dashboard summary show the same reply numbers",
            an["sequences"][0]["replied_count"] == cm["replied_count"] and ds["kpis"]["total_replied"] == cm["replied_count"],
            f"seq1={an['sequences'][0]['replied_count']} dash={ds['kpis']['total_replied']} camp={cm['replied_count']}")
    # positive-reply classification path without an LLM
    pos = scalar(f"SELECT COUNT(*) FROM email_events e JOIN email_messages m ON m.message_id=e.message_id "
                 f"WHERE m.campaign_id={q(cid)} AND e.event_type=8")
    s, sug = req("GET", "/leads/reply-suggestions", token=C["users"]["mgr"]["token"])
    R.record("E2E-J1-67", "Positive-reply classification for 'looks interesting... pricing for 40 seats'", None,
             f"OPENAI_API_KEY is not set: classify_reply_intent() returns None, so no POSITIVE_REPLY event ({pos}) and "
             f"/leads/reply-suggestions is {len(sug.get('items', []))} item(s). The app has no keyword fallback; the reply is ingested normally.",
             "environment")
    R.check("E2E-J1-68", "Without an LLM the reply is still ingested and the rest of the chain is unaffected (no POSITIVE event is invented)",
            pos == "0" and s == 200, f"{pos} {s}")
    C["replier_inbound"] = rin[0]["message_id"]


def phase_bg_reply(R, C):
    """J1-69: the WORKER's background IMAP loop (no API call) must pick up a reply."""
    ws, cid = C["w"], C["cid"]
    emails = C["emails"]
    imap = C["_imap"]
    e = emails[ROLE["bg_reply"]].lower()
    st = C["step1"][e]
    addr = C["addr1"] if st["inbox_id"] == C["inbox1"] else C["addr2"]
    logins_before = len([x for x in imap.logins if x == (addr.lower(), True)])
    imap.deliver(addr, reply_mime(e, addr, "Re: Question for Ida", "Yes please send the details and a quote.", st["pm_id"]))
    pid = C["pid"][emails[ROLE["bg_reply"]]]
    t0 = time.time()
    ok = wait_until(lambda: len(_inbound_for(pid)) >= 1, timeout=620, interval=5)
    after = len([x for x in imap.logins if x == (addr.lower(), True)])
    R.check("E2E-J1-69", "Worker's background IMAP loop (no sync API call) logs in and ingests the reply within 10 minutes (BR-DF-05)",
            ok and after > logins_before, f"ingested={bool(ok)} after {round(time.time() - t0)}s; mailbox logins {logins_before}->{after}")
    enrol = scalar(f"SELECT status FROM campaign_prospects WHERE campaign_id={q(cid)} AND prospect_id={q(pid)}")
    R.check("E2E-J1-70", "Background-ingested reply also stops the sequence (enrolment REPLIED)", enrol == "REPLIED", f"{enrol}")




def _stages(ws):
    s, b = req("GET", "/sales/stages", token=ws.token)
    items = b if isinstance(b, list) else b.get("items") or b.get("stages") or []
    return {x["name"]: x for x in items}


def phase_sales(R, C):
    """J1-71..J1-92: reply -> lead -> BANT -> SQL -> deal -> stages -> won; lifecycle, history, notifications."""
    ws, cid = C["w"], C["cid"]
    u = C["users"]
    emails = C["emails"]
    pid = C["pid"][emails[ROLE["replier"]]]
    C["won_pid"] = pid
    a1, a2, mgr = u["a1"], u["a2"], u["mgr"]
    s, l = req("POST", "/leads/from-message", {"message_id": C["replier_inbound"], "owner_id": a1["user_id"]}, mgr["token"])
    R.check("E2E-J1-71", "Manager turns the reply into a lead owned by agent 1 (one click): ENGAGED, source 'Campaign reply', campaign attached",
            s == 201 and l.get("stage") == "ENGAGED" and l.get("source") == "Campaign reply" and l.get("campaign_id") == cid
            and l.get("owner_id") == a1["user_id"] and "pricing" in json.dumps(l.get("qualification")), f"{s} {str(l)[:400]}")
    lid = l.get("lead_id")
    C["lead_id"] = lid
    s, dup = req("POST", "/leads/from-message", {"message_id": C["replier_inbound"]}, mgr["token"])
    R.check("E2E-J1-72", "Second lead for the same open contact is refused (409 with the existing lead id)",
            s == 409 and dup["detail"].get("lead_id") == lid, f"{s} {dup}")
    vis = {k: req("GET", f"/leads/{lid}", token=u[k]["token"])[0] for k in ("owner", "mgr", "a1", "a2")}
    R.check("E2E-J1-73", "Lead visibility follows the hierarchy: owner/manager/agent1 see it, agent2 gets 404",
            vis == {"owner": 200, "mgr": 200, "a1": 200, "a2": 404}, f"{vis}")
    s, n = req("GET", "/notifications", token=a1["token"])
    items = n.get("items", []) if isinstance(n, dict) else n
    R.check("E2E-J1-74", "Agent 1 is notified of the assignment ('New lead: <contact>')",
            any("New lead" in (x.get("title") or "") for x in items), f"{str(n)[:300]}")
    s, bad = req("PATCH", f"/leads/{lid}", {"stage": "SQL"}, a1["token"])
    R.check("E2E-J1-75", "Cannot mark SQL before BANT is confirmed (400 lists the missing criteria)",
            s == 400 and "Budget" in str(bad), f"{s} {bad}")
    s, b = req("PATCH", f"/leads/{lid}", {"qualification": {"budget": True, "authority": True, "need": True, "timeline": True,
                                                            "notes": "40 seats, Q4 budget"}}, a1["token"])
    s2, b2 = req("PATCH", f"/leads/{lid}", {"stage": "SQL"}, a1["token"])
    R.check("E2E-J1-76", "After BANT the agent qualifies the lead: stage SQL with qualified_at set",
            s == 200 and s2 == 200 and b2["stage"] == "SQL" and b2["qualified_at"], f"{s} {s2} {str(b2)[:300]}")
    q_m = [x["lead_id"] for x in req("GET", "/leads/sql-queue", token=mgr["token"])[1].get("items", [])]
    q_a2 = [x["lead_id"] for x in req("GET", "/leads/sql-queue", token=a2["token"])[1].get("items", [])]
    q_a1 = [x["lead_id"] for x in req("GET", "/leads/sql-queue", token=a1["token"])[1].get("items", [])]
    R.check("E2E-J1-77", "SQL queue: manager and agent 1 see the lead, agent 2 does not",
            lid in q_m and lid in q_a1 and lid not in q_a2, f"mgr={lid in q_m} a1={lid in q_a1} a2={lid in q_a2}")
    st = _stages(ws)
    C["stage_ids"] = {k: v["stage_id"] for k, v in st.items()}
    close = date.today() + timedelta(days=45)
    s, opp = req("POST", f"/leads/{lid}/convert", {"name": f"Egan Corp - 40 seats {C['rid']}", "amount": 24000,
                                                   "close_date": str(close)}, a1["token"])
    R.check("E2E-J1-78", "Convert SQL -> opportunity: amount 24000, close date, owner agent 1, first open stage, attributed to the campaign",
            s == 201 and opp["amount"] == 24000 and opp["owner_id"] == a1["user_id"] and opp.get("campaign_id") == cid
            and opp["status"] == "OPEN" and opp["close_date"] == str(close), f"{s} {str(opp)[:400]}")
    oid = opp.get("opportunity_id")
    C["opp_id"] = oid
    _, lead_after = req("GET", f"/leads/{lid}", token=a1["token"])
    _, con = req("GET", f"/contacts/{pid}", token=a1["token"])
    R.check("E2E-J1-79", "Lead becomes CONVERTED linked to the deal; contact lifecycle moves to OPPORTUNITY",
            lead_after["stage"] == "CONVERTED" and lead_after["opportunity_id"] == oid
            and con.get("lifecycle_stage") == "OPPORTUNITY", f"{lead_after['stage']} {con.get('lifecycle_stage')}")
    # stage moves with amount/close date change
    new_close = close + timedelta(days=20)
    path = []
    for name, extra in (("Needs analysis", {}), ("Proposal", {"amount": 30000}), ("Negotiation", {"close_date": str(new_close)})):
        s, b = req("PATCH", f"/opportunities/{oid}", {"stage_id": st[name]["stage_id"], **extra}, a1["token"])
        path.append((name, s, b.get("status"), b.get("amount"), b.get("close_date")))
    R.check("E2E-J1-80", "Deal moves Qualification -> Needs analysis -> Proposal (amount 30000) -> Negotiation (new close date)",
            [p[1] for p in path] == [200] * 3 and path[-1][3] == 30000 and path[-1][4] == str(new_close), f"{path}")
    s, d = req("GET", f"/opportunities/{oid}", token=a1["token"])
    fields = [h["field"] for h in d.get("history", [])]
    R.check("E2E-J1-81", "Deal history records stage_id x3, amount and close_date changes with the author's name",
            fields.count("stage_id") >= 3 and "amount" in fields and "close_date" in fields
            and all(h.get("changed_by_name") for h in d["history"]), f"{fields}")
    s, bad = req("PATCH", f"/opportunities/{oid}", {"stage_id": st["Closed won"]["stage_id"]}, a1["token"])
    R.check("E2E-J1-82", "Closing as won without a reason is refused (400)", s == 400 and "why" in str(bad).lower(), f"{s} {bad}")
    s, won = req("PATCH", f"/opportunities/{oid}", {"stage_id": st["Closed won"]["stage_id"],
                                                    "closed_reason": "Champion + budget; signed for 40 seats"}, a1["token"])
    R.check("E2E-J1-83", "Won with a reason: status WON, closed_at set, reason stored, probability 100",
            s == 200 and won["status"] == "WON" and won.get("closed_at") and won.get("closed_reason", "").startswith("Champion")
            and won.get("probability", 100) == 100, f"{s} {str(won)[:300]}")
    _, con = req("GET", f"/contacts/{pid}", token=a1["token"])
    R.check("E2E-J1-84", "Contact lifecycle is CUSTOMER after the win", con.get("lifecycle_stage") == "CUSTOMER", f"{con.get('lifecycle_stage')}")
    s, hist = req("GET", f"/contacts/{pid}/history", token=a1["token"])
    lc = [h for h in (hist if isinstance(hist, list) else hist.get("items", [])) if h.get("field") == "lifecycle_stage"]
    R.check("E2E-J1-85", "Contact history shows lifecycle changes through the funnel (MQL -> SQL -> Opportunity -> Customer)",
            len(lc) >= 4 and (lc[0].get("new_value") or "").lower() == "customer"
            and [(h.get("new_value") or "").lower() for h in reversed(lc)][:4] == ["marketing qualified lead", "sales qualified lead", "opportunity", "customer"], f"{[(h.get('old_value'), h.get('new_value')) for h in lc]}")
    s, tl = req("GET", f"/opportunities/{oid}/timeline", token=a1["token"])
    R.check("E2E-J1-86", "Deal timeline is available to its owner and not to agent 2",
            s == 200 and req("GET", f"/opportunities/{oid}/timeline", token=a2["token"])[0] == 404, f"{s}")
    _, na = req("GET", "/notifications", token=a1["token"])
    _, nm = req("GET", "/notifications", token=mgr["token"])
    kinds_a = [x.get("kind") for x in na.get("items", [])]
    R.check("E2E-J1-87", "Agent 1 holds the assignment notification created on the way (ASSIGNED 'New lead: Ed Egan')",
            "ASSIGNED" in kinds_a, f"agent kinds={kinds_a}")
    R.check("E2E-J1-87b", "Notifications for the deal moving to Won / for the manager (stage changes, win)",
            any("won" in (x.get("title") or "").lower() for x in na.get("items", []) + nm.get("items", [])),
            f"no built-in notification is raised when a deal is won or changes stage (agent: {kinds_a}, manager: {len(nm.get('items', []))} item(s)); "
            f"only workflow rules can add one (see J6)", "not-implemented")


def phase_reports(R, C):
    """J1-88..J1-97: deal book + sales reports vs independently computed numbers; FY attribution; amount hiding."""
    ws = C["w"]
    u = C["users"]
    st = C["stage_ids"]
    a1, a2, mgr = u["a1"], u["a2"], u["mgr"]
    prob = {"Qualification": 10, "Needs analysis": 25, "Proposal": 50, "Negotiation": 75, "Closed won": 100, "Closed lost": 0}
    # (owner, name, stage, amount, close_date) -- fiscal year starts in April
    today = date.today()
    fy = today.year if today.month >= 4 else today.year - 1
    book = [
        ("a1", "B1", "Proposal", 10000, date(fy, 4, 1)),            # first day of FY Q1
        ("a1", "B2", "Negotiation", 20000, date(fy + 1, 3, 31)),    # last day of FY Q4
        ("a2", "B3", "Qualification", 5000, date(fy + 1, 4, 1)),    # first day of NEXT FY
        ("a2", "B4", "Needs analysis", 8000, date(fy, 9, 30)),      # last day of Q2
        ("a2", "B5", "Proposal", 12000, date(fy, 10, 1)),           # first day of Q3
    ]
    made = {}
    for who, name, stg, amt, cd in book:
        s, b = req("POST", "/opportunities", {"name": f"{name} {C['rid']}", "stage_id": st[stg], "amount": amt,
                                              "close_date": str(cd), "owner_id": u[who]["user_id"]}, mgr["token"])
        assert s == 201, (s, b)
        made[name] = b["opportunity_id"]
    s, b = req("POST", "/opportunities", {"name": f"B6 lost {C['rid']}", "stage_id": st["Closed lost"], "amount": 7000,
                                          "close_date": str(today), "owner_id": a2["user_id"], "closed_reason": "Chose competitor"}, mgr["token"])
    assert s == 201, (s, b)
    # independent expectations over the whole workspace: deal book + the won deal (30000, closed today)
    won_amt, won_close = 30000, None
    _, wd = req("GET", f"/opportunities/{C['opp_id']}", token=u["owner"]["token"])
    won_close = date.fromisoformat(wd["close_date"])
    deals = [(a, stg, amt, cd) for _, _, stg, amt, cd in [(0, 0, b_[2], b_[3], b_[4]) for b_ in book] for a in [0]]
    open_deals = [(stg, amt, cd) for _, _, stg, amt, cd in book]
    open_total = sum(a for _, a, _ in open_deals)
    weighted = sum(a * prob[s_] / 100 for s_, a, _ in open_deals)
    s, pr = req("GET", "/sales-reports/pipeline", token=ws.token)
    s2, rev = req("GET", "/pipeline/revenue", token=ws.token)
    t = rev["totals"]
    R.check("E2E-J1-88", "Pipeline revenue totals = independently computed open amount 55000 / weighted 17400 (+ won excluded)",
            abs(t["open_amount"] - open_total) < 0.01 and abs(t["weighted_amount"] - weighted) < 0.01,
            f"open={t['open_amount']} (exp {open_total}) weighted={t['weighted_amount']} (exp {weighted})")

    def fq(d):
        fyy = d.year if d.month >= 4 else d.year - 1
        return fyy, ((d.month - 4) % 12) // 3 + 1
    s, fc = req("GET", f"/sales-reports/forecast?fy={fy}", token=ws.token)
    exp_q = {}
    for stg, amt, cd in open_deals:
        k = fq(cd)
        e = exp_q.setdefault(k, {"pipeline": 0, "weighted": 0, "won": 0})
        e["pipeline"] += amt
        e["weighted"] += amt * prob[stg] / 100
    wk = fq(won_close)
    exp_q.setdefault(wk, {"pipeline": 0, "weighted": 0, "won": 0})["won"] += won_amt
    errs = []
    for qi, qd in enumerate(fc["quarters"], 1):
        e = exp_q.get((fy, qi), {"pipeline": 0, "weighted": 0, "won": 0})
        if abs(qd["pipeline"] - e["pipeline"]) > .01 or abs(qd["weighted"] - e["weighted"]) > .01 or abs(qd["won"] - e["won"]) > .01:
            errs.append((qd["quarter"], {k: qd[k] for k in ("pipeline", "weighted", "won")}, e))
    R.check("E2E-J1-89", "Forecast by FY quarter (April-March) matches independently bucketed deals incl. boundary dates (Apr 1, Mar 31, Sep 30, Oct 1)",
            s == 200 and not errs and fc["fy"] == fy, f"{errs}")
    nxt = [y for y in fc["years"] if y["fy"] == fy + 1][0]
    R.check("E2E-J1-90", "A deal closing 1 April next year lands in the next financial year (5000), not this one",
            abs(nxt["pipeline"] - 5000) < .01 and fc["total"]["pipeline"] == sum(e["pipeline"] for k, e in exp_q.items() if k[0] == fy),
            f"next={nxt['pipeline']} this={fc['total']['pipeline']}")
    s, tp = req("GET", "/sales-reports/team-performance", token=ws.token)
    reps = {r["user_id"]: r for r in tp["reps"]}
    r1, r2 = reps[a1["user_id"]], reps[a2["user_id"]]
    R.check("E2E-J1-91", "Team performance: agent1 won 1 deal / 30000, 1 lead, 1 SQL, 1 deal created+converted, open pipeline 30000; agent2 lost 1, open 25000",
            r1["won_count"] == 1 and r1["won_amount"] == 30000 and r1["leads"] == 1 and r1["sqls"] == 1
            and r1["open_pipeline"] == 30000 and r2["lost_count"] == 1 and r2["open_pipeline"] == 25000 and r2["won_count"] == 0
            and r1["sql_to_opportunity"] == 100.0, f"a1={ {k: r1[k] for k in ('won_count','won_amount','leads','sqls','open_pipeline')} } a2={ {k: r2[k] for k in ('lost_count','open_pipeline')} }")
    s, lb = req("GET", "/sales-reports/leaderboard", token=ws.token)
    rowsx = lb["rows"]
    R.check("E2E-J1-92", "Leaderboard ranks agent 1 first (30000 won) ahead of agent 2 (0)",
            rowsx and rowsx[0]["user_id"] == a1["user_id"], f"{[(r.get('name'), r.get('won_amount')) for r in rowsx[:3]]}")
    s, sd = req("GET", "/sales-reports/dashboard", token=ws.token)
    k = sd["kpis"]
    R.check("E2E-J1-93", "Sales dashboard KPIs: won 1 / 30000, lost 1, open pipeline 55000, weighted 17400, new leads 1, SQLs 1",
            k["won_count"] == 1 and k["won_amount"] == 30000 and k["lost_count"] == 1 and abs(k["open_pipeline"] - open_total) < .01
            and abs(k["weighted_pipeline"] - weighted) < .01 and k["new_leads"] == 1 and k["sqls"] == 1, f"{k}")
    s, mg = req("GET", "/sales-reports/dashboard", token=mgr["token"])
    s2, ag = req("GET", "/sales-reports/dashboard", token=a2["token"])
    R.check("E2E-J1-94", "Scoping: manager sees the team's numbers (same as owner), agent 2 sees only own (open 25000, won 0)",
            mg["kpis"]["won_amount"] == 30000 and ag["kpis"]["won_amount"] == 0 and ag["kpis"]["open_pipeline"] == 25000,
            f"mgr={mg['kpis']['won_amount']} a2 won={ag['kpis']['won_amount']} open={ag['kpis']['open_pipeline']}")
    # amount hiding
    req("PUT", "/sales/settings", {"amount_hidden_roles": ["AGENT"], "amount_hidden_levels": []}, ws.token)
    try:
        _, o1 = req("GET", f"/opportunities/{C['opp_id']}", token=a1["token"])
        _, lst = req("GET", "/opportunities", token=a2["token"])
        _, fca = req("GET", f"/sales-reports/forecast?fy={fy}", token=a2["token"])
        _, tpa = req("GET", "/sales-reports/team-performance", token=a2["token"])
        _, dsa = req("GET", "/sales-reports/dashboard", token=a2["token"])
        w = req("PATCH", f"/opportunities/{C['opp_id']}", {"amount": 1}, a1["token"])[0]
        _, om = req("GET", f"/opportunities/{C['opp_id']}", token=mgr["token"])
        hidden = (o1["amount"] is None and all(i["amount"] is None for i in lst["items"]) and lst["total_amount"] is None
                  and fca["total"]["pipeline"] is None and dsa["kpis"]["won_amount"] is None
                  and all(r["won_amount"] is None for r in tpa["reps"]))
        R.check("E2E-J1-95", "Amount hiding for AGENT role: deal, list, forecast, dashboard and team scorecard show no amounts; writing one is 403; manager still sees 30000",
                hidden and w == 403 and om["amount"] == 30000,
                f"deal={o1['amount']} list={lst['total_amount']} fc={fca['total']['pipeline']} dash={dsa['kpis']['won_amount']} "
                f"team={[r['won_amount'] for r in tpa['reps']][:3]} write={w} mgr={om['amount']}")
    finally:
        req("PUT", "/sales/settings", {"amount_hidden_roles": [], "amount_hidden_levels": []}, ws.token)


def phase_followups(R, C):
    """J1-96..J1-103: step 2 / 3 ordering (B01), held contact, pause/resume without burst."""
    ws, cid = C["w"], C["cid"]
    emails = [e.lower() for e in C["emails"]]
    dead = {emails[ROLE[k]] for k in ("unsub_link", "unsub_oneclick", "replier", "reply_unsub", "bounce", "bg_reply")}
    held = emails[ROLE["held"]]
    live = [e for e in emails if e not in dead]               # incl. OOO (continues) and held
    s1 = {m["email"]: m for m in msgs(cid, 1, "SENT")}
    s2 = {m["email"]: m for m in msgs(cid, 2, ["SCHEDULED", "QUEUED"])}
    bad = [e for e in live if e not in s2 or dt(s2[e]["scheduled_at"]) < dt(s1[e]["sent_at"]) + timedelta(days=2) - timedelta(seconds=90)]
    R.check("E2E-J1-96", "B01: every live contact's step 2 is scheduled >= step-1 SENT time + 2 days (spacing counts from the real send)",
            not bad, f"violations={bad[:3]} sample={[(e, s1[e]['sent_at'], s2[e]['scheduled_at']) for e in bad[:1] if e in s2]}")
    R.check("E2E-J1-97", "No follow-up is pending for dead contacts (unsubscribed / replied / bounced): 0 SCHEDULED rows",
            not [m for m in msgs(cid, status=["SCHEDULED", "QUEUED"]) if m["email"] in dead],
            f"{[ (m['email'][:12], m['step']) for m in msgs(cid, status=['SCHEDULED','QUEUED']) if m['email'] in dead][:5]}")
    # hold one contact: its step-1 is made unsent again (never delivered), step 2 forced due -> must NOT go out
    sql(f"UPDATE email_messages m JOIN email_sequences s ON s.sequence_id=m.sequence_id JOIN prospects p ON p.prospect_id=m.prospect_id "
        f"SET m.status='SCHEDULED', m.sent_at=NULL, m.scheduled_at=UTC_TIMESTAMP() + INTERVAL 1 DAY "
        f"WHERE m.campaign_id={q(cid)} AND s.step_number=1 AND LOWER(p.email)={q(held)}")
    n_before = len(captured_for(emails=[held], domain=C["dom"]))
    travel_due(cid, 2)
    ok = wait_until(lambda: len(msgs(cid, 2, "SENT")) >= len(live) - 1, timeout=360, interval=5)
    sent2 = msgs(cid, 2, "SENT")
    sent_to = {m["email"] for m in sent2}
    cap2 = [c for c in captured_for(emails=emails, domain=C["dom"]) if c.subject.lower().startswith("following up")]
    R.check("E2E-J1-98", "Step 2 goes only to the live contacts (13 minus the held one = 8): none to dead contacts, none to the contact whose step 1 is unsent",
            ok and sent_to == set(live) - {held} and len(cap2) == len(sent_to) and held not in {c.to for c in cap2},
            f"sent={len(sent_to)} expected={len(live) - 1} extra={sent_to - set(live)} held_sent={held in sent_to}")
    lat = [(m["email"], dt(m["sent_at"])) for m in sent2]
    ord_ok = all(dt(s1[e]["sent_at"]) < t for e, t in lat)
    nxt = {m["email"]: m for m in msgs(cid, 3, ["SCHEDULED", "QUEUED"])}
    spacing = [e for e, t in lat if e in nxt and dt(nxt[e]["scheduled_at"]) < t + timedelta(days=3) - timedelta(seconds=90)]
    R.check("E2E-J1-99", "Order and spacing: each step 2 was sent after its step 1, and step 3 is re-planned >= step-2 sent + 3 days",
            ord_ok and not spacing, f"order_ok={ord_ok} step3_too_early={spacing[:3]}")
    s2c = [c for c in cap2][0] if cap2 else None
    # the step-2 template is either the starter follow-up ("Following up for <company>") or, without starter templates,
    # the hand-made one ("Following up, <first name>"); either way it is its own template, rendered for the contact
    s2_db = rows(f"SELECT m.subject, p.first_name, p.company_name FROM email_messages m JOIN prospects p ON p.prospect_id=m.prospect_id "
                 f"JOIN email_sequences s ON s.sequence_id=m.sequence_id WHERE m.campaign_id={q(cid)} AND s.step_number=2 "
                 f"AND LOWER(p.email)={q(s2c.to.lower())}") if s2c else []
    R.check("E2E-J1-100", "Follow-up email content: its own step-2 template ('Following up ...'), contact personalised, footer + unsubscribe header present",
            s2c and s2_db and s2c.subject.lower().startswith("following up") and s2c.subject == s2_db[0][0]
            and "{{" not in s2c.subject + s2c.text and (s2_db[0][1] in s2c.subject + s2c.text or s2_db[0][2] in s2c.subject + s2c.text)
            and C["postal"] in s2c.text and s2c.unsubscribe_url,
            f"{s2c.subject if s2c else None} db={s2_db[:1]}")
    # pause / resume with no burst
    travel_due(cid, 3)
    s, p, pause_secs = timed("POST", f"/campaigns/{cid}/pause", None, ws.token)
    R.check("E2E-J1-101a", "Pause answers promptly (< 20 s) while the scheduler is working the campaign", s == 200 and pause_secs < 20,
            f"{s} took {pause_secs}s", "defect")
    time.sleep(75)
    sent3_paused = len(msgs(cid, 3, "SENT"))
    R.check("E2E-J1-101", "Paused campaign: step 3 forced due, yet nothing is sent while PAUSED",
            s == 200 and sent3_paused == 0, f"pause={s} {str(p)[:100]} sent_while_paused={sent3_paused}")
    travel_due(cid, 3)
    s, r = req("POST", f"/campaigns/{cid}/resume", None, ws.token)
    pend3 = msgs(cid, 3, ["SCHEDULED", "QUEUED", "PAUSED_BY_CAMPAIGN"])
    lanes = {}
    for m in pend3:
        if m["email"] == held:      # its step 1 was deliberately held back: only rebased contacts are re-spread
            continue
        lanes.setdefault(m["inbox_id"], []).append(dt(m["scheduled_at"]))
    gaps = [(b - a).total_seconds() for ts in lanes.values() for a, b in zip(sorted(ts), sorted(ts)[1:])]
    R.check("E2E-J1-102", "Resume does not burst: overdue step-3 messages are re-spread from now, one per slot per mailbox lane (>= campaign min gap of 120 s apart)",
            s == 200 and len(pend3) >= 7 and gaps and min(gaps) >= 119, f"resume={s} n={len(pend3)} lanes={ {k[:6]: len(v) for k, v in lanes.items()} } min_gap_in_lane={min(gaps) if gaps else None}")
    ok = wait_until(lambda: len(msgs(cid, 3, "SENT")) >= len(pend3) - 1, timeout=int(max(len(v) for v in lanes.values()) * 125 + 300), interval=8)
    sent3 = msgs(cid, 3, "SENT")
    ts = sorted(dt(m["sent_at"]) for m in sent3)
    R.check("E2E-J1-103", "After resume every step-3 email goes out exactly once, spread over time (not a burst)",
            ok and len({m["email"] for m in sent3}) == len(sent3) >= len(pend3) - 1 and (ts[-1] - ts[0]).total_seconds() >= 119,
            f"sent={len(sent3)}/{len(pend3)} span={(ts[-1]-ts[0]).total_seconds() if ts else None}s")


GDPR_TABLES_SQL = ("SELECT table_name FROM information_schema.columns WHERE table_schema=DATABASE() AND column_name='prospect_id'")


def phase_gdpr(R, C):
    """J1-104..J1-107: erase the won contact; nothing linked may remain."""
    ws = C["w"]
    pid = C["won_pid"]
    e = [x for x in C["emails"]][ROLE["replier"]].lower()
    mids = [r[0] for r in rows(f"SELECT message_id FROM email_messages WHERE prospect_id={q(pid)}")]
    before = {}
    for (t,) in rows(GDPR_TABLES_SQL):
        n = int(scalar(f"SELECT COUNT(*) FROM `{t}` WHERE prospect_id={q(pid)}") or 0)
        if n:
            before[t] = n
    s, b = req("DELETE", f"/prospects/{pid}", None, ws.token)
    after = {}
    for t in before:
        n = int(scalar(f"SELECT COUNT(*) FROM `{t}` WHERE prospect_id={q(pid)}") or 0)
        if n:
            after[t] = n
    ev = scalar(f"SELECT COUNT(*) FROM email_events WHERE message_id IN ({','.join(q(m) for m in mids)})") if mids else "0"
    pii = scalar(f"SELECT COUNT(*) FROM email_messages WHERE LOWER(from_email)={q(e)} OR LOWER(to_email)={q(e)}")
    conv = scalar(f"SELECT COUNT(*) FROM conversations WHERE tenant_id={q(ws.tenant_id)} AND prospect_id={q(pid)}")
    R.check("E2E-J1-104", "GDPR erase (DELETE /prospects/{id}) removes the contact and every row keyed by prospect_id",
            s == 200 and not after and scalar(f"SELECT COUNT(*) FROM prospects WHERE prospect_id={q(pid)}") == "0",
            f"{s} {b} tables that had rows={before}; still present={after}")
    R.check("E2E-J1-105", "Erased contact: no email events, no messages/conversations with their address remain",
            ev == "0" and pii == "0" and conv == "0", f"events={ev} messages_with_address={pii} conversations={conv}")
    opp = scalar(f"SELECT COUNT(*) FROM opportunities WHERE opportunity_id={q(C['opp_id'])}")
    lead = scalar(f"SELECT COUNT(*) FROM leads WHERE lead_id={q(C['lead_id'])}")
    R.check("E2E-J1-106", "Linked lead and deal follow the erasure (gone, or no longer reference the contact)",
            lead == "0" and (opp == "0" or scalar(f"SELECT prospect_id FROM opportunities WHERE opportunity_id={q(C['opp_id'])}") in ("", "NULL")),
            f"lead rows={lead} opportunity rows={opp}")
    audit = int(scalar(f"SELECT COUNT(*) FROM audit_logs WHERE tenant_id={q(ws.tenant_id)} AND entity_id={q(pid)}") or 0)
    R.check("E2E-J1-107", "An audit row records the erasure", audit > 0,
            f"audit_logs rows for the contact id = {audit} (the erasure leaves no audit trail)")
    em = [x.lower() for x in C["emails"]]
    still = [em[ROLE[k]] for k in ("unsub_link", "unsub_oneclick", "bounce")
             if scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(ws.tenant_id)} AND LOWER(email)={q(em[ROLE[k]])}") == "1"]
    R.check("E2E-J1-108", "Suppression list survives the erasure of another contact (unsubscribed x2 and bounced addresses still blocked)", len(still) == 3, f"{still}")




# ───────────────────────── context persistence (resume a phase during development / re-runs) ─────────────────────────

import os as _os
STATE_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".state")


def save_ctx(C, name="j1"):
    _os.makedirs(STATE_DIR, exist_ok=True)
    out = {k: v for k, v in C.items() if not k.startswith("_") and k not in ("w", "captured1")}
    out["w_dict"] = {k: v for k, v in vars(C["w"]).items()}
    out["users_clean"] = {k: {kk: vv for kk, vv in u.items() if kk != "token"} for k, u in C["users"].items()}
    with open(_os.path.join(STATE_DIR, name + ".json"), "w") as f:
        json.dump(out, f, indent=1, default=str)


def load_ctx(name="j1"):
    """Rebuild the context: new fake IMAP (same mailboxes, ports updated on the inboxes), fresh logins."""
    from .helpers import Workspace
    with open(_os.path.join(STATE_DIR, name + ".json")) as f:
        C = json.load(f)
    ws = Workspace.__new__(Workspace)
    ws.__dict__.update(C.pop("w_dict"))
    ws.token = user_token(ws.email, ws.password)
    C["w"] = ws
    users = C.pop("users_clean")
    C["users"] = {}
    for k, u in users.items():
        C["users"][k] = {**u, "token": ws.token if k == "owner" else user_token(u["email"], ws.password)}
    imap = FakeImap(SCRATCH)
    C["_imap"] = imap
    for ib, addr in ((C["inbox1"], C["addr1"]), (C["inbox2"], C["addr2"])):
        imap.add_account(addr, "Pw-e2e-1")
        s, b = req("PUT", f"/inboxes/{ib}", {"imap_host": HOST_FROM_CONTAINERS, "imap_port": imap.port,
                                             "imap_username": addr, "imap_password": "Pw-e2e-1"}, ws.token)
        assert s == 200, (s, b)
    C["captured1"] = {c.to: c for c in captured_for(emails=C["emails"], domain=C["dom"])}
    return C
