"""J6 - Workflows & automation, driven with on-demand job runs: campaign automation rules (events -> actions through the worker),
sales workflow rules, round-robin lead assignment, stale-deal alerts, lead recycle, notification e-mail delivery."""
import json
import time
from datetime import date, datetime, timedelta

from .flows import (add_missing_step_templates, captured_for, detach_imap, mk_inbox, msgs, quick_campaign, travel_due,
                    wait_status, upload_list, CSV_HEAD)
from .helpers import (add_user, ev_click, ev_delivery, ev_open, new_workspace, q, req, rows, run_job, scalar, ses_captured,
                      ses_post, ses_verify, sql, wait_until)


def _notes(token):
    s, b = req("GET", "/notifications?page_size=100", token=token)
    return b.get("items", []) if isinstance(b, dict) else (b or [])


def run(R):
    ses_verify("platform@e2e-platform.example")
    ws = new_workspace("j6")
    rid = ws.rid
    ws.set_sender_identity()
    owner = ws.token
    mgr = add_user(ws, "MANAGER", f"Mia{rid}")
    ag = [add_user(ws, "AGENT", f"Ag{i}x{rid}") for i in range(3)]
    for key, lvl, boss, uid in [("o", 1, None, ws.user_id), ("m", 2, ws.user_id, mgr["user_id"])] + \
            [("a", 3, mgr["user_id"], a["user_id"]) for a in ag]:
        s, b = req("PUT", f"/team/hierarchy/{uid}", {"sales_level": lvl, "manager_id": boss}, owner)
        assert s == 200, (s, b)

    # ── campaign automation rules ──
    ib, addr, _ = mk_inbox(ws, f"auto.{rid}", f"auto-{rid}.example", delay=2)
    C = quick_campaign(ws, "auto", [ib], 4, rid, waits=(0, 2), prefix="p")
    cid = C["cid"]
    ms = {m["email"]: m for m in C["sent"]}
    em_ = [e.lower() for e in C["emails"]]
    t_extra = req("POST", "/templates", {"campaign_id": cid, "subject": "Special offer for {{first_name}}", "body": "<p>Hi {{first_name}}, here is the case study you clicked.</p>"}, owner)[1]
    r1 = req("POST", "/automation-rules/", {"name": "Pause on open", "trigger_type": "EMAIL_OPENED", "action_type": "PAUSE_PROSPECT", "campaign_id": cid}, owner)
    r2 = req("POST", "/automation-rules/", {"name": "Offer on click", "trigger_type": "EMAIL_CLICKED", "action_type": "SEND_EMAIL",
                                            "action_config": {"template_id": t_extra.get("template_id")}, "campaign_id": cid}, owner)
    R.check("E2E-J6-01", "Two campaign automation rules created (open -> pause prospect, click -> send template)", r1[0] in (200, 201) and r2[0] in (200, 201), f"{r1} {r2}")
    n_before = len(captured_for(emails=[em_[1]], domain=f"auto-{rid}.example"))
    ses_post(ev_open(ms[em_[0]]["message_id"], ms[em_[0]]["ses_id"], addr, em_[0]))
    ses_post(ev_click(ms[em_[1]]["message_id"], ms[em_[1]]["ses_id"], addr, em_[1]))
    p0 = scalar(f"SELECT status FROM campaign_prospects WHERE campaign_id={q(cid)} AND prospect_id=(SELECT prospect_id FROM prospects WHERE tenant_id={q(ws.tenant_id)} AND LOWER(email)={q(em_[0])})")
    R.check("E2E-J6-02", "SES Open event fires the 'pause prospect' rule: enrolment becomes PAUSED", p0 == "PAUSED", f"{p0}")
    got = wait_until(lambda: [c for c in captured_for(emails=[em_[1]], domain=f"auto-{rid}.example") if c.subject.startswith("Special offer")], timeout=300, interval=4)
    R.check("E2E-J6-03", "SES Click event fires the 'send email' rule: the worker sends the rule's template to that contact through SES (personalised subject)",
            bool(got) and "Special offer for Pat1" in got[0].subject, f"subjects mailed to the contact={[c.subject for c in captured_for(emails=[em_[1]])]}; rule message state="
            f"{rows(f'SELECT status, LEFT(failure_reason,90) FROM email_messages WHERE campaign_id={q(cid)} AND template_id={q(t_extra.get(chr(116)+chr(101)+chr(109)+chr(112)+chr(108)+chr(97)+chr(116)+chr(101)+chr(95)+chr(105)+chr(100)))}')}")
    travel_due(cid, 2)
    time.sleep(75)
    sent2 = {m["email"] for m in msgs(cid, 2, "SENT")}
    R.check("E2E-J6-04", "A PAUSED prospect receives no further sequence step (step 2 goes only to the other 3 contacts)",
            em_[0] not in sent2 and sent2 == {em_[1], em_[2], em_[3]}, f"step2 sent to {sorted(e[:3] for e in sent2)}; paused contact included={em_[0] in sent2}")

    # ── sales workflow rules ──
    wf1 = req("POST", "/workflow/rules", {"name": "SQL handover", "object_type": "LEAD", "field": "stage", "operator": "equals", "value": "SQL",
                                          "actions": [{"type": "create_task", "title": "Prepare handover for {name}", "due_in_days": 2, "assign_to": "owner"},
                                                      {"type": "notify", "to": ["manager"], "message": "{name} reached SQL"}]}, owner)
    wf2 = req("POST", "/workflow/rules", {"name": "Big deal", "object_type": "DEAL", "field": "amount", "operator": "greater_than", "value": "20000",
                                          "actions": [{"type": "update_field", "field": "forecast_category", "value": "COMMIT"},
                                                      {"type": "notify", "to": ["manager"], "message": "Big deal {name}"}]}, owner)
    R.check("E2E-J6-05", "Workflow rules created (lead->SQL: task + notify manager; deal amount > 20000: set COMMIT + notify)",
            wf1[0] == 201 and wf2[0] == 201, f"{wf1} {wf2}")
    cps = {}
    for i in range(3):
        cps[i] = req("POST", "/contacts", {"email": f"wf{i}@wf-{rid}.example", "first_name": f"Wf{i}", "last_name": "Lead", "company_name": f"Wf Co {i}"}, owner)[1]["prospect_id"]
    lead = req("POST", "/leads", {"prospect_id": cps[0], "owner_id": ag[0]["user_id"]}, owner)[1]
    req("PATCH", f"/leads/{lead['lead_id']}", {"qualification": {"budget": True, "authority": True, "need": True, "timeline": True}}, ag[0]["token"])
    s, up = req("PATCH", f"/leads/{lead['lead_id']}", {"stage": "SQL"}, ag[0]["token"])
    tasks = req("GET", "/tasks?page_size=100", token=ag[0]["token"])[1]
    titems = tasks.get("items", []) if isinstance(tasks, dict) else tasks
    t = [x for x in titems if "handover" in (x.get("title") or "")]
    due_ok = t and t[0].get("due_at") and abs((datetime.fromisoformat(t[0]["due_at"][:19]) - (datetime.utcnow() + timedelta(days=2))).total_seconds()) < 3600
    R.check("E2E-J6-06", "Lead -> SQL runs the rule: owner gets a task 'Prepare handover for Wf0 Lead' due in 2 days",
            s == 200 and t and t[0]["title"] == "Prepare handover for Wf0 Lead" and due_ok, f"{s} {t[:1]}")
    mn = [n for n in _notes(mgr["token"]) if "reached SQL" in (n.get("title") or "")]
    rules = req("GET", "/workflow/rules", token=owner)[1]
    ritems = rules if isinstance(rules, list) else rules.get("items", [])
    rc = {r["name"]: r.get("run_count") for r in ritems}
    R.check("E2E-J6-07", "Manager is notified ('Wf0 Lead reached SQL'); rule run_count = 1; a non-matching change (SQL -> same) does not re-run it",
            len(mn) == 1 and rc.get("SQL handover") == 1, f"notes={[n.get('title') for n in _notes(mgr['token'])]} counts={rc}")
    req("PATCH", f"/leads/{lead['lead_id']}", {"next_step": "call"}, ag[0]["token"])
    sc = req("POST", f"/leads/{lead['lead_id']}/convert", {"name": f"Wf deal {rid}", "amount": 15000, "close_date": str(date.today() + timedelta(days=30))}, ag[0]["token"])
    oid = sc[1]["opportunity_id"]
    o1 = req("PATCH", f"/opportunities/{oid}", {"amount": 25000}, ag[0]["token"])[1]
    mn2 = [n for n in _notes(mgr["token"]) if "Big deal" in (n.get("title") or "")]
    R.check("E2E-J6-08", "Deal amount raised 15000 -> 25000 crosses the threshold: forecast category set to COMMIT by the rule and manager notified once",
            o1.get("forecast_category") == "COMMIT" and len(mn2) == 1, f"{o1.get('forecast_category')} notes={len(mn2)}")
    o2 = req("PATCH", f"/opportunities/{oid}", {"amount": 30000}, ag[0]["token"])[1]
    mn3 = [n for n in _notes(mgr["token"]) if "Big deal" in (n.get("title") or "")]
    R.check("E2E-J6-09", "Raising it further (25000 -> 30000, already above) does not fire the rule again", len(mn3) == 1, f"{len(mn3)}")

    # ── round robin ──
    pool = [a["user_id"] for a in ag]
    s, st = req("PUT", "/sales/settings", {"lead_assignment": {"mode": "round_robin", "users": pool}}, owner)
    csv = CSV_HEAD + "\n" + "\n".join(f"Rr{i},Test,Rr Co {i},rr{i}@rr-{rid}.example,CEO" for i in range(6)) + "\n"
    v, c = upload_list(ws, csv, f"rr {rid}")
    ids = [req("GET", f"/contacts?q=rr{i}@rr-{rid}.example", token=owner)[1]["items"][0] for i in range(6)]
    req("POST", "/contacts/bulk", {"prospect_ids": [x["prospect_id"] for x in ids], "action": "assign_owner", "owner_id": None}, owner)   # unowned
    owners0 = [x.get("owner_id") for x in ids]
    got_owner = []
    for x in ids:
        s, l = req("POST", "/leads", {"prospect_id": x["prospect_id"]}, owner)
        got_owner.append(l.get("owner_id") if s == 201 else (s, l))
    R.check("E2E-J6-10", "Round-robin assignment: 6 unowned contacts -> leads cycle through the 3-agent pool in order (a1,a2,a3,a1,a2,a3) and the agents are notified",
            got_owner == pool * 2, f"contact owners before={set(owners0)} lead owners={[pool.index(o) if o in pool else o for o in got_owner]}")
    nt = _notes(ag[1]["token"])
    R.check("E2E-J6-11", "Assigned agent gets an 'ASSIGNED' notification per lead (agent 2 received 2)", len([n for n in nt if "New lead" in (n.get("title") or "")]) == 2, f"{[n.get('title') for n in nt]}")
    # ── stale deals ──
    d_old = req("POST", "/opportunities", {"name": f"Stale idle {rid}", "owner_id": ag[1]["user_id"], "amount": 1000, "close_date": str(date.today() + timedelta(days=60))}, owner)[1]
    d_late = req("POST", "/opportunities", {"name": f"Stale late {rid}", "owner_id": ag[2]["user_id"], "amount": 2000, "close_date": str(date.today() - timedelta(days=1))}, owner)[1]
    d_ok = req("POST", "/opportunities", {"name": f"Fresh {rid}", "owner_id": ag[2]["user_id"], "amount": 3000, "close_date": str(date.today() + timedelta(days=60))}, owner)[1]
    sql(f"UPDATE opportunities SET updated_at = UTC_TIMESTAMP() - INTERVAL 20 DAY, created_at = UTC_TIMESTAMP() - INTERVAL 20 DAY WHERE opportunity_id={q(d_old['opportunity_id'])}")
    req("PUT", "/notifications/preferences", {"email": False}, ag[2]["token"])      # agent 3 opts out of e-mail
    ok, out = run_job("sales_jobs")
    sa = [n for n in _notes(ag[1]["token"]) if "Stale deal" in (n.get("title") or "")]
    sb = [n for n in _notes(ag[2]["token"]) if "Stale deal" in (n.get("title") or "")]
    R.check("E2E-J6-12", "Stale-deal job: idle 20 days -> owner alerted ('no activity for 20 days'); past close date -> alerted; fresh deal -> nothing",
            ok and len(sa) == 1 and "no activity" in (sa[0].get("body") or "") and len(sb) == 1 and "Stale late" in sb[0]["title"] and not [n for n in sb if "Fresh" in n["title"]],
            f"job_ok={ok} {out[-120:]} idle={[n.get('title') for n in sa]} late={[n.get('title') for n in sb]}")
    run_job("sales_jobs")
    sa2 = [n for n in _notes(ag[1]["token"]) if "Stale deal" in (n.get("title") or "")]
    R.check("E2E-J6-13", "Running the job again does not duplicate the alert (one per deal per week)", len(sa2) == 1, f"{len(sa2)}")
    em = wait_until(lambda: [c for c in ses_captured() if c.to == ag[1]["email"].lower() and "Stale deal" in c.subject], timeout=60, interval=3)
    R.check("E2E-J6-14", "Notification e-mail delivery: the stale-deal alert is emailed to the owner through SES ('[Vector] Stale deal: ...' with a link)",
            bool(em) and em[0].subject.startswith("[Vector]") and "/app/deals/" in em[0].text, f"{[c.subject for c in ses_captured() if c.to == ag[1]['email'].lower()]}")
    em2 = [c for c in ses_captured() if c.to == ag[2]["email"].lower()]
    R.check("E2E-J6-15", "A user who switched e-mail notifications off still gets the in-app alert but no e-mail (agent 3: alert yes, mail none; agent 2 mailed)",
            len(sb) == 1 and not em2 and bool(em), f"in-app={len(sb)} emails to agent3={[c.subject for c in em2]}")
    # ── lead recycle ──
    rl = req("POST", "/leads", {"prospect_id": cps[1], "owner_id": ag[0]["user_id"]}, owner)[1]
    s, dq = req("PATCH", f"/leads/{rl['lead_id']}", {"stage": "DISQUALIFIED", "disqualified_reason": "Bad timing", "recycle_in_days": 30}, ag[0]["token"])
    sql(f"UPDATE leads SET recycle_at = UTC_TIMESTAMP() - INTERVAL 1 HOUR WHERE lead_id={q(rl['lead_id'])}")
    run_job("sales_jobs")
    after = req("GET", f"/leads/{rl['lead_id']}", token=ag[0]["token"])[1]
    nn = [n for n in _notes(ag[0]["token"]) if "back in play" in (n.get("title") or "")]
    R.check("E2E-J6-16", "Lead recycle: a disqualified lead whose recycle date has passed returns to NEW, owner notified ('Lead back in play'), recycle date cleared",
            s == 200 and after["stage"] == "NEW" and not after.get("recycle_at") and len(nn) == 1, f"{s} {after.get('stage')} {after.get('recycle_at')} notes={len(nn)}")
    n_un = len([n for n in _notes(ag[0]["token"]) if not n.get("read_at")])
    req("POST", "/notifications/read", {"all": True}, ag[0]["token"])
    s, after_read = req("GET", "/notifications?page_size=100", token=ag[0]["token"])
    R.check("E2E-J6-17", "Marking notifications read clears the unread counter", n_un > 0 and after_read.get("unread") == 0, f"before={n_un} after={after_read.get('unread')}")
    return ws
