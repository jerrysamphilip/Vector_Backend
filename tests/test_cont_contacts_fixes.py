"""
Functional tests for the contacts gap round (team gap list #3, #5, #24, #28, #29):

  * upload confirm re-validates every ACCEPTED row; bad rows are reported, the rest saved;
    confirming the same upload twice returns the stored result
  * PATCH /prospects/{id} uses the /contacts visibility, email rules and property history
  * CRM import: an over-long value is one row's error; Windows-1252 CSVs decode
  * company domains: free mail refused, syntax checked, subdomains collapse
  * email change: suppressed address refused, validity reset, unsent emails retargeted
"""
import io
import json
import uuid

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


@pytest.fixture(scope="module")
def tenant_ct(client):
    return register_tenant(client, "ct")


def _uid():
    return uuid.uuid4().hex[:6]


def _contact(client, h, label, **kw):
    r = client.post("/contacts", headers=h, json={
        "email": f"{label}.{_uid()}.{RUN}@ctclient{RUN}.example.com", "first_name": label.title(), **kw})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _agent(client, tenant_id, label):
    """An agent allowed to manage prospects, at sales level 3 with no reports: sees own records only."""
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{label}-{RUN}-{_uid()}@ct{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Agent", email=email,
                    password_hash=hash_password(password), role="AGENT", status="ACTIVE", auth_provider="local",
                    email_verified=True, sales_level=3, custom_permissions=["manage_prospects", "manage_campaigns"])
        db.add(user)
        db.commit()
        user_id = user.user_id
    return {"user_id": user_id, "headers": auth(login(client, email, password))}


# ── #3 upload confirm ─────────────────────────────────────────

def _upload_row(n, **kw):
    return {"row": n, "email": f"up{n}.{_uid()}.{RUN}@ctupload{RUN}.example.com", "first_name": f"Up{n}",
            "last_name": "Loader", "company_name": f"Upload Co {RUN}", "status": "ACCEPTED", **kw}


def test_confirm_revalidates_tampered_rows_and_saves_the_rest(client, tenant_ct):
    h = tenant_ct["headers"]
    upload_id = str(uuid.uuid4())
    good1, good2 = _upload_row(1), _upload_row(4)
    records = [
        good1,
        _upload_row(2, email="not-an-email"),                      # tampered: bad format
        _upload_row(3, designation="D" * 400),                     # too long for the column
        good2,
        _upload_row(5, email=f"p.{RUN}@gmail.com"),                # personal email
        _upload_row(6, first_name=""),                             # required name missing
        _upload_row(7, status="REJECTED"),                         # never saved
    ]
    r = client.post(f"/uploads/{upload_id}/confirmations", headers=h,
                    json={"upload_id": upload_id, "title": f"Tampered {RUN}", "records": records})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "CONFIRMED" and body["inserted"] == 2 and body["new_prospects"] == 2
    assert body["rejected"] == 4 and body["failed"] == 0
    reasons = {e["row"]: e["reason"] for e in body["not_added"]}
    assert set(reasons) == {2, 3, 5, 6}
    assert "Invalid or personal email" in reasons[2] and "Invalid or personal email" in reasons[5]
    assert "Designation is too long" in reasons[3]
    assert "Missing required field: First Name" in reasons[6]

    assert sql("SELECT COUNT(*) FROM prospect_list_members WHERE list_id = :l", l=upload_id) == 2
    assert sql("SELECT COUNT(*) FROM prospects WHERE tenant_id = :t AND email IN (:a, :b)",
               t=tenant_ct["tenant_id"], a=good1["email"], b=good2["email"]) == 2
    assert sql("SELECT COUNT(*) FROM prospects WHERE tenant_id = :t AND email = 'not-an-email'",
               t=tenant_ct["tenant_id"]) == 0

    # Confirming the same upload again returns the stored result and inserts nothing
    again = client.post(f"/uploads/{upload_id}/confirmations", headers=h,
                        json={"upload_id": upload_id, "title": f"Tampered {RUN}", "records": records})
    assert again.status_code == 200, again.text
    assert again.json()["already_confirmed"] is True
    assert again.json()["inserted"] == 2 and again.json()["new_prospects"] == 2
    assert sql("SELECT COUNT(*) FROM prospect_list_members WHERE list_id = :l", l=upload_id) == 2
    assert sql("SELECT COUNT(*) FROM prospect_lists WHERE list_id = :l", l=upload_id) == 1


def test_confirm_with_only_bad_rows_creates_no_list(client, tenant_ct):
    h = tenant_ct["headers"]
    upload_id = str(uuid.uuid4())
    r = client.post(f"/uploads/{upload_id}/confirmations", headers=h, json={
        "upload_id": upload_id, "title": "Bad", "records": [_upload_row(1, last_name="L" * 101)]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "NO_RECORDS_ADDED" and r.json()["inserted"] == 0
    assert "Last Name is too long" in r.json()["not_added"][0]["reason"]
    assert sql("SELECT COUNT(*) FROM prospect_lists WHERE list_id = :l", l=upload_id) == 0


# ── #5 PATCH /prospects/{id} ──────────────────────────────────

def test_prospect_patch_respects_visibility_like_contacts(client, tenant_ct):
    h = tenant_ct["headers"]
    pid = _contact(client, h, "vis")  # owned by the tenant owner
    agent = _agent(client, tenant_ct["tenant_id"], "outsider")
    r = client.patch(f"/prospects/{pid}", headers=agent["headers"], json={"first_name": "Hijacked"})
    contacts = client.patch(f"/contacts/{pid}", headers=agent["headers"], json={"first_name": "Hijacked"})
    assert r.status_code == contacts.status_code == 404, (r.text, contacts.text)
    assert sql("SELECT first_name FROM prospects WHERE prospect_id = :p", p=pid) == "Vis"

    own = _contact(client, agent["headers"], "mine")
    r = client.patch(f"/prospects/{own}", headers=agent["headers"], json={"first_name": "Mine2"})
    assert r.status_code == 200, r.text


def test_prospect_patch_validates_email_and_writes_history(client, tenant_ct):
    h = tenant_ct["headers"]
    pid = _contact(client, h, "hist")
    v = client.get(f"/contacts/{pid}", headers=h).json()["row_version"]

    assert client.patch(f"/prospects/{pid}", headers=h, json={"email": "broken@"}).status_code == 422
    new_email = f"Hist.New.{_uid()}.{RUN}@CTclient{RUN}.example.com"
    r = client.patch(f"/prospects/{pid}", headers=h, json={
        "email": new_email, "designation": "CTO", "poc_state": "California", "expected_version": v})
    assert r.status_code == 200, r.text
    assert r.json()["row_version"] == v + 1
    assert sql("SELECT email FROM prospects WHERE prospect_id = :p", p=pid) == new_email.lower()
    assert sql("SELECT timezone FROM prospects WHERE prospect_id = :p", p=pid)
    changed = {sql("SELECT GROUP_CONCAT(field ORDER BY field) FROM property_changes WHERE object_id = :p "
                   "AND source = 'UI' AND field <> 'created'", p=pid)}
    assert {"designation", "email", "poc_state", "timezone"} <= set(next(iter(changed)).split(","))

    # A blank email in an edit form means "leave it"
    r = client.patch(f"/prospects/{pid}", headers=h, json={"email": "", "last_name": "Kept"})
    assert r.status_code == 200, r.text
    assert sql("SELECT email FROM prospects WHERE prospect_id = :p", p=pid) == new_email.lower()

    other = _contact(client, h, "taken")
    taken = sql("SELECT email FROM prospects WHERE prospect_id = :p", p=other)
    assert client.patch(f"/prospects/{pid}", headers=h, json={"email": taken}).status_code == 409


# ── #24 CRM import ─────────────────────────────────────────────

def _import(client, h, content: bytes, mapping, name="c.csv"):
    return client.post("/imports", headers=h, files={"file": (name, io.BytesIO(content), "text/csv")},
                       data={"mapping": json.dumps(mapping)})


def test_import_reports_over_long_value_per_row(client, tenant_ct):
    h = tenant_ct["headers"]
    tag = _uid()
    emails = [f"imp{i}.{tag}.{RUN}@ctimport{RUN}.example.com" for i in range(3)]
    csv = "email,first_name,job title\n" + \
        f"{emails[0]},A,CEO\n{emails[1]},B,{'T' * 200}\n{emails[2]},C,CFO\n"
    r = _import(client, h, csv.encode(), {"email": "contact.email", "first_name": "contact.first_name",
                                          "job title": "contact.designation"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "COMPLETED" and body["contacts_created"] == 2 and body["error_count"] == 1
    assert body["errors"][0]["row"] == 3 and "Job title is too long" in body["errors"][0]["reason"]
    assert sql("SELECT COUNT(*) FROM prospects WHERE email IN (:a, :c)", a=emails[0], c=emails[2]) == 2
    assert sql("SELECT COUNT(*) FROM prospects WHERE email = :b", b=emails[1]) == 0


def test_import_decodes_windows_1252(client, tenant_ct):
    h = tenant_ct["headers"]
    email = f"jose.{_uid()}.{RUN}@ctimport{RUN}.example.com"
    content = f"email,first_name,last_name\n{email},José,Muñoz\n".encode("cp1252")
    r = _import(client, h, content, {"email": "contact.email", "first_name": "contact.first_name",
                                     "last_name": "contact.last_name"})
    assert r.status_code == 201, r.text
    assert r.json()["contacts_created"] == 1
    assert sql("SELECT CONCAT(first_name, ' ', last_name) FROM prospects WHERE email = :e", e=email) == "José Muñoz"


def test_import_refuses_too_many_columns(client, tenant_ct):
    header = ",".join(f"c{i}" for i in range(201))
    r = client.post("/imports/preview", headers=tenant_ct["headers"],
                    files={"file": ("wide.csv", io.BytesIO(f"{header}\n".encode()), "text/csv")})
    assert r.status_code == 400 and "201 columns" in r.json()["detail"]


def test_import_company_domain_rules(client, tenant_ct):
    h = tenant_ct["headers"]
    tag = _uid()
    csv = (f"email,company,domain\n"
           f"a.{tag}@hr.ctsub{tag}.co.uk,Sub Co {tag},\n"
           f"b.{tag}@gmail.com,Free Co {tag},gmail.com\n"
           f"c.{tag}@ctbad{tag}.com,Bad Co {tag},not a domain\n")
    r = _import(client, h, csv.encode(), {"email": "contact.email", "company": "company.name",
                                          "domain": "company.domain"})
    assert r.status_code == 201, r.text
    assert r.json()["contacts_created"] == 2 and r.json()["error_count"] == 1
    assert "not a valid domain" in r.json()["errors"][0]["reason"]
    t = tenant_ct["tenant_id"]
    assert sql("SELECT domain FROM accounts WHERE tenant_id = :t AND name = :n", t=t, n=f"Sub Co {tag}") \
        == f"ctsub{tag}.co.uk"
    assert sql("SELECT COUNT(*) FROM accounts WHERE tenant_id = :t AND domain = 'gmail.com'", t=t) == 0
    assert sql("SELECT domain FROM accounts WHERE tenant_id = :t AND name = :n", t=t, n=f"Free Co {tag}") is None


# ── #28 company domains ───────────────────────────────────────

def test_free_mail_domain_refused_as_company_domain(client, tenant_ct):
    h = tenant_ct["headers"]
    r = client.post("/accounts", headers=h, json={"name": f"Gmail Co {RUN}", "domain": "gmail.com"})
    assert r.status_code == 400 and "personal email provider" in r.json()["detail"]
    r = client.post("/accounts", headers=h, json={"name": f"Orange Co {RUN}", "domain": "orange.fr"})
    assert r.status_code == 400
    r = client.post("/accounts", headers=h, json={"name": f"Bad Co {RUN}", "domain": "bad domain!"})
    assert r.status_code == 400 and "not a valid domain" in r.json()["detail"]

    tag = _uid()
    r = client.post("/accounts", headers=h, json={"name": f"Acme {tag}", "domain": f"https://us.acme{tag}.com/x"})
    assert r.status_code == 201, r.text
    assert r.json()["domain"] == f"acme{tag}.com"
    aid = r.json()["account_id"]
    assert client.patch(f"/accounts/{aid}", headers=h, json={"domain": "yahoo.co.uk"}).status_code == 400
    # A free-mail website alone does not give the company a domain
    r = client.post("/accounts", headers=h, json={"name": f"Site {tag}", "website": "https://gmail.com"})
    assert r.status_code == 201 and r.json()["domain"] is None


def test_contact_at_subdomain_links_to_registrable_company(client, tenant_ct):
    h = tenant_ct["headers"]
    tag = _uid()
    a = client.post("/contacts", headers=h, json={"email": f"a@us.ctreg{tag}.com", "first_name": "A"}).json()
    b = client.post("/contacts", headers=h, json={"email": f"b@mail.ctreg{tag}.com", "first_name": "B"}).json()
    assert a["account_id"] and a["account_id"] == b["account_id"]
    assert sql("SELECT domain FROM accounts WHERE account_id = :a", a=a["account_id"]) == f"ctreg{tag}.com"


# ── #29 email change ──────────────────────────────────────────

def test_email_change_refuses_suppressed_and_retargets_unsent(client, tenant_ct):
    from app.core.clock import utcnow
    from app.models.email_message import EmailMessage
    from app.models.prospect import GlobalUnsubscribe

    h = tenant_ct["headers"]
    t = tenant_ct["tenant_id"]
    pid = _contact(client, h, "chg")
    old = sql("SELECT email FROM prospects WHERE prospect_id = :p", p=pid)
    suppressed = f"gone.{_uid()}.{RUN}@ctclient{RUN}.example.com"
    with SessionLocal() as db:
        db.add(GlobalUnsubscribe(tenant_id=t, email=suppressed, reason="Hard bounce", unsubscribed_at=utcnow()))
        msgs = {status: EmailMessage(prospect_id=pid, direction="OUTBOUND", status=status, to_email=old,
                                     sent_at=utcnow() if status == "SENT" else None)
                for status in ("QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN", "SENT", "CANCELLED")}
        db.add_all(msgs.values())
        db.commit()
        ids = {status: m.message_id for status, m in msgs.items()}
    sql("UPDATE prospects SET is_valid_email = 0 WHERE prospect_id = :p", p=pid)

    for path in (f"/contacts/{pid}", f"/prospects/{pid}"):
        r = client.patch(path, headers=h, json={"email": suppressed.upper()})
        assert r.status_code == 409, r.text
        assert "suppression list" in str(r.json()["detail"])
    assert sql("SELECT email FROM prospects WHERE prospect_id = :p", p=pid) == old

    new = f"chg.new.{_uid()}.{RUN}@ctclient{RUN}.example.com"
    r = client.patch(f"/contacts/{pid}", headers=h, json={"email": new})
    assert r.status_code == 200, r.text
    assert sql("SELECT is_valid_email FROM prospects WHERE prospect_id = :p", p=pid) == 1

    def to(status):
        return sql("SELECT to_email FROM email_messages WHERE message_id = :m", m=ids[status])
    assert to("QUEUED") == to("SCHEDULED") == to("PAUSED_BY_CAMPAIGN") == new
    assert to("SENT") == to("CANCELLED") == old
    assert sql("SELECT COUNT(*) FROM property_changes WHERE object_id = :p AND field = 'email' "
               "AND old_value = :o AND new_value = :n", p=pid, o=old, n=new) == 1
