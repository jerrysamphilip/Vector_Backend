"""Happy-path scenario: creates the prerequisite data for a tenant through the API (filling t.ids) and exercises every
endpoint with a VALID request (HAPPY + SCHEMA checks). create_phase runs for tenant A (recorded) and tenant B (not
recorded; B only needs resources so that tenant isolation can be probed). tail_phase runs the destructive/idempotency
steps on throw-away victims."""
import base64
import hashlib
import hmac
import json
import struct
import time
import uuid

from engine import UNSET, OK2
from fixtures import q
from sweeplib import http as _http, sql


def http(*a, **k):
    s, b, h, d = _http(*a, **k)
    return s, b


ENV_EXT = (400, 401, 402, 403, 404, 409, 422, 424, 429, 500, 501, 502, 503, 504)  # tightened per call below
CSV_HEAD = "First Name,Last Name,Company Name,email,Designation"


def totp_now(secret, offset=0):
    s = secret.strip().replace(" ", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    step = int(time.time() // 30) + offset
    d = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = d[-1] & 15
    return str(((struct.unpack(">I", d[o:o + 4])[0]) & 0x7FFFFFFF) % 10 ** 6).zfill(6)


def email(t, tag, dom=None):
    return f"{tag}-{t.rid}@{dom or 'iso' + t.label + t.rid + '.example'}"


def create_phase(E, t):
    """All resource creation + every read/update endpoint happy path."""
    h = lambda ep, **kw: E.happy(ep, t, **kw)  # noqa: E731
    ids = t.ids
    mk = f"{t.label}{t.rid}{uuid.uuid4().hex[:3]}"
    ids["marker"] = mk
    ids["user_id"] = t.uid["SUPER_ADMIN"]
    ids["owner_id"] = t.uid["SUPER_ADMIN"]
    owner = t.uid["SUPER_ADMIN"]

    # ---- meta / public
    for ep in ("GET /", "GET /health", "GET /health/ready"):
        h(ep, who=None)

    # ---- company / sender identity
    h("PUT /api/company-profiles/sender-identity",
      body={"company_name": f"Sweep Co {mk}", "postal_address": "1 Test Street, Austin, TX 78701, USA"})
    h("GET /api/company-profiles/sender-identity")
    h("POST /api/company-profiles", body={"profile_name": f"Profile {mk}", "company_name": f"Co {mk}",
                                          "default_sender_name": "Sweeper", "postal_address": "1 Test St, Austin"},
      save={"profile_id": "profile_id"})
    h("GET /api/company-profiles")
    h("GET /api/company-profiles/{profile_id}")
    h("PUT /api/company-profiles/{profile_id}", body={"company_description": f"desc {mk}"})

    # ---- users / invitations
    s, b = h("POST /api/users/invite", body={"email": f"victim-{mk}@systest.example", "role": "AGENT"},
             save={"victim_user_id": "user_id", "invitation_id": "invitation_id"})
    h("GET /api/users")
    h("GET /api/users/invitations")
    h("GET /api/users/{user_id}/permissions", pp={"user_id": ids.get("victim_user_id", "x")})
    h("PUT /api/users/{user_id}/permissions", pp={"user_id": ids.get("victim_user_id", "x")},
      body={"permissions": ["manage_prospects"]})
    h("GET /team/hierarchy")
    ids["user_id"] = ids.get("victim_user_id", owner)
    h("PUT /team/hierarchy/{user_id}", body={"sales_level": 1, "manager_id": None})
    ids["user_id"] = owner

    # ---- sales config
    h("GET /sales/settings")
    h("PUT /sales/settings", body={"default_recycle_days": 90})
    h("GET /sales/stages")
    h("POST /sales/stages", body={"name": f"Stage {mk}", "probability": 40, "sort_order": 50},
      save={"stage_id": "stage_id"})
    h("PATCH /sales/stages/{stage_id}", body={"name": f"Stage {mk} b", "probability": 45})
    h("GET /sales/targets", query={"fy": 2031})
    h("PUT /sales/targets", body={"user_id": owner, "fy": 2031, "quarter": 1, "amount": 5000})
    h("POST /products", body={"name": f"Prod {mk}", "sku": f"SKU-{mk}", "unit_price": 10, "unit": "each"},
      save={"product_id": "product_id"})
    h("GET /products")
    h("PATCH /products/{product_id}", body={"description": "updated", "unit_price": 12})

    # ---- accounts
    h("POST /accounts", body={"name": f"Acct {mk}", "domain": f"acct{mk}.example.com", "industry": "Software"},
      save={"account_id": "account_id"})
    h("GET /accounts")
    h("GET /accounts/{account_id}")
    h("PATCH /accounts/{account_id}", body={"phone": "+15125550100", "description": "x"})
    h("POST /accounts/backfill")

    # ---- contacts (and a second/third for merge/bulk/duplicates)
    cdom = f"c{mk}.example.com"
    h("POST /contacts", body={"email": f"alpha@{cdom}", "first_name": "Alpha", "last_name": mk,
                              "company_name": f"Acct {mk}", "account_id": ids["account_id"],
                              "designation": "VP Sales"}, save={"prospect_id": "prospect_id"})
    for tag in ("beta", "gamma", "delta"):
        s, b = h("POST /contacts", body={"email": f"{tag}@{cdom}", "first_name": tag.title(), "last_name": mk,
                                         "company_name": f"Acct {mk}"}, keep=False)
        if isinstance(b, dict):
            ids[f"prospect_{tag}"] = b.get("prospect_id")
    h("GET /contacts")
    h("GET /contacts/{prospect_id}")
    h("PATCH /contacts/{prospect_id}", body={"designation": "CRO", "tags": ["sweep"]})
    h("GET /contacts/board", query={"group_by": "lifecycle_stage"})
    h("GET /contacts/facets")
    h("GET /contacts/meta")
    h("GET /contacts/owners")
    h("GET /contacts/duplicates")
    h("GET /contacts/export", query={"format": "csv"})
    h("GET /contacts/deleted")
    h("GET /contacts/fields")
    h("POST /contacts/fields", body={"label": f"CF {mk}", "field_type": "TEXT"}, save={"field_id": "field_id"})
    h("PATCH /contacts/fields/{field_id}", body={"label": f"CF {mk} b", "field_type": "TEXT"})
    h("POST /contacts/{prospect_id}/activities", body={"activity_type": "NOTE", "subject": f"note {mk}", "body": "b"},
      save={"activity_id": "activity_id"})
    h("PATCH /contacts/activities/{activity_id}", body={"subject": f"note {mk} b"})
    h("GET /contacts/{prospect_id}/history")
    h("GET /contacts/{prospect_id}/timeline")
    h("POST /contacts/bulk", body={"prospect_ids": [ids["prospect_beta"]], "action": "add_tags", "tags": ["bulk"]})

    # ---- lists
    h("POST /lists", body={"list_name": f"List {mk}", "list_type": "STATIC"}, save={"list_id": "list_id"})
    h("GET /lists")
    h("GET /lists/{list_id}")
    h("PATCH /lists/{list_id}", body={"description": "d"})
    h("POST /lists/preview", body={"filters": {"op": "AND", "conditions": [{"field": "lifecycle_stage", "operator": "is", "value": "LEAD"}]}})
    h("POST /lists/{list_id}/members", body={"prospect_ids": [ids["prospect_id"], ids["prospect_beta"], ids["prospect_gamma"]]})
    h("POST /lists/{list_id}/members/remove", body={"prospect_ids": [ids["prospect_gamma"]]})

    # ---- upload -> prospect list (+ import)
    csv = "\n".join([CSV_HEAD] + [f"U{i},Ser,Acme {mk},up{i}@u{mk}.example.com,CEO" for i in range(3)]) + "\n"
    s, b = h("POST /uploads/validations", files={"file": (f"u-{mk}.csv", csv.encode(), "text/csv")},
             save={"upload_id": "upload_id"})
    recs = b.get("records") if isinstance(b, dict) else None
    if recs:
        s, c = h("POST /uploads/{upload_id}/confirmations", body={"upload_id": ids["upload_id"], "title": f"Upl {mk}", "records": recs},
                 save={"plist_id": "list_id"})
        h("POST /uploads/{upload_id}/revalidations", body={"records": recs})
    h("GET /prospect-lists")
    pl = ids.get("plist_id") or ids.get("list_id")
    ids["list_id_crm"] = ids["list_id"]
    if pl:
        ids["list_id"] = pl
        h("GET /prospect-lists/{list_id}/prospects")
        s, b = http("GET", f"/prospect-lists/{pl}/prospects", token=t.tok["SUPER_ADMIN"])
        pp = ((b.get("prospects") or b.get("items") or [{}])[0]).get("prospect_id") if isinstance(b, dict) else None
        if pp:
            ids["plist_prospect_id"] = pp
            h("PATCH /prospect-lists/{list_id}/prospects/{prospect_id}/notes", pp={"prospect_id": pp}, body={"notes": "n"})
            h("PATCH /prospects/{prospect_id}", pp={"prospect_id": pp}, body={"designation": "CTO"})
        h("POST /prospect-lists/{list_id}/segmentations/previews", body={"list_id": pl, "persona_types": None})
        h("POST /prospect-lists/{list_id}/revalidations", env=(400, 422, 502, 503))
        ids["list_id"] = ids["list_id_crm"]
    h("GET /prospects/stats")
    h("POST /prospects/classifications/test", body={"designation": "VP Sales", "company_name": "Acme"}, env=(400, 422, 502, 503))
    h("POST /prospects/classifications/batch", body={"prospect_ids": [ids["prospect_id"]]}, env=(400, 422, 502, 503))

    csv2 = "Email,First Name,Company\n" + "\n".join(f"imp{i}@imp{mk}.example.com,I{i},ImpCo {mk}" for i in range(3)) + "\n"
    h("POST /imports/preview", files={"file": (f"imp-{mk}.csv", csv2.encode(), "text/csv")})
    s, b = h("POST /imports", files={"file": (f"imp-{mk}.csv", csv2.encode(), "text/csv")},
             form={"mapping": json.dumps({"Email": "contact.email", "First Name": "contact.first_name", "Company": "company.name"}),
                   "options": json.dumps({})}, save={"import_id": "import_id"})
    h("GET /imports")
    h("GET /imports/{import_id}")
    h("GET /imports/{import_id}/errors.csv")

    # ---- views / reports / workflow / templates library
    h("POST /views", body={"name": f"View {mk}", "object_type": "CONTACT", "shared": False, "filters": None}, save={"view_id": "view_id"})
    h("GET /views", query={"object_type": "CONTACT"})
    h("PATCH /views/{view_id}", body={"name": f"View {mk} b"})
    h("GET /reports/custom/meta")
    h("POST /reports/custom/run", body={"object": "contacts", "group_by": "lifecycle_stage"})
    h("POST /reports/custom", body={"name": f"Rep {mk}", "definition": {"object": "contacts", "group_by": "lifecycle_stage"}},
      save={"report_id": "report_id"})
    h("GET /reports/custom")
    h("GET /reports/custom/{report_id}")
    h("PATCH /reports/custom/{report_id}", body={"name": f"Rep {mk} b"})
    h("GET /workflow/meta")
    h("POST /workflow/rules", body={"name": f"WF {mk}", "object_type": "CONTACT", "field": "lifecycle_stage", "operator": "equals",
                                    "value": "LEAD", "actions": [{"type": "notify", "to": ["owner"]}]}, save={"wf_rule_id": "rule_id"})
    h("GET /workflow/rules")
    h("PATCH /workflow/rules/{rule_id}", body={"name": f"WF {mk} b"})
    h("POST /template-library", body={"name": f"TL {mk}", "category": "General", "subject": "Hello {{first_name}}",
                                      "body": "Body {{first_name}}"}, save={"tlib_id": "template_id"})
    h("GET /template-library")
    h("PATCH /template-library/{template_id}", body={"subject": "Hello again"})
    h("POST /template-library/{template_id}/render", body={"prospect_id": ids["prospect_id"], "count_use": False})

    # ---- leads / opportunities / tasks / proposals
    h("POST /leads", body={"prospect_id": ids["prospect_id"], "source": "WEB"}, save={"lead_id": "lead_id"})
    h("GET /leads")
    h("GET /leads/board")
    h("GET /leads/meta")
    h("GET /leads/reply-suggestions")
    h("GET /leads/sql-queue")
    h("GET /leads/{lead_id}")
    h("PATCH /leads/{lead_id}", body={"next_step": "call", "source": "WEB"})
    h("POST /opportunities", body={"name": f"Opp {mk}", "account_id": ids["account_id"], "prospect_id": ids["prospect_id"],
                                   "amount": 1000, "close_date": "2031-06-01"}, save={"opportunity_id": "opportunity_id"})
    h("GET /opportunities")
    h("GET /opportunities/board")
    h("GET /opportunities/{opportunity_id}")
    h("PATCH /opportunities/{opportunity_id}", body={"next_step": "demo", "amount": 1500})
    h("POST /opportunities/{opportunity_id}/activities", body={"activity_type": "NOTE", "subject": "hi"})
    h("GET /opportunities/{opportunity_id}/timeline")
    h("GET /pipeline/revenue")
    h("POST /tasks", body={"title": f"Task {mk}", "due_at": "2031-01-01T10:00:00Z", "prospect_id": ids["prospect_id"]},
      save={"task_id": "task_id"})
    h("GET /tasks")
    h("PATCH /tasks/{task_id}", body={"notes": "n", "priority": "HIGH"})
    h("POST /proposals", body={"opportunity_id": ids["opportunity_id"], "title": f"Prop {mk}", "amount": 100},
      save={"proposal_id": "proposal_id"})
    h("GET /proposals")
    h("PUT /proposals/{proposal_id}/lines", body={"lines": [{"product_id": ids["product_id"], "description": "x", "quantity": 2, "unit_price": 10}]})
    h("GET /proposals/{proposal_id}/lines")
    h("PATCH /proposals/{proposal_id}", body={"notes": "n"})
    h("GET /proposals/{proposal_id}/pdf")

    # ---- inbox / campaigns
    h("POST /inboxes", body={"email_address": f"sw1@sw{mk}.example.com", "smtp_host": "smtp.invalid", "smtp_username": "sw1",
                             "smtp_password": "x", "warmup_enabled": False, "daily_limit": 500, "max_emails_per_day": 500,
                             "delay_between_emails": 1}, save={"inbox_id": "inbox_id"})
    h("GET /inboxes")
    h("GET /inboxes/{inbox_id}")
    h("PUT /inboxes/{inbox_id}", body={"daily_limit": 400})
    h("GET /inboxes/warmup/overview")
    h("GET /inboxes/{inbox_id}/warmup")
    h("POST /inboxes/warmup/run")
    h("GET /inboxes/oauth/microsoft/status")
    h("POST /inboxes/{inbox_id}/oauth/microsoft/start", env=(400, 422, 501, 502, 503))
    h("GET /inboxes/{inbox_id}/test-imap", env=(400, 422, 502, 503, 504))
    h("POST /inboxes/{inbox_id}/test-smtp", env=(400, 422, 502, 503, 504))
    h("POST /inboxes/{inbox_id}/sync", env=(400, 422, 502, 503, 504))

    camp = {"campaign_name": f"Camp {mk}", "inbox_ids": [ids["inbox_id"]], "respect_timezone": False, "campaign_timezone": "UTC",
            "min_gap_minutes": 0, "send_window_start": "09:00:00", "send_window_end": "17:00:00", "start_date": "2031-01-10"}
    h("POST /campaigns", body=camp, save={"campaign_id": "campaign_id"})
    h("GET /campaigns")
    h("GET /campaigns/lists")
    h("GET /campaigns/{campaign_id}")
    h("PUT /campaigns/{campaign_id}", body={"sender_name": "Sweeper"})
    h("POST /campaigns/{campaign_id}/sequences", body={"step_number": 1, "wait_days": 0}, save={"sequence_id": "sequence_id"})
    h("POST /campaigns/{campaign_id}/sequences", body={"step_number": 2, "wait_days": 2}, keep=False, kind="step2")
    h("GET /campaigns/{campaign_id}/sequences")
    h("PUT /campaigns/sequences/{sequence_id}", body={"wait_days": 1})
    h("POST /campaigns/{campaign_id}/content/stub")
    # (re)generation replaces the campaign's templates: run it before a template_id is picked for the template
    # calls below, else the saved template (and its attachment) are gone by the time DELETE / ISO-intact run
    h("POST /campaigns/{campaign_id}/generations",
      body={"campaign_id": ids["campaign_id"], "list_id": ids["list_id"], "product_name": "p", "company_name": "c", "campaign_description": "d"},
      env=(400, 422, 500, 502, 503))
    h("POST /campaigns/{campaign_id}/personas/{persona_type}/generations",
      body={"list_id": ids["list_id"], "product_name": "p", "company_name": "c", "campaign_description": "d"}, env=(400, 404, 422, 500, 502, 503))
    s, b = http("GET", f"/campaigns/{ids['campaign_id']}/templates", token=t.tok["SUPER_ADMIN"])
    if isinstance(b, dict) and b.get("templates"):
        ids["template_id"] = b["templates"][0]["template_id"]
    h("GET /campaigns/{campaign_id}/templates")
    h("GET /campaigns/{campaign_id}/sequences/{step}/emails")
    h("GET /templates/{template_id}")
    h("PUT /templates/{template_id}", body={"subject": f"Subj {mk}", "body": "Hi {{first_name}}, hello"})
    h("GET /templates/{template_id}/versions")
    h("POST /campaign-wizard/update-template", body={"template_id": ids["template_id"], "subject": f"Subj2 {mk}"})
    h("POST /templates", body={"campaign_id": ids["campaign_id"], "sequence_id": ids["sequence_id"], "subject": f"T2 {mk}",
                               "body": "Hi {{first_name}}", "designation": "VP"}, keep=False, save={"template_id2": "template_id"})
    h("POST /templates/{template_id}/attachments", files={"file": (f"a-{mk}.txt", b"attachment body", "text/plain")},
      save={"attachment_id": "attachment_id"})
    h("GET /templates/{template_id}/attachments")
    h("GET /templates/{template_id}/attachments/{attachment_id}/download")
    h("POST /templates/{template_id}/approve")
    h("POST /campaigns/{campaign_id}/templates/{template_id}/approve")
    h("POST /campaigns/{campaign_id}/templates/approve-all")
    h("POST /templates/{template_id}/regenerations",
      body={"template_id": ids["template_id"], "field": "subject", "campaign_description": "d", "persona_type": "MARKET_ACCESS",
            "product_name": "p"}, env=(400, 422, 500, 502, 503))
    h("POST /campaigns/{campaign_id}/templates/{template_id}/regenerate", body={"regenerate_subject": True},
      env=(400, 422, 502, 503))
    h("POST /campaigns/schedules/previews", body={"sequence_steps": [{"step_number": 1, "wait_days": 0}]})
    h("POST /campaigns/wizard-creations", body={"campaign_name": f"Wiz {mk}", "send_start_hour": 9, "send_end_hour": 17,
                                                 "timezone": "UTC", "inbox_ids": [ids["inbox_id"]]}, save={"wizard_campaign_id": "campaign_id"})
    h("POST /campaigns/wizard-executions", body={"campaign_name": f"WizEx {mk}", "list_id": ids["list_id"]},
      env=(400, 422, 500, 502, 503))
    h("POST /campaigns/{campaign_id}/prospects", body={"prospect_ids": [ids["prospect_id"], ids["prospect_beta"]]})
    h("POST /campaigns/{campaign_id}/enrollments", body={"campaign_id": ids["campaign_id"], "list_id": ids["list_id"]}, env=(400, 409, 422))
    h("POST /campaigns/{campaign_id}/enrollments/bulk", body={"campaign_id": ids["campaign_id"], "list_ids": [ids["list_id"]]}, env=(400, 409, 422))
    h("GET /campaigns/{campaign_id}/prospects")
    h("POST /campaigns/{campaign_id}/launch")
    ids["message_id"] = sql(f"SELECT message_id FROM email_messages WHERE campaign_id={q(ids['campaign_id'])} LIMIT 1") or None
    h("GET /campaigns/{campaign_id}/schedule")
    h("GET /campaigns/{campaign_id}/metrics")
    h("GET /campaigns/{campaign_id}/analytics")
    h("GET /campaigns/{campaign_id}/audit")
    if ids["message_id"]:
        h("PATCH /campaigns/{campaign_id}/messages/{message_id}/reschedule", body={"new_scheduled_at": "2031-02-10T10:00:00Z"})
        h("POST /campaigns/{campaign_id}/schedule/reschedule-date",
          body={"date": "2031-02-10", "step_number": 1, "new_scheduled_at": "2031-02-12T10:00:00Z"}, env=(400, 404, 422))
    h("POST /campaigns/{campaign_id}/pause", query={"reason": "sweep"})
    h("POST /campaigns/{campaign_id}/resume")
    h("POST /campaigns/{campaign_id}/activations", body={"campaign_id": ids["campaign_id"]}, expect=OK2 + (400, 409))
    h("POST /campaigns/{campaign_id}/send-now", expect=OK2 + (400, 409))
    # mark-opened needs a SENT message; whether send-now's worker pass has sent one yet is timing, so make one SENT
    sql(f"UPDATE email_messages SET status='SENT', sent_at=UTC_TIMESTAMP() WHERE campaign_id={q(ids['campaign_id'])} "
        f"AND prospect_id={q(ids['prospect_id'])} AND direction='OUTBOUND' ORDER BY scheduled_at LIMIT 1")
    h("POST /campaigns/{campaign_id}/prospects/{prospect_id}/mark-opened")
    h("POST /automation-rules/", body={"name": f"AR {mk}", "trigger_type": "EMAIL_OPENED", "action_type": "PAUSE_PROSPECT",
                                       "campaign_id": ids["campaign_id"]}, save={"auto_rule_id": "rule_id"})
    h("GET /automation-rules/", query={"campaign_id": ids["campaign_id"]})
    h("PUT /automation-rules/{rule_id}", body={"description": "d"})
    h("POST /campaign-drafts/", body={"draft_name": f"Draft {mk}", "current_step": 1, "draft_data": {"a": 1}}, save={"draft_id": "draft_id"})
    h("GET /campaign-drafts/")
    h("GET /campaign-drafts/current")
    h("GET /campaign-drafts/{draft_id}")


    # ---- second campaign: draft -> approve -> activate; bounce + unsubscribe targets; bounced-prospect revalidation
    h("POST /campaigns", body={**camp, "campaign_name": f"Camp2 {mk}", "start_date": "2031-01-12"}, keep=False, kind="camp2",
      save={"campaign_id2": "campaign_id"})
    c2 = ids["campaign_id2"]
    http("POST", f"/campaigns/{c2}/sequences", {"step_number": 1, "wait_days": 0}, t.tok["SUPER_ADMIN"])
    http("POST", f"/campaigns/{c2}/content/stub", None, t.tok["SUPER_ADMIN"])
    http("POST", f"/campaigns/{c2}/templates/approve-all", None, t.tok["SUPER_ADMIN"])
    http("POST", f"/campaigns/{c2}/prospects", {"prospect_ids": [ids["prospect_delta"], ids["prospect_gamma"]]}, t.tok["SUPER_ADMIN"])
    h("POST /campaigns/{campaign_id}/activations", pp={"campaign_id": c2}, body={"campaign_id": c2}, expect=OK2 + (400, 409))
    h("GET /campaigns/bounced-prospects/revalidation-candidates")
    rows2 = sql(f"SELECT message_id, prospect_id FROM email_messages WHERE campaign_id={q(c2)}").splitlines()
    msgs2 = {r.split("\t")[1]: r.split("\t")[0] for r in rows2 if "\t" in r}
    ids["message_id_gamma"] = msgs2.get(ids["prospect_gamma"])
    dmsg = msgs2.get(ids["prospect_delta"])
    if dmsg:
        ev = {"notificationType": "Bounce", "eventType": "Bounce",
              "mail": {"timestamp": "2031-01-01T00:00:00Z", "messageId": f"ses-{uuid.uuid4().hex}", "source": "s@example.com",
                       "destination": [f"delta@{cdom}"], "tags": {"message_id": [dmsg]}},
              "bounce": {"bounceType": "Permanent", "bounceSubType": "General", "timestamp": "2031-01-01T00:00:00Z",
                         "bouncedRecipients": [{"emailAddress": f"delta@{cdom}", "diagnosticCode": "smtp; 550 5.1.1 user unknown"}]}}
        http("POST", "/webhooks/ses/notifications", ev, query={"token": "local-dev-webhook-token"})
        h("PATCH /campaigns/prospects/{prospect_id}/revalidate", pp={"prospect_id": ids["prospect_delta"]},
          body={"new_email": f"delta2@{cdom}", "reason": "corrected typo"}, expect=OK2 + (400, 404, 409), keep=False)
    h("POST /campaigns/{campaign_id}/prospects/{prospect_id}/mark-replied", expect=OK2 + (400, 404, 409))
    # imap configured but unreachable: must be a clean 5xx-gateway/4xx, not a bare 500
    http("PUT", f"/inboxes/{ids['inbox_id']}", {"imap_host": "imap.invalid", "imap_username": "u", "imap_password": "p"}, t.tok["SUPER_ADMIN"])
    h("POST /inboxes/{inbox_id}/sync", query={"days": 7}, env=(400, 422, 424, 502, 503, 504), kind="imap-configured")
    http("PUT", f"/inboxes/{ids['inbox_id']}", {"imap_host": "", "imap_password": ""}, t.tok["SUPER_ADMIN"])

    # ---- user connection (OAuth unavailable locally -> seeded row in own tenant)
    cn = str(uuid.uuid4())
    sql(f"INSERT INTO user_connections (connection_id,tenant_id,user_id,provider,account_email,sync_email,sync_calendar,created_at) "
        f"VALUES ({q(cn)},{q(t.tenant_id)},{q(owner)},'GOOGLE',{q('g' + mk + '@example.com')},1,1,NOW())")
    ids["connection_id"] = cn
    h("GET /connections")
    h("PATCH /connections/{connection_id}", body={"sync_calendar": False})
    h("POST /connections/{connection_id}/sync", env=(400, 401, 424, 502, 503, 504))
    # pending invitation (the invite endpoint stores invitations as ACCEPTED, so seed a PENDING one)
    inv = str(uuid.uuid4())
    sql(f"INSERT INTO invitations (invitation_id,tenant_id,invited_by,email,role,token,status,expires_at,created_at) VALUES "
        f"({q(inv)},{q(t.tenant_id)},{q(owner)},{q('pend-' + mk + '@systest.example')},'AGENT',{q(hashlib.sha256(('tok-' + inv).encode()).hexdigest())},'PENDING',DATE_ADD(NOW(), INTERVAL 5 DAY),NOW())")
    ids["invitation_id"] = inv
    ids["pending_invitation_token"] = "tok-" + inv

    # ---- conversations (created by IMAP/send in production; seeded here in own tenant)
    if ids.get("message_id") and ids.get("inbox_id"):
        cv = str(uuid.uuid4())
        sql(f"INSERT INTO conversations (id,tenant_id,prospect_id,inbox_id,subject,status,is_unread,last_message_at,created_at,updated_at) "
            f"VALUES ({q(cv)},{q(t.tenant_id)},{q(ids['prospect_id'])},{q(ids['inbox_id'])},'Sweep thread','OPEN',1,NOW(),NOW(),NOW())")
        sql(f"UPDATE email_messages SET conversation_id={q(cv)} WHERE message_id={q(ids['message_id'])}")
        sql(f"INSERT INTO email_messages (message_id,campaign_id,conversation_id,prospect_id,inbox_id,direction,status,subject,body_text,to_email,from_email,sent_at) "
            f"SELECT {q(str(uuid.uuid4()))},campaign_id,{q(cv)},prospect_id,inbox_id,'INBOUND','SENT','Re: Sweep thread','Interested!',"
            f"{q('sw1@sw' + mk + '.example.com')},to_email,NOW() FROM email_messages WHERE message_id={q(ids['message_id'])}")
        ids["conversation_id"] = cv
        inbound = sql(f"SELECT message_id FROM email_messages WHERE conversation_id={q(cv)} AND direction='INBOUND' LIMIT 1")
        ids["inbound_message_id"] = inbound
    h("GET /conversations")
    if ids.get("conversation_id"):
        h("GET /conversations/{conversation_id}")
        h("POST /conversations/{conversation_id}/reply", body={"body_text": "Thanks!"}, env=(400, 422, 500, 502, 503))
    if ids.get("inbound_message_id"):
        h("POST /leads/from-message", body={"message_id": ids["inbound_message_id"]}, expect=OK2 + (400, 409), keep=False)
        h("POST /campaigns/{campaign_id}/messages/{message_id}/mark-replied", pp={"message_id": ids["message_id"]})

    # ---- deliverability / reports / misc reads
    for ep in ("GET /deliverability/alert-preferences", "GET /deliverability/alerts", "GET /deliverability/dashboard-alerts",
               "GET /deliverability/domains", "GET /deliverability/integrations/status", "GET /deliverability/statistics",
               "GET /personas", "GET /persona-blueprints", "GET /persona-blueprints/{persona_type}", "GET /search",
               "GET /notifications", "GET /connections", "GET /workflow/meta", "GET /scheduler/status"):
        kw = {"query": {"q": "alpha"}} if ep == "GET /search" else {}
        h(ep, **kw)
    h("PUT /deliverability/alert-preferences", body={"preferences": []}, expect=OK2 + (400, 422))
    h("POST /deliverability/snapshots", env=(400, 422))
    h("GET /deliverability/integrations/events", query={"provider": "ses", "domain_name": f"sw{mk}.example.com"})
    h("GET /deliverability/integrations/metrics", query={"provider": "ses", "domain_name": f"sw{mk}.example.com"})
    h("POST /deliverability/integrations/ingest", body={"provider": "ses", "domain_name": f"sw{mk}.example.com", "payload": {}, "dry_run": True},
      expect=OK2 + (400, 422))
    h("POST /deliverability/domains/{domain_name}/scans", pp={"domain_name": f"sw{mk}.example.com"}, env=(400, 404, 422, 502, 503))
    h("GET /deliverability/domains/{domain_name}/sent-log", pp={"domain_name": f"sw{mk}.example.com"}, expect=OK2 + (404,))
    h("PUT /notifications/preferences", body={"email": True})
    h("POST /notifications/read", body={"ids": None})
    for ep in ("GET /reports/dashboard-summary", "GET /reports/global-analytics", "GET /reports/campaign-comparison",
               "GET /reports/provider-performance", "GET /reports/export/campaign-comparison", "GET /reports/export/deliverability",
               "GET /reports/export/global-analytics", "GET /reports/export/global-analytics/docx",
               "GET /reports/export/prospects/{campaign_id}",
               "GET /sales-reports/campaign-roi", "GET /sales-reports/daily-limit", "GET /sales-reports/dashboard",
               "GET /sales-reports/forecast", "GET /sales-reports/funnel", "GET /sales-reports/leaderboard",
               "GET /sales-reports/leads", "GET /sales-reports/pipeline", "GET /sales-reports/targets",
               "GET /sales-reports/team-performance"):
        h(ep)
    # AI / external
    h("POST /emails/compliance-checks", body={"subject": "Hello", "body": "Body text. Unsubscribe here."}, expect=OK2 + (400,))
    h("GET /emails/cta-options/{purpose}", pp={"purpose": "intro"})
    h("POST /emails/generations", body={"prospect_id": ids["prospect_id"], "product_name": "p"}, env=(400, 422, 500, 502, 503))
    h("POST /emails/sequences/generations", body={"prospect_id": ids["prospect_id"], "product_name": "p"}, env=(400, 422, 500, 502, 503))
    h("POST /emails/validations", body={"email": f"alpha@{cdom}"}, expect=OK2 + (400,))
    h("POST /prospects/classifications/test", body={"designation": "CEO"}, env=(400, 422, 502, 503), kind="b", check="HAPPY")
    # connections (OAuth providers are not configured locally)
    h("POST /connections/{provider}/start", env=(400, 404, 422, 501, 502, 503))

    # ---- tracking (public)
    mid = ids.get("message_id")
    if mid:
        h("GET /tracking/open/{message_id}.png", who=None)
        dest = f"https://example.com/{mk}"
        h("GET /tracking/click/{message_id}", who=None, query={"url": dest}, follow=False)
        sig = hmac.new(b"local-dev-only-change-me-0123456789abcdef", f"click|{mid}|{dest}".encode(), hashlib.sha256).hexdigest()[:32]
        h("GET /tracking/click/{message_id}", who=None, query={"url": dest, "sig": sig}, expect=(302, 307), follow=False, kind="signed", keep=False)
        h("GET /tracking/unsubscribe/{message_id}", who=None)
        if ids.get("message_id_gamma"):
            h("POST /tracking/unsubscribe/{message_id}", who=None, pp={"message_id": ids["message_id_gamma"]}, form={"confirm": "1"})
    # SES webhook (token-protected)
    h("POST /webhooks/ses/notifications", who=None, query={"token": "local-dev-webhook-token"},
      body={"notificationType": "Delivery", "eventType": "Delivery", "mail": {"messageId": "zz-unknown", "timestamp": "2031-01-01T00:00:00Z", "source": "a@b.co", "destination": ["x@y.co"]}, "delivery": {"timestamp": "2031-01-01T00:00:00Z", "recipients": ["x@y.co"]}},
      expect=OK2 + (400,))


def tail_phase(E, t):
    """Destructive endpoints + idempotency (second DELETE -> 404) on fresh victims."""
    mk = t.ids["marker"]
    h = lambda ep, **kw: E.happy(ep, t, **kw)  # noqa: E731
    tok = t.tok["SUPER_ADMIN"]

    def twice(ep, pp, tag, ok=OK2):
        s, b = h(ep, pp=pp, kind=tag)
        E.idem_second(ep, t, pp, tag, s)

    # accounts: delete -> restore
    s, b = http("POST", "/accounts", {"name": f"Victim {mk}", "domain": f"v{mk}.example.com"}, tok)
    aid = b["account_id"]
    twice("DELETE /accounts/{account_id}", {"account_id": aid}, "acct")
    h("GET /accounts/deleted")
    h("POST /accounts/{account_id}/restore", pp={"account_id": aid})
    # contacts: delete, restore, bulk delete, merge
    s, b = http("POST", "/contacts", {"email": f"victim@v{mk}.example.com", "first_name": "V", "last_name": "X"}, tok)
    pid = b["prospect_id"]
    twice("DELETE /contacts/{prospect_id}", {"prospect_id": pid}, "contact")
    h("POST /contacts/restore", body={"prospect_ids": [pid]})
    s, b = http("POST", "/contacts", {"email": f"dup1@v{mk}.example.com", "first_name": "D", "last_name": "One"}, tok)
    dup = b["prospect_id"]
    h("POST /contacts/merge", body={"primary_id": pid, "duplicate_ids": [dup], "choices": {}})
    s, b = http("POST", "/contacts/{x}".replace("{x}", pid) + "/activities", {"activity_type": "NOTE", "subject": "s"}, tok)
    act = b.get("activity_id") if isinstance(b, dict) else None
    if act:
        twice("DELETE /contacts/activities/{activity_id}", {"activity_id": act}, "activity")
    s, b = http("POST", "/contacts/fields", {"label": f"CFV {mk}", "field_type": "TEXT"}, tok)
    twice("DELETE /contacts/fields/{field_id}", {"field_id": b["field_id"]}, "field")
    # lists
    s, b = http("POST", "/lists", {"list_name": f"LV {mk}", "list_type": "STATIC"}, tok)
    twice("DELETE /lists/{list_id}", {"list_id": b["list_id"]}, "list")
    # leads / opps / tasks / proposals / views / reports / templates library / rules
    s, b = http("POST", "/contacts", {"email": f"lv@v{mk}.example.com", "first_name": "L", "last_name": "V"}, tok)
    lp = b["prospect_id"]
    s, b = http("POST", "/leads", {"prospect_id": lp, "source": "WEB"}, tok)
    lid = b["lead_id"]
    http("PATCH", f"/leads/{lid}", {"qualification": {"budget": True, "authority": True, "need": True, "timeline": True}, "stage": "SQL"}, tok)
    h("POST /leads/{lead_id}/convert", pp={"lead_id": lid}, body={"name": f"Conv {mk}", "amount": 100, "close_date": "2031-05-05"},
      save={"converted_opp": "opportunity_id"})
    s, b = http("POST", "/contacts", {"email": f"lv2@v{mk}.example.com", "first_name": "L", "last_name": "V2"}, tok)
    s, b = http("POST", "/leads", {"prospect_id": b["prospect_id"], "source": "WEB"}, tok)
    twice("DELETE /leads/{lead_id}", {"lead_id": b["lead_id"]}, "lead")
    s, b = http("POST", "/opportunities", {"name": f"OV {mk}", "amount": 1}, tok)
    oid = b["opportunity_id"]
    s2, b2 = http("POST", "/proposals", {"opportunity_id": oid, "title": f"PV {mk}", "amount": 1}, tok)
    twice("DELETE /proposals/{proposal_id}", {"proposal_id": b2["proposal_id"]}, "proposal")
    twice("DELETE /opportunities/{opportunity_id}", {"opportunity_id": oid}, "opp")
    s, b = http("POST", "/tasks", {"title": f"TV {mk}"}, tok)
    twice("DELETE /tasks/{task_id}", {"task_id": b["task_id"]}, "task")
    s, b = http("POST", "/views", {"name": f"VV {mk}", "object_type": "CONTACT"}, tok)
    twice("DELETE /views/{view_id}", {"view_id": b["view_id"]}, "view")
    s, b = http("POST", "/reports/custom", {"name": f"RV {mk}", "definition": {"object": "contacts", "group_by": "lifecycle_stage"}}, tok)
    twice("DELETE /reports/custom/{report_id}", {"report_id": b["report_id"]}, "report")
    s, b = http("POST", "/template-library", {"name": f"TLV {mk}", "subject": "s", "body": "b"}, tok)
    twice("DELETE /template-library/{template_id}", {"template_id": b["template_id"]}, "tlib")
    s, b = http("POST", "/workflow/rules", {"name": f"WV {mk}", "object_type": "CONTACT", "field": "lifecycle_stage", "operator": "equals",
                                            "value": "LEAD", "actions": [{"type": "notify", "to": ["owner"]}]}, tok)
    twice("DELETE /workflow/rules/{rule_id}", {"rule_id": b.get("rule_id") or b.get("id")}, "wf")
    s, b = http("POST", "/api/company-profiles", {"profile_name": f"PV {mk}", "company_name": "c"}, tok)
    twice("DELETE /api/company-profiles/{profile_id}", {"profile_id": b["profile_id"]}, "profile")
    s, b = http("POST", "/automation-rules/", {"name": f"ARV {mk}", "trigger_type": "EMAIL_OPENED", "action_type": "PAUSE_PROSPECT",
                                              "campaign_id": t.ids["campaign_id"]}, tok)
    twice("DELETE /automation-rules/{rule_id}", {"rule_id": b["rule_id"]}, "autorule")
    s, b = http("POST", "/campaign-drafts/", {"draft_name": f"DV {mk}", "current_step": 1, "draft_data": {"a": 1}}, tok)
    twice("DELETE /campaign-drafts/{draft_id}", {"draft_id": b["draft_id"]}, "draft")
    h("DELETE /campaign-drafts/", expect=OK2)
    # campaigns: sequences, prospects, campaign, templates
    s, b = http("POST", "/inboxes", {"email_address": f"cv@sw{mk}.example.com", "smtp_host": "smtp.invalid", "smtp_username": "c", "smtp_password": "x"}, tok)
    cb = {"campaign_name": f"CV {mk}", "inbox_ids": [b["inbox_id"]], "respect_timezone": False, "campaign_timezone": "UTC"}
    s, b = http("POST", "/campaigns", cb, tok)
    cid = b["campaign_id"]
    s, b = http("POST", f"/campaigns/{cid}/sequences", {"step_number": 1, "wait_days": 0}, tok)
    twice("DELETE /campaigns/sequences/{sequence_id}", {"sequence_id": b["sequence_id"]}, "seq")
    s, b = http("POST", "/contacts", {"email": f"enr@v{mk}.example.com", "first_name": "E", "last_name": "V"}, tok)
    ep_ = b["prospect_id"]
    http("POST", f"/campaigns/{cid}/prospects", {"prospect_ids": [ep_]}, tok)
    twice("DELETE /campaigns/{campaign_id}/prospects/{prospect_id}", {"campaign_id": cid, "prospect_id": ep_}, "enrolled")
    twice("DELETE /campaigns/{campaign_id}", {"campaign_id": cid}, "campaign")
    if t.ids.get("template_id") and t.ids.get("attachment_id"):
        twice("DELETE /templates/{template_id}/attachments/{attachment_id}", {}, "attach")
    if t.ids.get("template_id2"):
        twice("DELETE /templates/{template_id}", {"template_id": t.ids["template_id2"]}, "template")
    # inbox delete
    s, b = http("POST", "/inboxes", {"email_address": f"del@sw{mk}.example.com", "smtp_host": "smtp.invalid", "smtp_username": "d",
                                     "smtp_password": "x"}, tok)
    twice("DELETE /inboxes/{inbox_id}", {"inbox_id": b["inbox_id"]}, "inbox")
    # prospect lists / prospects
    csv = "\n".join([CSV_HEAD, f"Z,Ser,Acme {mk},zz@z{mk}.example.com,CEO"]) + "\n"
    s, b = http("POST", "/uploads/validations", files={"file": (f"z-{mk}.csv", csv.encode(), "text/csv")}, token=tok)
    s2, c = http("POST", f"/uploads/{b['upload_id']}/confirmations", {"upload_id": b["upload_id"], "title": f"ZL {mk}", "records": b["records"]}, tok)
    zl = c.get("list_id") or (c.get("prospect_list") or {}).get("list_id")
    s3, items = http("GET", f"/prospect-lists/{zl}/prospects", token=tok)
    zp = (items.get("prospects") or items.get("items"))[0]["prospect_id"]
    twice("DELETE /prospects/{prospect_id}", {"prospect_id": zp}, "prospect")
    twice("DELETE /prospect-lists/{list_id}", {"list_id": zl}, "plist")
    # users: soft delete, reactivate, role, permanent, invitation delete, permissions reset
    s, b = http("POST", "/api/users/invite", {"email": f"v2-{mk}@systest.example", "role": "AGENT"}, tok)
    uid, inv = b["user_id"], b["invitation_id"]
    h("PUT /api/users/{user_id}/role", pp={"user_id": uid}, body={"role": "MANAGER"})
    h("DELETE /api/users/{user_id}/permissions", pp={"user_id": uid})
    twice("DELETE /api/users/{user_id}", {"user_id": uid}, "user")
    h("PUT /api/users/{user_id}/reactivate", pp={"user_id": uid})
    http("DELETE", f"/api/users/{uid}", token=tok)
    twice("DELETE /api/users/{user_id}/permanent", {"user_id": uid}, "userperm")
    twice("DELETE /api/users/invitations/{invitation_id}", {"invitation_id": t.ids["invitation_id"]}, "invite")
    twice("DELETE /deliverability/domains/{domain_name}", {"domain_name": f"sw{mk}.example.com"}, "domain")
    twice("DELETE /connections/{connection_id}", {"connection_id": t.ids["connection_id"]}, "connection")


def auth_phase(E, A):
    """Authentication endpoints (public + self-service). Uses throw-away AGENT_ONLY_* users for password/MFA changes."""
    rec = E.rec
    mk = A.ids["marker"]
    # register: performed by the fixture (response captured there)
    ep = E.EP["POST /api/auth/register"]
    rec.rec(ep["id"], "POST", ep["path"], "HAPPY", "", A.register_status == 201, f"status {A.register_status}",
            evidence={"resp": str(A.register_response)[:300]}, title="POST /api/auth/register creates tenant + returns tokens")
    E.hits_ok.add(ep["id"])
    E.check_schema(ep, 201, A.register_response)
    h = lambda ep_, **kw: E.happy(ep_, A, **kw)  # noqa: E731
    h("GET /api/auth/me")
    h("GET /api/auth/session")
    h("PUT /api/auth/me", body={"first_name": "Sweeper"})
    h("PUT /api/auth/mfa/tenant-policy", body={"require_mfa": False})
    h("GET /api/auth/google", who=None, env=(400, 404, 501, 503))
    h("POST /api/auth/google/callback", who=None, body={"credential": "garbage"}, env=(400, 401, 403, 501, 503))
    # login / refresh / logout on a throw-away user
    u = "AGENT_ONLY_manage_team"
    s, b = h("POST /api/auth/login", who=None, body={"email": A.email[u], "password": A.password})
    rt = b.get("refresh_token") if isinstance(b, dict) else None
    if rt:
        s, b2 = h("POST /api/auth/refresh", who=None, body={"refresh_token": rt})
        rt2 = b2.get("refresh_token") if isinstance(b2, dict) else None
        h("POST /api/auth/logout", who=None, body={"refresh_token": rt2 or rt}, expect=(200, 204))
    # forgot / reset password
    h("POST /api/auth/forgot-password", who=None, body={"email": A.email["AGENT_ONLY_view_analytics"]})
    ru = A.uid["AGENT_ONLY_view_analytics"]
    tok = "reset-" + uuid.uuid4().hex
    sql(f"INSERT INTO password_reset_tokens (token_id,user_id,token_hash,expires_at,created_at) VALUES ({q(str(uuid.uuid4()))},{q(ru)},"
        f"{q(hashlib.sha256(tok.encode()).hexdigest())},DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 MINUTE),UTC_TIMESTAMP())")
    h("POST /api/auth/reset-password", who=None, body={"token": tok, "new_password": A.password})  # same password back
    # magic login
    mu = A.uid["AGENT_ONLY_manage_templates"]
    mtok = "magic-" + uuid.uuid4().hex
    sql(f"INSERT INTO magic_login_tokens (token_id,user_id,token_hash,expires_at,created_at) VALUES ({q(str(uuid.uuid4()))},{q(mu)},"
        f"{q(hashlib.sha256(mtok.encode()).hexdigest())},DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 MINUTE),UTC_TIMESTAMP())")
    h("POST /api/auth/magic-login", who=None, body={"token": mtok})
    # accept invite
    inv2 = str(uuid.uuid4())
    sql(f"INSERT INTO invitations (invitation_id,tenant_id,invited_by,email,role,token,status,expires_at,created_at) VALUES "
        f"({q(inv2)},{q(A.tenant_id)},{q(A.uid['SUPER_ADMIN'])},{q('acc-' + mk + '@systest.example')},'AGENT',{q(hashlib.sha256(('tok-' + inv2).encode()).hexdigest())},'PENDING',DATE_ADD(NOW(), INTERVAL 5 DAY),NOW())")
    h("POST /api/users/accept-invite", who=None, body={"token": "tok-" + inv2, "first_name": "Acc", "last_name": "Epted",
                                                       "password": "Accept-Passw0rd-1x"})
    # MFA lifecycle on another throw-away user
    mfa_u = "AGENT_ONLY_export_data"
    tk = A.tok[mfa_u]
    ok = lambda s_: s_ == 200  # noqa: E731
    s, b = E.happy("POST /api/auth/mfa/setup", A, who=mfa_u)
    secret = b.get("secret") if isinstance(b, dict) else None
    if secret:
        s, b = E.happy("POST /api/auth/mfa/enable", A, who=mfa_u, body={"code": totp_now(secret)})
        codes = b.get("recovery_codes") if isinstance(b, dict) else None
        if codes:
            s, rb = E.happy("POST /api/auth/mfa/recovery-codes", A, who=mfa_u, body={"code": totp_now(secret, 1)})
            codes = (rb.get("recovery_codes") if isinstance(rb, dict) else None) or codes
            # sign in with MFA: login -> mfa_token -> verify with a recovery code
            s, lb, _, _ = _http("POST", "/api/auth/login", {"email": A.email[mfa_u], "password": A.password})
            if isinstance(lb, dict) and lb.get("mfa_token"):
                s, vb = E.happy("POST /api/auth/mfa/verify", A, who=None, body={"mfa_token": lb["mfa_token"], "recovery_code": codes[0]})
            rc = _http("GET", "/api/auth/me", None, tk)  # still valid
            E.happy("POST /api/auth/mfa/disable", A, who=mfa_u, body={"password": A.password, "recovery_code": codes[1]})
    # password change last
    E.happy("PUT /api/auth/me/password", A, who="AGENT_ONLY_manage_inboxes",
            body={"current_password": A.password, "new_password": A.password + "x"})
    # put the password back, so a later run that reuses the cached workspace (.state.json) can change it again
    _http("PUT", "/api/auth/me/password", {"current_password": A.password + "x", "new_password": A.password},
          A.tok["AGENT_ONLY_manage_inboxes"])
