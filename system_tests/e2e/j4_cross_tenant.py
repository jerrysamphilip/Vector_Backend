"""J4 - Cross-tenant: two workspaces with the SAME recipient addresses and the SAME sending domain run the same journey
at the same time. Zero leakage of replies, suppression, events, domain health, reports, search, exports; independent daily limits."""
import json
import time

from .flows import (CSV_HEAD, add_missing_step_templates, captured_for, contact_by_email, create_campaign, detach_imap,
                    enroll, generate_content, launch, mk_inbox, msgs, travel_due, upload_list, wait_status)
from .helpers import (FakeImap, SCRATCH, ev_bounce, ev_delivery, ev_open, follow, new_workspace, q, reply_mime, req, rows,
                      scalar, ses_post, sql, wait_until)


def _prep(ws, tag, rid, emails, inbox, imap_addr):
    lines = [f"Rec{i},Same{i},Shared Co {i},{e},Director" for i, e in enumerate(emails)]
    v, c = upload_list(ws, CSV_HEAD + "\n" + "\n".join(lines) + "\n", f"{tag} list")
    cid, _ = create_campaign(ws, f"{tag} campaign {rid}", [inbox], waits=(0, 1, 1), sender_name=f"{tag} Sender")
    generate_content(ws, cid, c["list_id"], (0, 1, 1))
    add_missing_step_templates(ws, cid)
    s, e = enroll(ws, cid, c["list_id"])
    assert s == 200 and e["enrolled_count"] == len(emails), (s, e)
    return cid, c["list_id"]


def run(R):
    X, Y = new_workspace("j4x"), new_workspace("j4y")
    rid = X.rid
    X.set_sender_identity()
    s, _ = req("PUT", "/api/company-profiles/sender-identity", {"company_name": f"Other Co {rid}", "postal_address": "99 Other Road, Denver, CO 80202, USA"}, Y.token)
    imap = FakeImap(SCRATCH)
    dom = f"shared-{rid}.example"
    ix, ax, _ = mk_inbox(X, f"x.{rid}", dom, imap=imap, delay=2)
    iy, ay, _ = mk_inbox(Y, f"y.{rid}", dom, imap=imap, delay=2)
    emails = [f"rec{i}@recipients-{rid}.example" for i in range(6)]
    cx, lx = _prep(X, "X", rid, emails, ix, ax)
    cy, ly = _prep(Y, "Y", rid, emails, iy, ay)
    try:
        sx, _l = launch(X, cx)
        sy, _l = launch(Y, cy)
        travel_due(cx, 1)
        travel_due(cy, 1)
        ok = wait_until(lambda: len(msgs(cx, 1, "SENT")) == 6 and len(msgs(cy, 1, "SENT")) == 6, timeout=300, interval=4)
        got = captured_for(emails=emails, domain=dom)
        per = {}
        for c in got:
            per.setdefault(c.to, []).append(c)
        mx, my = {m["email"]: m for m in msgs(cx, 1, "SENT")}, {m["email"]: m for m in msgs(cy, 1, "SENT")}
        R.check("E2E-J4-01", "Same 6 recipients, same domain, two tenants launched together: each recipient gets exactly 2 emails (one per tenant), each from its own mailbox",
                sx == 200 and sy == 200 and ok and all(len(v) == 2 for v in per.values()) and len(per) == 6
                and all({c.header("From").split("<")[-1].strip(">") for c in v} == {ax, ay} for v in per.values()),
                f"{ {k: len(v) for k, v in per.items()} }")
        unsubs = {c.unsubscribe_url for c in got}
        foot = [("Other Co" in c.text) for c in got if c.source.endswith(ay + ">")]
        R.check("E2E-J4-02", "Each tenant's emails carry its own identity: distinct unsubscribe URLs (12), tenant Y's footer address is Y's, X's is X's",
                len(unsubs) == 12 and all(foot) and all(("1 Test Street" in c.text) for c in got if c.source.endswith(ax + ">")),
                f"unsubs={len(unsubs)} yfoot={foot}")
        # events for X only
        for i in (0, 1, 3):
            m = mx[emails[i]]
            ses_post(ev_delivery(m["message_id"], m["ses_id"], ax, emails[i]))
            ses_post(ev_open(m["message_id"], m["ses_id"], ax, emails[i]))
        kx = req("GET", "/reports/dashboard-summary", token=X.token)[1]["kpis"]
        ky = req("GET", "/reports/dashboard-summary", token=Y.token)[1]["kpis"]
        R.check("E2E-J4-03", "SES events for X's messages move only X's numbers (X opened 3 / Y opened 0), Y's metrics untouched",
                kx["total_opened"] == 3 and ky["total_opened"] == 0 and ky["total_emails_sent"] == 6,
                f"X={kx['total_opened']} Y={ky['total_opened']}/{ky['total_emails_sent']}")
        # unsubscribe r0 in X via X's real link
        cap_x = {c.to: c for c in got if c.source.endswith(ax + ">")}
        s, page = follow("POST", cap_x[emails[0]].unsubscribe_url, form={"confirm": "1"})
        supp = lambda ws, e: scalar(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id={q(ws.tenant_id)} AND LOWER(email)={q(e)}")  # noqa: E731
        s_y_enroll = req("POST", f"/campaigns/{cy}/prospects", {"prospect_ids": [contact_by_email(Y.token, emails[0])["prospect_id"]]}, Y.token)
        R.check("E2E-J4-04", "X's recipient unsubscribes: suppressed in X only; Y still has them subscribed with step 2 pending",
                supp(X, emails[0]) == "1" and supp(Y, emails[0]) == "0"
                and scalar(f"SELECT consent_status FROM prospects WHERE prospect_id={q(contact_by_email(Y.token, emails[0])['prospect_id'])}") != "UNSUBSCRIBED"
                and len(msgs(cy, 2, ["SCHEDULED", "QUEUED"], email_addr=emails[0])) == 1
                and not msgs(cx, 2, ["SCHEDULED", "QUEUED"], email_addr=emails[0]),
                f"X={supp(X, emails[0])} Y={supp(Y, emails[0])}")
        # reply to X's mailbox
        imap.deliver(ax, reply_mime(emails[1], ax, "Re: hi", "Interested, call me.", mx[emails[1]]["pm_id"]))
        s, _ = req("POST", f"/inboxes/{ix}/sync?days=7", None, X.token)
        cxs = req("GET", "/conversations?page_size=50", token=X.token)[1].get("items", [])
        cys = req("GET", "/conversations?page_size=50", token=Y.token)[1].get("items", [])
        ccx = {c.get("prospect_id"): c for c in cxs}
        ccy = {c.get("prospect_id"): c for c in cys}
        px1, py1 = contact_by_email(X.token, emails[1])["prospect_id"], contact_by_email(Y.token, emails[1])["prospect_id"]
        R.check("E2E-J4-05", "Reply lands in X only: X's conversation with that address ends INBOUND (REPLIED), Y's own conversation with the same address stays OUTBOUND-only, enrolment ACTIVE, step 2 pending",
                ccx.get(px1, {}).get("last_message_direction") == "INBOUND" and ccy.get(py1, {}).get("last_message_direction") == "OUTBOUND"
                and ccx.get(px1, {}).get("id") != ccy.get(py1, {}).get("id")
                and scalar(f"SELECT status FROM campaign_prospects WHERE campaign_id={q(cy)} AND prospect_id={q(py1)}") == "ACTIVE"
                and scalar(f"SELECT status FROM campaign_prospects WHERE campaign_id={q(cx)} AND prospect_id={q(px1)}") == "REPLIED"
                and scalar(f"SELECT COUNT(*) FROM email_messages WHERE prospect_id={q(py1)} AND direction='INBOUND'") == "0",
                f"X={ccx.get(px1, {}).get('last_message_direction')} Y={ccy.get(py1, {}).get('last_message_direction')}")
        # hard bounce r2 in Y
        m = my[emails[2]]
        ses_post(ev_bounce(m["message_id"], m["ses_id"], ay, emails[2]))
        rx = scalar(f"SELECT bounce_rate_24h FROM sending_domains WHERE tenant_id={q(X.tenant_id)} AND domain_name={q(dom)}")
        ry = scalar(f"SELECT bounce_rate_24h FROM sending_domains WHERE tenant_id={q(Y.tenant_id)} AND domain_name={q(dom)}")
        R.check("E2E-J4-06", "Bounce on Y's message: Y suppresses r2 and Y's domain health shows it; X's suppression and domain row are clean",
                supp(Y, emails[2]) == "1" and supp(X, emails[2]) == "0" and float(ry or 0) > 0 and float(rx or 0) == 0,
                f"Y supp={supp(Y, emails[2])} X supp={supp(X, emails[2])} rates X={rx} Y={ry}")
        # step 2 for both
        travel_due(cx, 2)
        travel_due(cy, 2)
        want_x, want_y = {emails[i] for i in (2, 3, 4, 5)}, {emails[i] for i in (0, 1, 3, 4, 5)}
        ok = wait_until(lambda: {m["email"] for m in msgs(cx, 2, "SENT")} == want_x and {m["email"] for m in msgs(cy, 2, "SENT")} == want_y, timeout=300, interval=4)
        sx2, sy2 = {m["email"] for m in msgs(cx, 2, "SENT")}, {m["email"] for m in msgs(cy, 2, "SENT")}
        R.check("E2E-J4-07", "Step 2 goes to exactly each tenant's own live contacts: X skips its unsubscribed+replied (4 sent), Y skips its bounced (5 sent)",
                ok and sx2 == want_x and sy2 == want_y, f"X={sorted(e[:4] for e in sx2)} Y={sorted(e[:4] for e in sy2)}")
        # independent daily limits: X capped at what it already sent
        sent_today_x = int(scalar(f"SELECT emails_sent_today FROM sending_inboxes WHERE inbox_id={q(ix)}"))
        req("PUT", f"/inboxes/{ix}", {"daily_limit": sent_today_x, "max_emails_per_day": sent_today_x}, X.token)
        travel_due(cx, 3)
        travel_due(cy, 3)
        ok = wait_until(lambda: len(msgs(cy, 3, "SENT")) == 5, timeout=300, interval=4)
        time.sleep(35)
        x3 = msgs(cx, 3, "SENT")
        R.check("E2E-J4-08", f"Independent daily limits: X's mailbox is capped at {sent_today_x}/day so its step 3 is held, while Y (same domain) sends all 5",
                ok and len(x3) == 0 and len(msgs(cx, 3, ["SCHEDULED", "QUEUED"])) == 4,
                f"Y step3 sent={len(msgs(cy, 3, 'SENT'))} X step3 sent={len(x3)} X pending={len(msgs(cx, 3, ['SCHEDULED', 'QUEUED']))}")
        # leakage probes through the API
        px, py = contact_by_email(X.token, emails[3])["prospect_id"], contact_by_email(Y.token, emails[3])["prospect_id"]
        probes = {
            "Y reads X contact": req("GET", f"/contacts/{px}", token=Y.token)[0],
            "Y reads X campaign": req("GET", f"/campaigns/{cx}", token=Y.token)[0],
            "Y reads X analytics": req("GET", f"/campaigns/{cx}/analytics", token=Y.token)[0],
            "X pauses Y campaign": req("POST", f"/campaigns/{cy}/pause", None, X.token)[0],
            "X patches Y contact": req("PATCH", f"/contacts/{py}", {"designation": "x"}, X.token)[0],
            "X deletes Y contact": req("DELETE", f"/prospects/{py}", None, X.token)[0],
        }
        R.check("E2E-J4-09", "Cross-tenant object access by id is refused (404) for read, write, pause and erase",
                all(v == 404 for v in probes.values()), f"{probes}")
        sx_ = req("GET", f"/search?q=recipients-{rid}", token=X.token)[1]
        sy_ = req("GET", f"/search?q=recipients-{rid}", token=Y.token)[1]
        ids_x = {x for x in rows(f"SELECT prospect_id FROM prospects WHERE tenant_id={q(X.tenant_id)}") for x in x}
        ids_y = {x for x in rows(f"SELECT prospect_id FROM prospects WHERE tenant_id={q(Y.tenant_id)}") for x in x}
        txt_x, txt_y = json.dumps(sx_), json.dumps(sy_)
        R.check("E2E-J4-10", "Global search for the shared address domain returns each tenant only its own records (no foreign ids)",
                not any(i in txt_x for i in ids_y) and not any(i in txt_y for i in ids_x) and any(i in txt_x for i in ids_x),
                f"X sees foreign={[i for i in ids_y if i in txt_x][:2]} Y sees foreign={[i for i in ids_x if i in txt_y][:2]}")
        ex = req("GET", "/contacts/export?format=csv", token=X.token, raw=True)[1].decode(errors="ignore")
        ey = req("GET", "/contacts/export?format=csv", token=Y.token, raw=True)[1].decode(errors="ignore")
        R.check("E2E-J4-11", "Contact exports contain only the caller's tenant (6 data rows each, no cross ids, own sender-specific data)",
                ex.strip().count("\n") == 6 and ey.strip().count("\n") == 6 and not any(i in ex for i in ids_y) and not any(i in ey for i in ids_x),
                f"rows {ex.strip().count(chr(10))}/{ey.strip().count(chr(10))}")
        kx = req("GET", "/reports/dashboard-summary", token=X.token)[1]["kpis"]
        ky = req("GET", "/reports/dashboard-summary", token=Y.token)[1]["kpis"]
        out_x = int(scalar(f"SELECT COUNT(*) FROM email_messages m JOIN campaigns c ON c.campaign_id=m.campaign_id WHERE c.tenant_id={q(X.tenant_id)} AND m.direction='OUTBOUND' AND m.sent_at IS NOT NULL"))
        out_y = int(scalar(f"SELECT COUNT(*) FROM email_messages m JOIN campaigns c ON c.campaign_id=m.campaign_id WHERE c.tenant_id={q(Y.tenant_id)} AND m.direction='OUTBOUND' AND m.sent_at IS NOT NULL"))
        R.check("E2E-J4-12", "Reports: each tenant's total_emails_sent equals its own OUTBOUND sends (X 10, Y 16 incl. the bounced one); inbound replies/bounce notices are not 'sent'",
                kx["total_emails_sent"] == out_x == 10 and ky["total_emails_sent"] == out_y == 16,
                f"dashboard X={kx['total_emails_sent']} (outbound rows {out_x}; X also holds 1 INBOUND reply) Y={ky['total_emails_sent']} (outbound {out_y}; Y holds 1 INBOUND bounce notice)")
        from .helpers import run_job
        run_job("send_safety")
        dx = req("GET", "/deliverability/domains", token=X.token)[1]
        dy = req("GET", "/deliverability/domains", token=Y.token)[1]
        lx_ = [d for d in dx if d.get("domain_name") == dom]
        ly_ = [d for d in dy if d.get("domain_name") == dom]
        rx_ = rows(f"SELECT sends_24h, bounce_rate_24h FROM sending_domains WHERE tenant_id={q(X.tenant_id)} AND domain_name={q(dom)}")
        ry_ = rows(f"SELECT sends_24h, bounce_rate_24h FROM sending_domains WHERE tenant_id={q(Y.tenant_id)} AND domain_name={q(dom)}")
        R.check("E2E-J4-13", "Domain health is per tenant: after the health cycle each has one row for the shared domain with its own 24h counters (X 10 sends / 0% bounce; Y 16 sends / bounce > 0)",
                len(lx_) == 1 and len(ly_) == 1 and rx_ and ry_ and int(rx_[0][0]) == out_x and int(ry_[0][0]) == out_y
                and float(rx_[0][1]) == 0 and float(ry_[0][1]) > 0,
                f"X API={lx_[:1] and lx_[0]['current_reputation_score']} row={rx_} Y API={ly_[:1] and ly_[0]['current_reputation_score']} row={ry_}")
    finally:
        detach_imap(X)
        detach_imap(Y)
