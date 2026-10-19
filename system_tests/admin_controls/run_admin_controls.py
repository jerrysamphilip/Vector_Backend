"""Admin UI controls round against the live stack (API + MySQL):

  list rename / description edit (PATCH /lists/{id}), invitation resend
  (POST /api/users/{id}/resend-invite: new credentials, old revoked, once a minute, guards),
  sales hierarchy guards used by the users screen (PUT /team/hierarchy/{id}), and adding a
  sending domain with DNS guidance (POST /deliverability/domains, GET .../dns-records).

SES may or may not be reachable from the stack: DKIM tokens are optional in the checks.
Two workspaces are registered (limit 10 per hour per IP).

    python3 system_tests/admin_controls/run_admin_controls.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import Results, Workspace, req, sql  # noqa: E402

R = Results("admin_controls", os.path.dirname(os.path.abspath(__file__)))
W = Workspace("adm")
W2 = Workspace("adm-b")
OWNER = W.token
AGENT = W.add_user("AGENT", "Ctlagent")
ADMIN = W.add_user("ADMIN", "Ctladmin")
rid = W.rid


def safe(fn, case_id, title):
    try:
        fn()
    except Exception as exc:  # a crash in one case must not hide the others
        R.record(case_id, title, False, f"error: {type(exc).__name__}: {exc}")


# ── AC-001..004 list rename ──────────────────────────────────
def lists():
    s, lst = req("POST", "/lists", {"list_name": f"Webinar {rid}", "list_type": "STATIC"}, OWNER)
    lid = (lst or {}).get("list_id")
    s2, upd = req("PATCH", f"/lists/{lid}", {"list_name": f"  Webinar Q4 {rid} ", "description": "Invitees"}, OWNER)
    s3, got = req("GET", f"/lists/{lid}", None, OWNER)
    R.check("AC-001", "A list can be renamed and given a description (trimmed, stored)",
            s == 201 and s2 == 200 and got.get("list_name") == f"Webinar Q4 {rid}" and got.get("description") == "Invitees",
            (s, s2, got))
    uid = W.user_id
    up_id = sql("SELECT UUID()")
    sql(f"INSERT INTO prospect_lists (list_id, tenant_id, list_name, source_type, list_type, uploaded_by, uploaded_at) "
        f"VALUES ('{up_id}', '{W.tenant_id}', 'upload-{rid}.csv', 'CSV', 'STATIC', '{uid}', NOW())")
    s4, _ = req("PATCH", f"/lists/{up_id}", {"list_name": f"Trade show {rid}"}, OWNER)
    R.check("AC-002", "An uploaded prospect list can be renamed too",
            s4 == 200 and sql(f"SELECT list_name FROM prospect_lists WHERE list_id='{up_id}'") == f"Trade show {rid}", s4)
    codes = {"other_tenant": req("PATCH", f"/lists/{lid}", {"list_name": "x"}, W2.token)[0],
             "blank": req("PATCH", f"/lists/{lid}", {"list_name": "  "}, OWNER)[0],
             "long_desc": req("PATCH", f"/lists/{lid}", {"description": "x" * 2001}, OWNER)[0],
             "long_name": req("PATCH", f"/lists/{lid}", {"list_name": "x" * 256}, OWNER)[0]}
    R.check("AC-003", "Rename is tenant-isolated (404) and bounded (blank 400, too long 422)",
            codes == {"other_tenant": 404, "blank": 400, "long_desc": 422, "long_name": 422}, codes)
    s5, _ = req("PATCH", f"/lists/{lid}", {"list_name": "Agent rename"}, AGENT["token"])
    R.check("AC-004", "Without manage_prospects a list cannot be renamed (403)", s5 == 403, s5)


safe(lists, "AC-001", "List rename")


# ── AC-010..013 invitation resend ────────────────────────────
def resend():
    email = f"resend-{rid}@systest.example"
    s, inv = req("POST", "/api/users/invite", {"email": email, "role": "AGENT"}, OWNER)
    uid = (inv or {}).get("user_id")
    s2, b2 = req("POST", f"/api/users/{uid}/resend-invite", None, OWNER)
    R.check("AC-010", "Resending straight away is refused (429, once a minute per user)",
            s == 201 and s2 == 429 and "seconds" in str((b2 or {}).get("detail", "")), (s, s2, b2))
    old_link = sql(f"SELECT token_hash FROM magic_login_tokens WHERE user_id='{uid}' AND used_at IS NULL")
    sql(f"UPDATE invitations SET created_at = NOW() - INTERVAL 2 MINUTE WHERE email='{email}'")
    s3, b3 = req("POST", f"/api/users/{uid}/resend-invite", None, OWNER)
    pending = sql(f"SELECT COUNT(*) FROM invitations WHERE email='{email}' AND status='PENDING'")
    revoked = sql(f"SELECT COUNT(*) FROM invitations WHERE email='{email}' AND status='REVOKED'")
    open_links = sql(f"SELECT COUNT(*) FROM magic_login_tokens WHERE user_id='{uid}' AND used_at IS NULL")
    new_link = sql(f"SELECT token_hash FROM magic_login_tokens WHERE user_id='{uid}' AND used_at IS NULL")
    no_password = sql(f"SELECT password_hash IS NULL FROM users WHERE user_id='{uid}'")
    R.check("AC-011", "After a minute: a new set-password link (no password, I-13); the old invitation and links are revoked",
            s3 == 200 and (b3 or {}).get("resent") is True and pending == "1" and int(revoked or 0) >= 1
            and open_links == "1" and new_link != old_link and no_password == "1"
            and "password" not in str(b3).lower(), (s3, pending, revoked, open_links, no_password))
    s4, _ = req("POST", f"/api/users/{uid}/resend-invite", None, OWNER)
    R.check("AC-012", "A second resend within the minute is refused again (429)", s4 == 429, s4)
    sa_email = f"resend-sa-{rid}@systest.example"
    sa = (req("POST", "/api/users/invite", {"email": sa_email, "role": "SUPER_ADMIN"}, OWNER)[1] or {}).get("user_id")
    sql(f"UPDATE invitations SET created_at = NOW() - INTERVAL 2 MINUTE WHERE email='{sa_email}'")
    codes = {"active_user": req("POST", f"/api/users/{AGENT['user_id']}/resend-invite", None, OWNER)[0],
             "other_tenant": req("POST", f"/api/users/{uid}/resend-invite", None, W2.token)[0],
             "agent": req("POST", f"/api/users/{uid}/resend-invite", None, AGENT["token"])[0],
             "admin_on_super_admin": req("POST", f"/api/users/{sa}/resend-invite", None, ADMIN["token"])[0]}
    R.check("AC-013", "Resend guards: joined user 409, other workspace 404, agent 403, admin on a Super Admin invite 403",
            codes == {"active_user": 409, "other_tenant": 404, "agent": 403, "admin_on_super_admin": 403}, codes)


safe(resend, "AC-010", "Invitation resend")


# ── AC-020..021 hierarchy guards ─────────────────────────────
def hierarchy():
    head = W.add_user("MANAGER", "Ctlhead")
    mgr = W.add_user("MANAGER", "Ctlmgr")
    rep = W.add_user("AGENT", "Ctlrep")
    ok = [req("PUT", f"/team/hierarchy/{head['user_id']}", {"sales_level": 1}, OWNER)[0],
          req("PUT", f"/team/hierarchy/{mgr['user_id']}", {"sales_level": 2, "manager_id": head["user_id"]}, OWNER)[0],
          req("PUT", f"/team/hierarchy/{rep['user_id']}", {"sales_level": 4, "manager_id": mgr["user_id"]}, OWNER)[0]]
    sql(f"UPDATE users SET sales_level=1 WHERE user_id='{rep['user_id']}'")
    s, b = req("PUT", f"/team/hierarchy/{head['user_id']}", {"sales_level": 2, "manager_id": rep["user_id"]}, OWNER)
    sql(f"UPDATE users SET sales_level=4 WHERE user_id='{rep['user_id']}'")
    s_self, _ = req("PUT", f"/team/hierarchy/{mgr['user_id']}", {"sales_level": 2, "manager_id": mgr["user_id"]}, OWNER)
    R.check("AC-020", "Reports-to cannot be the user or someone in their own team (400)",
            ok == [200, 200, 200] and s == 400 and s_self == 400, (ok, s, b, s_self))
    s2, _ = req("PUT", f"/team/hierarchy/{rep['user_id']}", {"sales_level": 4, "manager_id": "x" * 37}, OWNER)
    R.check("AC-021", "manager_id is bounded (422)", s2 == 422, s2)


safe(hierarchy, "AC-020", "Hierarchy guards")


# ── AC-030..035 sending domains ──────────────────────────────
def domains():
    name = f"send-{rid}.example.com"
    s, b = req("POST", "/deliverability/domains", {"domain_name": f" {name.upper()}. "}, OWNER)
    kinds = {r.get("kind") for r in (b or {}).get("dns_records", [])}
    R.check("AC-030", "Add domain: normalised, stored for this workspace, DNS guidance for SPF / DKIM / DMARC",
            s == 201 and (b.get("domain") or {}).get("domain_name") == name and {"SPF", "DKIM", "DMARC"} <= kinds
            and sql(f"SELECT COUNT(*) FROM sending_domains WHERE tenant_id='{W.tenant_id}' AND domain_name='{name}'") == "1",
            (s, kinds))
    s_dup, _ = req("POST", "/deliverability/domains", {"domain_name": name}, OWNER)
    s_other, _ = req("POST", "/deliverability/domains", {"domain_name": name}, W2.token)
    R.check("AC-031", "Unique per workspace (409); another workspace may add the same name (201)",
            s_dup == 409 and s_other == 201, (s_dup, s_other))
    bad = {v: req("POST", "/deliverability/domains", {"domain_name": v}, OWNER)[0]
           for v in ["localhost", "http://example.com", "a@example.com", "10.0.0.1", "*.example.com", "exa mple.com", ""]}
    R.check("AC-032", "Hostname validation rejects schemes, addresses, IPs, wildcards, spaces and blanks",
            all(c in (400, 422) for c in bad.values()), bad)
    s_agent, _ = req("POST", "/deliverability/domains", {"domain_name": f"agent-{rid}.example.com"}, AGENT["token"])
    s_view, view = req("GET", f"/deliverability/domains/{name}/dns-records", None, AGENT["token"])
    R.check("AC-033", "Only admins add domains (agent 403); any role reads the DNS records",
            s_agent == 403 and s_view == 200 and len((view or {}).get("dns_records", [])) >= 4, (s_agent, s_view))
    only = f"only-{rid}.example.org"
    req("POST", "/deliverability/domains", {"domain_name": only}, OWNER)
    s_iso, _ = req("GET", f"/deliverability/domains/{only}/dns-records", None, W2.token)
    R.check("AC-034", "DNS records of another workspace's domain are 404", s_iso == 404, s_iso)
    s_scan, scan = req("POST", f"/deliverability/domains/{name}/scans", None, OWNER)
    s_after, after = req("GET", f"/deliverability/domains/{name}/dns-records", None, OWNER)
    R.check("AC-035", "Check status (diagnostics scan) runs on an added domain and the statuses are refreshed",
            s_scan == 202 and s_after == 200 and after.get("spf_status") in ("PASS", "WARNING", "FAIL")
            and after.get("dmarc_status") in ("PASS", "WARNING", "FAIL"), (s_scan, after))
    for n, tok in ((name, OWNER), (only, OWNER), (name, W2.token)):
        req("DELETE", f"/deliverability/domains/{n}", None, tok)


safe(domains, "AC-030", "Sending domains")


# ── AC-040 OpenAPI ───────────────────────────────────────────
s, spec = req("GET", "/openapi.json")
paths = (spec or {}).get("paths", {}) if isinstance(spec, dict) else {}
R.check("AC-040", "New endpoints are documented (OpenAPI)",
        "/api/users/{user_id}/resend-invite" in paths and "post" in paths.get("/deliverability/domains", {})
        and "/deliverability/domains/{domain_name}/dns-records" in paths, s)

R.write()
c = {k: sum(1 for r in R.rows if r["status"] == k) for k in ("PASS", "FAIL", "BLOCKED")}
print("COUNTS", c)
