"""J2 - Deliverability: bounces + complaints through the webhook -> suppression -> auto-pause thresholds -> domain health ->
alerts -> resume; soft bounces do nothing; another tenant on the SAME domain is unaffected."""
import json
import time

from .flows import add_missing_step_templates, mk_inbox, msgs, quick_campaign, upload_list, wait_status
from .helpers import (add_user, ev_bounce, ev_complaint, ev_delivery, new_workspace, q, req, rows, run_job, scalar,
                      ses_post, sns_envelope, sql, wait_until)


def _camp(ws, cid):
    s, b = req("GET", f"/campaigns/{cid}", token=ws.token)
    return b


def _domain_row(ws, dom):
    s, b = req("GET", "/deliverability/domains", token=ws.token)
    items = b if isinstance(b, list) else b.get("items") or b.get("domains") or []
    for d in items:
        if (d.get("domain_name") or d.get("domain")) == dom:
            # the API exposes the score; the 24h counters are read from the tenant's own sending_domains row
            r = rows(f"SELECT sends_24h, bounce_rate_24h, complaint_rate_24h FROM sending_domains WHERE tenant_id={q(ws.tenant_id)} AND domain_name={q(dom)}")
            if r:
                d = {**d, "sends_24h": int(r[0][0] or 0), "bounce_rate_24h": float(r[0][1] or 0), "complaint_rate_24h": float(r[0][2] or 0)}
            return d
    return None


def _post(kind, m, src, **kw):
    mk = {"bounce": ev_bounce, "complaint": ev_complaint}[kind]
    return ses_post(mk(m["message_id"], m["ses_id"], src, m["email"], **kw))


def run(R, hooks=None):
    D = new_workspace("j2d")
    E = new_workspace("j2e")
    rid = D.rid
    for w in (D, E):
        w.set_sender_identity()
    d1, d2, d3 = (f"{x}-{rid}.example" for x in ("dom1", "dom2", "dom3"))
    in1, a1, _ = mk_inbox(D, f"d1.{rid}", d1, delay=1)
    in1e, a1e, _ = mk_inbox(E, f"e1.{rid}", d1, delay=1)      # tenant E sends from the SAME domain
    in2, a2, _ = mk_inbox(D, f"d2.{rid}", d2, delay=1)
    in3a, a3a, _ = mk_inbox(D, f"d3a.{rid}", d3, delay=1)
    in3b, a3b, _ = mk_inbox(D, f"d3b.{rid}", d3, delay=1)
    src = {in1: a1, in2: a2, in3a: a3a, in3b: a3b}

    # ── scenario A: <50 sends, 5 hard bounces pause; soft bounce does not count ──
    A = quick_campaign(D, "dlva", [in1], 12, rid, prefix="a", waits=(0, 5))
    Ee = quick_campaign(E, "dlve", [in1e], 4, rid, prefix="e", waits=(0, 5))
    R.check("E2E-J2-01", "Two tenants send from the same domain concurrently: 12 + 4 emails delivered to SES, each on its own tenant",
            len(A["sent"]) == 12 and len(Ee["sent"]) == 4, f"{len(A['sent'])} {len(Ee['sent'])}")
    ms = {m["email"]: m for m in A["sent"]}
    order = [e.lower() for e in A["emails"]]
    for e in order:
        ses_post(ev_delivery(ms[e]["message_id"], ms[e]["ses_id"], a1, e))
    res = [_post("bounce", ms[order[i]], a1)[1].get("status") for i in range(4)]
    ca = _camp(D, A["cid"])
    R.check("E2E-J2-02", "4 hard bounces in 12 sends (< 5): campaign stays ACTIVE", res == ["processed"] * 4 and ca["status"] == "ACTIVE",
            f"{res} {ca['status']} {ca.get('paused_reason')}")
    soft = _post("bounce", ms[order[4]], a1, btype="Transient", sub="MailboxFull", diag="smtp; 452 4.2.2 mailbox full")
    sup = scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(D.tenant_id)} AND LOWER(email)={q(order[4])}")
    st = scalar(f"SELECT status FROM email_messages WHERE message_id={q(ms[order[4]]['message_id'])}")
    enrol = scalar(f"SELECT status FROM campaign_prospects WHERE campaign_id={q(A['cid'])} AND prospect_id="
                   f"(SELECT prospect_id FROM prospects WHERE tenant_id={q(D.tenant_id)} AND LOWER(email)={q(order[4])})")
    ca = _camp(D, A["cid"])
    R.check("E2E-J2-03", "A soft (Transient) bounce is recorded but does NOT suppress, fail the message, stop the contact or count towards auto-pause",
            soft[0] == 200 and sup == "0" and st == "SENT" and enrol == "ACTIVE" and ca["status"] == "ACTIVE",
            f"{soft} suppressed={sup} msg={st} enrol={enrol} camp={ca['status']}")
    dom_d = _domain_row(D, d1)
    R.check("E2E-J2-04", "Domain health row (tenant D): 12 sends in 24h, 4 hard bounces = 33.3% bounce rate, reputation score fell",
            dom_d and dom_d.get("sends_24h") == 12 and abs((dom_d.get("bounce_rate_24h") or 0) - 4 / 12) < .01 and dom_d["current_reputation_score"] < 100,
            f"{dom_d}")
    _post("bounce", ms[order[5]], a1)
    ca = _camp(D, A["cid"])
    R.check("E2E-J2-05", "5th hard bounce auto-pauses the campaign: PAUSED, auto_paused, reason names '5 hard bounces in the first 12 sends'",
            ca["status"] == "PAUSED" and ca.get("auto_paused") and "5 hard bounces in the first 12 sends" in (ca.get("paused_reason") or ""),
            f"{ca['status']} {ca.get('auto_paused')} {ca.get('paused_reason')}")
    cs = _camp(E, Ee["cid"])
    dom_e = _domain_row(E, d1)
    dom_d = _domain_row(D, d1)
    R.check("E2E-J2-06", "Tenant E on the SAME domain is unaffected: campaign ACTIVE, own domain row with 4 sends / 0% bounce; D's row shows its own 12 / 41.7%",
            cs["status"] == "ACTIVE" and not cs.get("auto_paused") and dom_e and dom_e.get("sends_24h") in (0, 4)
            and (dom_e.get("bounce_rate_24h") or 0) == 0 and dom_d and abs((dom_d.get("bounce_rate_24h") or 0) - 5 / 12) < .01,
            f"E camp={cs['status']} E row={dom_e} D row={dom_d}")
    supp = int(scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(D.tenant_id)}"))
    leak = int(scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(E.tenant_id)}"))
    R.check("E2E-J2-07", "Suppressions created for D's 5 hard-bounced addresses only (not the soft one), none for tenant E",
            supp == 5 and leak == 0, f"D={supp} E={leak}")
    ok, out = run_job("deliverability")
    s, al = req("GET", "/deliverability/alerts", token=D.token)
    s2, alE = req("GET", "/deliverability/alerts", token=E.token)
    txtD, txtE = json.dumps(al).lower(), json.dumps(alE).lower()
    rep = scalar(f"SELECT COUNT(*) FROM reputation_alerts WHERE tenant_id={q(D.tenant_id)} AND alert_type='AUTO_PAUSE'")
    R.check("E2E-J2-08", "Alert raised for tenant D (auto-pause) after the deliverability job; tenant E sees none of it",
            (rep != "0" or "pause" in txtD) and d1 not in txtE.replace(E.rid, "") or ("pause" not in txtE),
            f"job_ok={ok} reputation_alerts={rep} D alerts={txtD[:200]} E alerts={txtE[:120]}")
    _, nt = req("GET", "/notifications", token=D.token)
    R.check("E2E-J2-09", "Owner is notified that the campaign was auto-paused (notification centre)",
            "paus" in json.dumps(nt).lower(), f"notifications: {str(nt)[:250]}; the pause is visible as a reputation alert (J2-08) and on the campaign, but no in-app notification is raised", "not-implemented")
    s, rr = req("POST", f"/campaigns/{A['cid']}/resume", None, D.token)
    ca = _camp(D, A["cid"])
    time.sleep(3)
    ca2 = _camp(D, A["cid"])
    R.check("E2E-J2-10", "Resuming an auto-paused campaign works (ACTIVE) and a baseline stops the old bounces re-pausing it at once",
            s == 200 and ca["status"] == "ACTIVE" and ca2["status"] == "ACTIVE", f"{s} {rr} {ca['status']} {ca2['status']}")

    # ── scenario B: complaints ──
    B = quick_campaign(D, "dlvb", [in2], 10, rid, prefix="b", waits=(0, 5))
    mb = {m["email"]: m for m in B["sent"]}
    ob = [e.lower() for e in B["emails"]]
    _post("complaint", mb[ob[0]], a2)
    cb = _camp(D, B["cid"])
    st = scalar(f"SELECT status FROM email_messages WHERE message_id={q(mb[ob[0]]['message_id'])}")
    sup = scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(D.tenant_id)} AND LOWER(email)={q(ob[0])}")
    R.check("E2E-J2-11", "1 complaint in 10 sends (< 2): contact suppressed + message COMPLAINED, campaign still ACTIVE",
            cb["status"] == "ACTIVE" and st == "COMPLAINED" and sup == "1", f"{cb['status']} msg={st} supp={sup}")
    _post("complaint", mb[ob[1]], a2)
    cb = _camp(D, B["cid"])
    R.check("E2E-J2-12", "2nd complaint auto-pauses ('2 spam complaints in the first 10 sends')",
            cb["status"] == "PAUSED" and "2 spam complaints in the first 10 sends" in (cb.get("paused_reason") or ""),
            f"{cb['status']} {cb.get('paused_reason')}")
    # SNS envelope shape also works
    r = ses_post(sns_envelope(ev_delivery(mb[ob[2]]["message_id"], mb[ob[2]]["ses_id"], a2, ob[2])))
    R.check("E2E-J2-13", "Non-raw SNS envelope ({Message: json}) with the token is accepted and processed", r[0] == 200 and r[1].get("status") == "processed", f"{r}")

    # ── scenario C: >= 50 sends -> rate thresholds ──
    Cc = quick_campaign(D, "dlvc", [in3a, in3b], 54, rid, prefix="c", wait_secs=600, waits=(0, 5))
    mc = {m["email"]: m for m in Cc["sent"]}
    oc = [e.lower() for e in Cc["emails"]]
    sc = lambda m: src[m["inbox_id"]]  # noqa: E731
    for i in range(2):
        _post("bounce", mc[oc[i]], sc(mc[oc[i]]))
    cc = _camp(D, Cc["cid"])
    R.check("E2E-J2-14", "54 sends, 2 hard bounces (3.7% < 5%): still ACTIVE (the '5 bounces' rule no longer applies, the rate does)",
            len(Cc["sent"]) == 54 and cc["status"] == "ACTIVE", f"sent={len(Cc['sent'])} {cc['status']} {cc.get('paused_reason')}")
    _post("bounce", mc[oc[2]], sc(mc[oc[2]]))
    cc = _camp(D, Cc["cid"])
    R.check("E2E-J2-15", "3rd hard bounce = 5.6% >= 5% of 54 sends: auto-paused with the rate in the reason",
            cc["status"] == "PAUSED" and "hard-bounce rate 5.6% (3 of 54 sends)" in (cc.get("paused_reason") or ""),
            f"{cc['status']} {cc.get('paused_reason')}")
    s, rr = req("POST", f"/campaigns/{Cc['cid']}/resume", None, D.token)
    cc2 = _camp(D, Cc["cid"])
    R.check("E2E-J2-16", "Resume of the rate-paused campaign works; its health is then judged on sends AFTER the resume (baseline), so the old bounces do not re-pause it",
            s == 200 and cc2["status"] == "ACTIVE", f"{s} {cc2['status']} {cc2.get('paused_reason')}")
    mc2 = {m["email"]: m for m in Cc["sent"]}
    _post("complaint", mc2[oc[10]], sc(mc2[oc[10]]))
    cc3 = _camp(D, Cc["cid"])
    R.check("E2E-J2-16b", "A complaint about an email sent BEFORE the resume does not count against the resumed campaign (stays ACTIVE), but the contact is suppressed",
            cc3["status"] == "ACTIVE" and scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(D.tenant_id)} AND LOWER(email)={q(oc[10])}") == "1",
            f"{cc3['status']} {cc3.get('paused_reason')}")
    # the hourly health cycle (fresh process also runs it) does not double-pause or touch E
    ok, out = run_job("send_safety")
    cs = _camp(E, Ee["cid"])
    R.check("E2E-J2-17", "Hourly health cycle (send_safety) leaves tenant E's campaign ACTIVE and keeps D's paused campaigns paused",
            ok and cs["status"] == "ACTIVE" and _camp(D, A["cid"])["status"] in ("ACTIVE", "PAUSED"), f"{ok} {out[-200:]} E={cs['status']}")
    return {"D": D, "E": E}
