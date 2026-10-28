"""BRD v2.0 section 8 CRM KPIs through GET /sales-reports/kpis: duplicate contact rate, reply-to-lead
conversion, lead-to-SQL, SQL-to-win, forecast accuracy (actual / committed) and CRM adoption, with the
role scoping (team KPIs for everyone, workspace KPIs for admins and the Sales Head), member= filter,
amount hiding, the committed figure on forecast snapshots and the daily activity recording."""

import uuid
from datetime import date, datetime, timedelta

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql
from app.core.clock import utcnow

KEYS = ["duplicate_contact_rate", "reply_to_lead", "lead_to_sql", "sql_to_win", "forecast_accuracy", "crm_adoption"]


def _member(client, tenant_id, label, role="AGENT", manager_id=None, sales_level=None, status="ACTIVE"):
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{label}-{RUN}-{uuid.uuid4().hex[:4]}@ck{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Kpi", email=email,
                    password_hash=hash_password(password), role=role, status=status, auth_provider="local",
                    email_verified=True, manager_id=manager_id, sales_level=sales_level)
        db.add(user)
        db.commit()
        user_id = user.user_id
    headers = auth(login(client, email, password)) if status == "ACTIVE" else None
    return {"user_id": user_id, "email": email, "headers": headers}


def _kpis(client, headers, **params):
    r = client.get("/sales-reports/kpis", headers=headers, params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    return body, {k["key"]: k for k in body["kpis"]}


def _contact(client, h, local, **kw):
    r = client.post("/contacts", headers=h, json={"email": f"{local}.{RUN}@ck{RUN}.example.com", **kw})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _positive_reply(prospect_id, when: datetime):
    """An outbound email to the contact and a POSITIVE_REPLY event on it (what IMAP sync writes)."""
    mid = str(uuid.uuid4())
    sql("INSERT INTO email_messages (message_id, prospect_id, tenant_id, direction, status, to_email, subject, "
        "sent_at) SELECT :m, prospect_id, tenant_id, 'OUTBOUND', 'SENT', 'x@example.com', 'Hi', :t FROM prospects "
        "WHERE prospect_id = :p", m=mid, p=prospect_id, t=when - timedelta(days=1))
    for event_type in (4, 8):
        sql("INSERT INTO email_events (event_id, message_id, tenant_id, event_type, event_time) "
            "SELECT :e, message_id, tenant_id, :k, :t FROM email_messages WHERE message_id = :m",
            e=str(uuid.uuid4()), m=mid, k=event_type, t=when)
    return mid


@pytest.fixture(scope="module")
def ws(client):
    """Owner (SUPER_ADMIN), a Sales Head (level 1), a manager (level 2) with rep1, rep2 on their own,
    and an inactive user (not a licensed seat)."""
    t = register_tenant(client, "ck")
    head = _member(client, t["tenant_id"], "head", role="MANAGER", sales_level=1)
    manager = _member(client, t["tenant_id"], "mgr", role="MANAGER", sales_level=2, manager_id=head["user_id"])
    rep1 = _member(client, t["tenant_id"], "rep1", manager_id=manager["user_id"], sales_level=3)
    rep2 = _member(client, t["tenant_id"], "rep2", sales_level=3)
    gone = _member(client, t["tenant_id"], "gone", status="INACTIVE")
    stages = {s["name"]: s for s in client.get("/sales/stages", headers=t["headers"]).json()}
    owner_id = sql("SELECT user_id FROM users WHERE email = :e", e=t["email"])
    return {"t": t, "h": t["headers"], "owner_id": owner_id, "head": head, "manager": manager, "rep1": rep1, "rep2": rep2, "gone": gone,
            "stages": stages}


# ── Shape and access ─────────────────────────────────────────

def test_kpis_shape_targets_and_definitions(client, ws):
    body, k = _kpis(client, ws["h"])
    assert [x["key"] for x in body["kpis"]] == KEYS
    assert body["workspace_view"] is True and body["amounts_hidden"] is False
    assert body["period"]["to"] == date.today().isoformat()
    assert body["period"]["from"] == (date.today() - timedelta(days=89)).isoformat()
    for x in body["kpis"]:
        assert {"value", "numerator", "denominator", "target", "period", "note", "definition", "formula",
                "label", "available", "scope"} <= set(x)
    assert k["duplicate_contact_rate"]["target"]["operator"] == "<" and k["duplicate_contact_rate"]["target"]["value"] == 1.0
    assert all(k[key]["target"] is None for key in KEYS if key != "duplicate_contact_rate")
    assert k["forecast_accuracy"]["formula"] == "actual revenue ÷ committed forecast × 100, per quarter"


def test_workspace_kpis_only_for_admins_and_sales_head(client, ws):
    for who in ("rep1", "manager"):
        _, k = _kpis(client, ws[who]["headers"])
        for key in ("duplicate_contact_rate", "crm_adoption"):
            assert k[key]["available"] is False and k[key]["value"] is None and k[key]["numerator"] is None
            assert "Workspace-level" in k[key]["note"]
        assert k["lead_to_sql"]["available"] is True
    body, k = _kpis(client, ws["head"]["headers"])
    assert body["workspace_view"] is True and k["crm_adoption"]["available"] and k["duplicate_contact_rate"]["available"]


def test_bad_filters(client, ws):
    h = ws["h"]
    assert client.get("/sales-reports/kpis", headers=h, params={"quarter": 2}).status_code == 400
    assert client.get("/sales-reports/kpis", headers=h, params={"fy": 1900}).status_code == 422
    assert client.get("/sales-reports/kpis", headers=h, params={"quarter": 5, "fy": 2026}).status_code == 422
    assert client.get("/sales-reports/kpis", headers=h, params={"date_from": "2026-05-02", "date_to": "2026-05-01"}).status_code == 400
    assert client.get("/sales-reports/kpis", headers=ws["rep1"]["headers"], params={"member": ws["rep2"]["user_id"]}).status_code == 404
    assert client.get("/sales-reports/kpis").status_code == 401


# ── Duplicate contact rate ───────────────────────────────────

def test_duplicate_contact_rate(client, ws):
    t = register_tenant(client, "ckd")
    h = t["headers"]
    rep = _member(client, t["tenant_id"], "drep", sales_level=3)
    ids = {
        # similar name at the same company (Rob / Robert Smith at Acme)
        "a": _contact(client, h, "robert", first_name="Robert", last_name="Smith", company_name="Acme Dup",
                      owner_id=rep["user_id"]),
        "b": _contact(client, h, "rsmith", first_name="Rob", last_name="Smith", company_name="Acme Dup"),
        # unique
        "g": _contact(client, h, "grace", first_name="Grace", last_name="Hopper", company_name="Navy"),
        "h": _contact(client, h, "alan", first_name="Alan", last_name="Turing", owner_id=rep["user_id"]),
        "i": _contact(client, h, "ada", first_name="Ada", last_name="Lovelace", company_name="Engines"),
        # the same phone number written differently
        "e": _contact(client, h, "phone1", first_name="Pat", phone="+1 (555) 010-2030"),
        "f": _contact(client, h, "phone2", first_name="Sam", mobile_phone="555.010.2030"),
    }
    # the same Gmail address apart from dots and a +tag
    for key, email in (("c", f"john.doe.{RUN}@gmail.com"), ("d", f"johndoe.{RUN}+news@gmail.com")):
        r = client.post("/contacts", headers=h, json={"email": email, "first_name": key.upper()})
        assert r.status_code == 201, r.text
        ids[key] = r.json()["prospect_id"]
    # a deleted contact is not counted, even though it duplicates Grace
    j = _contact(client, h, "grace2", first_name="Grace", last_name="Hopper", company_name="Navy")
    assert client.delete(f"/contacts/{j}", headers=h).status_code == 200

    _, k = _kpis(client, h)
    d = k["duplicate_contact_rate"]
    assert (d["numerator"], d["denominator"]) == (6, 9)
    assert d["value"] == pytest.approx(66.7) and d["meets_target"] is False and d["scope"] == "workspace"
    # Matches the duplicate groups the Duplicates page shows
    groups = client.get("/contacts/duplicates", headers=h, params={"limit": 200}).json()["groups"]
    in_groups = {c["prospect_id"] for g in groups for c in g["contacts"]}
    assert in_groups == {ids[x] for x in "abcdef"}
    # member= narrows to that person's contacts: Robert (whose duplicate belongs to someone else) and Alan
    _, k = _kpis(client, h, member=rep["user_id"])
    assert (k["duplicate_contact_rate"]["numerator"], k["duplicate_contact_rate"]["denominator"]) == (0, 2)
    assert k["duplicate_contact_rate"]["meets_target"] is True
    # Merging a group resolves it
    r = client.post("/contacts/merge", headers=h, json={"primary_id": ids["c"], "duplicate_ids": [ids["d"]]})
    assert r.status_code == 200, r.text
    _, k = _kpis(client, h)
    assert (k["duplicate_contact_rate"]["numerator"], k["duplicate_contact_rate"]["denominator"]) == (4, 8)
    assert k["duplicate_contact_rate"]["value"] == 50.0
    # A workspace without contacts
    other = register_tenant(client, "ckd2")
    _, k = _kpis(client, other["headers"])
    assert k["duplicate_contact_rate"]["value"] is None and k["duplicate_contact_rate"]["denominator"] == 0


# ── Reply-to-lead conversion ─────────────────────────────────

def test_reply_to_lead_conversion(client, ws):
    h = ws["h"]
    rep1, rep2 = ws["rep1"]["user_id"], ws["rep2"]["user_id"]
    recent = utcnow() - timedelta(days=5)
    p1 = _contact(client, h, "rl1", first_name="One", owner_id=rep1)
    p2 = _contact(client, h, "rl2", first_name="Two", owner_id=rep2)
    p3 = _contact(client, h, "rl3", first_name="Three", owner_id=rep2)
    p4 = _contact(client, h, "rl4", first_name="Four", owner_id=rep2)
    p5 = _contact(client, h, "rl5", first_name="Five", owner_id=rep2)
    p6 = _contact(client, h, "rl6", first_name="Six", owner_id=rep2)
    p7 = _contact(client, h, "rl7", first_name="Seven", owner_id=rep2)
    m1 = _positive_reply(p1, recent)
    _positive_reply(p1, recent + timedelta(hours=1))          # a second positive reply: still one contact
    _positive_reply(p2, recent)
    _positive_reply(p3, recent)
    _positive_reply(p4, recent)
    _positive_reply(p5, utcnow() - timedelta(days=200))   # outside the period
    # The reply's inbound message, so the one-click flow has something to turn into a lead
    inbound = str(uuid.uuid4())
    sql("INSERT INTO email_messages (message_id, prospect_id, tenant_id, direction, status, to_email, subject, "
        "body_text, sent_at) SELECT :m, prospect_id, tenant_id, 'INBOUND', 'RECEIVED', 'me@example.com', 'Re: Hi', "
        "'Yes, lets talk', :t FROM prospects WHERE prospect_id = :p", m=inbound, p=p1, t=recent)
    r = client.post("/leads/from-message", headers=h, json={"message_id": inbound})
    assert r.status_code == 201, r.text
    assert r.json()["source"] == "Campaign reply"
    # A lead created by hand after a positive reply counts too
    assert client.post("/leads", headers=h, json={"prospect_id": p2, "source": "Referral"}).status_code == 201
    # A lead with the reply source but no tracked positive reply counts (numerator only)
    assert client.post("/leads", headers=h, json={"prospect_id": p6, "source": "Campaign reply"}).status_code == 201
    # Other leads do not
    assert client.post("/leads", headers=h, json={"prospect_id": p7, "source": "Website"}).status_code == 201
    assert m1

    _, k = _kpis(client, h)
    rl = k["reply_to_lead"]
    assert (rl["numerator"], rl["denominator"], rl["value"]) == (3, 4, 75.0) and rl["note"] is None
    # Scoped by owner: rep2 (p2, p6 leads; p2, p3, p4 replies); the manager's team is rep1
    _, k = _kpis(client, ws["rep2"]["headers"])
    assert (k["reply_to_lead"]["numerator"], k["reply_to_lead"]["denominator"]) == (2, 3)
    _, k = _kpis(client, ws["manager"]["headers"])
    assert (k["reply_to_lead"]["numerator"], k["reply_to_lead"]["denominator"], k["reply_to_lead"]["value"]) == (1, 1, 100.0)
    # A longer range includes the old reply
    _, k = _kpis(client, h, date_from=(date.today() - timedelta(days=365)).isoformat())
    assert (k["reply_to_lead"]["numerator"], k["reply_to_lead"]["denominator"]) == (3, 5)


def test_reply_to_lead_without_positive_replies(client):
    t = register_tenant(client, "ckz")
    _, k = _kpis(client, t["headers"])
    rl = k["reply_to_lead"]
    assert rl["value"] is None and rl["denominator"] == 0
    # The test environment has no OpenAI key, so the note says why nothing is classified
    assert rl["classifier_available"] is False and "not configured" in rl["note"]


# ── Lead-to-SQL and SQL-to-win match their reports ───────────

def test_lead_to_sql_and_sql_to_win_match_reports(client, ws):
    h = ws["h"]
    rep1 = ws["rep1"]["user_id"]
    for i, (stage, won) in enumerate([("SQL", True), ("SQL", False), (None, False)]):
        p = _contact(client, h, f"ls{i}", first_name=f"Ls{i}", owner_id=rep1)
        lead = client.post("/leads", headers=h, json={"prospect_id": p, "source": "Test"}).json()["lead_id"]
        if stage:
            sql("UPDATE leads SET stage = 'SQL', qualified_at = NOW() - INTERVAL 2 DAY WHERE lead_id = :l", l=lead)
        if won:
            r = client.post("/opportunities", headers=h, json={
                "name": f"Won {i} {RUN}", "prospect_id": p, "owner_id": rep1, "amount": "100",
                "stage_id": ws["stages"]["Closed won"]["stage_id"], "close_date": date.today().isoformat(),
                "closed_reason": "Price"})
            assert r.status_code == 201, r.text
            sql("UPDATE opportunities SET lead_id = :l WHERE opportunity_id = :o", l=lead, o=r.json()["opportunity_id"])
    for headers, params in ((h, {}), (ws["manager"]["headers"], {}), (h, {"member": ws["rep2"]["user_id"]})):
        _, k = _kpis(client, headers, **params)
        leads = client.get("/sales-reports/leads", headers=headers, params=params).json()
        s2w = client.get("/sales-reports/sql-to-win", headers=headers, params=params).json()
        assert (k["lead_to_sql"]["numerator"], k["lead_to_sql"]["denominator"]) == (leads["sqls"], leads["total"])
        assert k["lead_to_sql"]["value"] == leads["lead_to_sql_rate"]
        assert (k["sql_to_win"]["numerator"], k["sql_to_win"]["denominator"]) == (s2w["won"], s2w["sqls"])
        assert k["sql_to_win"]["value"] == s2w["sql_to_win_rate"]
    _, k = _kpis(client, ws["manager"]["headers"])
    assert (k["lead_to_sql"]["numerator"], k["sql_to_win"]["numerator"], k["sql_to_win"]["denominator"]) == (2, 1, 2)
    assert k["sql_to_win"]["value"] == 50.0


# ── Forecast accuracy ────────────────────────────────────────

def _snapshot(tenant_id, start, owner, committed, commit_open=0, won=0, weighted=0):
    from app.models.sales_extra import ForecastSnapshot
    from app.services import forecast_snapshots as fs
    with SessionLocal() as db:
        db.add(ForecastSnapshot(tenant_id=tenant_id, period_type="QUARTER", period_start=start,
                                period_end=fs.add_months(start, 3), owner_id=owner, won=won, weighted=weighted,
                                forecast=won + weighted, commit_open=commit_open, committed=committed,
                                source="SCHEDULED", taken_at=datetime.combine(start, datetime.min.time())))
        db.commit()


def _won(client, ws, owner, amount, close: date):
    r = client.post("/opportunities", headers=ws["h"], json={
        "name": f"W {uuid.uuid4().hex[:6]}", "owner_id": owner, "amount": str(amount),
        "stage_id": ws["stages"]["Closed won"]["stage_id"], "close_date": close.isoformat(), "closed_reason": "Price"})
    assert r.status_code == 201, r.text


def test_forecast_accuracy_last_finished_quarter(client, ws):
    from app.services import crm_kpis as kp
    from app.services import sales as svc

    t = ws["t"]
    rep1, rep2 = ws["rep1"]["user_id"], ws["rep2"]["user_id"]
    q = kp.forecast_quarters(None, None, date.today())[0]
    _snapshot(t["tenant_id"], q, rep1, committed=1000, commit_open=600, won=400)
    _snapshot(t["tenant_id"], q, rep2, committed=3000, commit_open=3000)
    _won(client, ws, rep1, 1200, q + timedelta(days=10))
    _won(client, ws, rep2, 1800, q + timedelta(days=20))
    _won(client, ws, rep2, 999, q - timedelta(days=1))   # the quarter before: not counted

    _, k = _kpis(client, ws["h"])
    fa = k["forecast_accuracy"]
    assert (fa["numerator"], fa["denominator"], fa["value"]) == (3000.0, 4000.0, 75.0)
    assert fa["in_progress"] is False and fa["note"] is None and fa["period"]["from"] == q.isoformat()
    assert fa["quarters"][0]["committed"] == 4000.0 and fa["quarters"][0]["closed"] is True
    # Can exceed 100%: rep1 won 1200 against 1000 committed; the manager's team is rep1
    _, k = _kpis(client, ws["h"], member=rep1)
    assert k["forecast_accuracy"]["value"] == 120.0
    _, k = _kpis(client, ws["manager"]["headers"])
    assert k["forecast_accuracy"]["value"] == 120.0
    # The same quarter by fiscal year and quarter, and the whole year (its finished quarters)
    fy, qn = svc.fiscal_year_of(q), svc.fiscal_quarter_of(q)
    _, k = _kpis(client, ws["h"], fy=fy, quarter=qn)
    assert k["forecast_accuracy"]["value"] == 75.0 and k["forecast_accuracy"]["period"]["label"].startswith(f"Q{qn}")
    _, k = _kpis(client, ws["h"], fy=fy)
    assert len(k["forecast_accuracy"]["quarters"]) == 4
    # The forecast-accuracy report uses the same BRD formula
    acc = client.get("/sales-reports/forecast-accuracy", headers=ws["h"]).json()
    row = next(p for p in acc["periods"] if p["period_start"] == q.isoformat())
    assert (row["committed"], row["actual"], row["accuracy"]) == (4000.0, 3000.0, 75.0)


def test_forecast_accuracy_notes(client, ws):
    from app.services import crm_kpis as kp
    from app.services import sales as svc

    t = ws["t"]
    last = kp.forecast_quarters(None, None, date.today())[0]
    legacy, missing = kp.add_months(last, -3), kp.add_months(last, -6)
    # A snapshot from before committed forecasts were stored
    _snapshot(t["tenant_id"], legacy, ws["rep1"]["user_id"], committed=None, commit_open=None, won=10, weighted=20)
    _, k = _kpis(client, ws["h"], fy=svc.fiscal_year_of(legacy), quarter=svc.fiscal_quarter_of(legacy))
    assert k["forecast_accuracy"]["value"] is None and "before committed" in k["forecast_accuracy"]["note"]
    _, k = _kpis(client, ws["h"], fy=svc.fiscal_year_of(missing), quarter=svc.fiscal_quarter_of(missing))
    assert k["forecast_accuracy"]["value"] is None and "No forecast was stored" in k["forecast_accuracy"]["note"]
    # member= takes that team's share of the snapshot: the head's team (manager, rep1) committed 1000
    _, k = _kpis(client, ws["h"], member=ws["head"]["user_id"], fy=svc.fiscal_year_of(last),
                 quarter=svc.fiscal_quarter_of(last))
    assert k["forecast_accuracy"]["denominator"] == 1000.0
    # A scope with no share of the snapshot committed nothing
    _, k = _kpis(client, ws["h"], member=ws["owner_id"],
                 fy=svc.fiscal_year_of(last), quarter=svc.fiscal_quarter_of(last))
    assert k["forecast_accuracy"]["value"] is None and k["forecast_accuracy"]["note"].startswith("Nothing was committed")
    other = register_tenant(client, "ckf")
    _, k = _kpis(client, other["headers"])
    assert k["forecast_accuracy"]["value"] is None and "No forecast was stored" in k["forecast_accuracy"]["note"]


def test_snapshot_stores_committed_and_current_quarter_in_progress(client, ws):
    from app.services import forecast_snapshots as fs
    from app.services import sales as svc

    h = ws["h"]
    rep2 = ws["rep2"]["user_id"]
    today = date.today()
    q_start, q_end = fs.period_bounds("QUARTER", today)
    close = today.isoformat()

    def opp(stage, amount, **kw):
        r = client.post("/opportunities", headers=h, json={
            "name": f"O {uuid.uuid4().hex[:6]}", "owner_id": rep2, "amount": str(amount),
            "stage_id": ws["stages"][stage]["stage_id"], "close_date": close, **kw})
        assert r.status_code == 201, r.text
        return r.json()

    opp("Negotiation", 5000)                                   # 75 %: Commit by default
    opp("Proposal", 7000)                                      # 50 %: Best case
    opp("Proposal", 2000, forecast_category="COMMIT")          # Commit set by hand
    opp("Negotiation", 900, forecast_category="OMITTED")       # taken out of the forecast by hand
    won_before = float(sql("SELECT COALESCE(SUM(amount), 0) FROM opportunities WHERE tenant_id = :t AND status = 'WON' "
                           "AND close_date >= :s AND close_date < :e AND owner_id = :o",
                           t=ws["t"]["tenant_id"], s=q_start, e=q_end, o=rep2))
    r = client.post("/sales-reports/forecast-snapshots", headers=h, json={"period_type": "QUARTER", "replace": True})
    assert r.status_code == 201, r.text
    row = sql("SELECT CONCAT(commit_open, '|', committed) FROM forecast_snapshots WHERE tenant_id = :t "
              "AND period_type = 'QUARTER' AND period_start = :s AND owner_id = :o", t=ws["t"]["tenant_id"], s=q_start, o=rep2)
    commit_open, committed = (float(x) for x in row.split("|"))
    assert commit_open == 7000.0 and committed == pytest.approx(won_before + 7000.0)

    _, k = _kpis(client, h, member=rep2, fy=svc.fiscal_year_of(today), quarter=svc.fiscal_quarter_of(today))
    fa = k["forecast_accuracy"]
    assert fa["in_progress"] is True and "in progress" in fa["note"]
    assert fa["denominator"] == pytest.approx(committed)
    assert fa["value"] == pytest.approx(round(100 * won_before / committed, 1))


def test_forecast_accuracy_hides_amounts(client, ws):
    h = ws["h"]
    assert client.put("/sales/settings", headers=h, json={"amount_hidden_roles": ["AGENT"]}).status_code == 200
    try:
        body, k = _kpis(client, ws["rep1"]["headers"])
        fa = k["forecast_accuracy"]
        assert body["amounts_hidden"] is True and fa["amounts_hidden"] is True
        assert fa["numerator"] is None and fa["denominator"] is None and fa["value"] == 120.0
        assert fa["quarters"][0]["committed"] is None and fa["quarters"][0]["actual"] is None
        # Counts are not money: still shown
        assert k["lead_to_sql"]["denominator"] is not None
    finally:
        client.put("/sales/settings", headers=h, json={"amount_hidden_roles": []})


# ── CRM adoption and activity recording ──────────────────────

def test_crm_adoption_weeks(client, ws):
    from app.services import crm_kpis as kp

    t = ws["t"]
    tid = t["tenant_id"]
    weeks = kp.week_windows(date.today())
    prev, two_ago = weeks[-2][0], weeks[-3][0]
    for user, day in ((ws["rep1"], prev), (ws["rep2"], prev + timedelta(days=6)), (ws["rep2"], prev),
                      (ws["gone"], prev), (ws["manager"], two_ago - timedelta(days=1))):
        sql("INSERT IGNORE INTO user_activity_days (tenant_id, user_id, day) VALUES (:t, :u, :d)",
            t=tid, u=user["user_id"], d=day)
    _, k = _kpis(client, ws["h"])
    ad = k["crm_adoption"]
    # Licensed: owner, head, manager, rep1, rep2 (the inactive user is not a seat). Everyone signed in this week.
    assert ad["denominator"] == 5 and ad["numerator"] == 5 and ad["value"] == 100.0
    assert len(ad["trend"]) == 9 and ad["trend"][-1]["partial"] is True and ad["period"]["label"] == "This week"
    by_week = {w["week_start"]: w for w in ad["trend"]}
    assert (by_week[prev.isoformat()]["active_users"], by_week[prev.isoformat()]["value"]) == (2, 40.0)
    # The manager's Sunday belongs to the week before two_ago
    assert by_week[two_ago.isoformat()]["active_users"] == 0
    assert by_week[(two_ago - timedelta(days=7)).isoformat()]["active_users"] == 1
    assert ad["last_full_week"]["week_start"] == prev.isoformat()
    # member= narrows seats to that team (manager + rep1)
    _, k = _kpis(client, ws["h"], member=ws["manager"]["user_id"])
    assert (k["crm_adoption"]["denominator"], k["crm_adoption"]["trend"][-2]["active_users"]) == (2, 1)
    # A period that ended earlier anchors on its last week
    _, k = _kpis(client, ws["h"], date_from=(prev - timedelta(days=30)).isoformat(), date_to=(prev + timedelta(days=2)).isoformat())
    assert k["crm_adoption"]["period"]["from"] == prev.isoformat() and k["crm_adoption"]["numerator"] == 2
    assert k["crm_adoption"]["trend"][-1]["partial"] is False and k["crm_adoption"]["note"] is None


def test_activity_recorded_once_per_day_from_auth(client, ws):
    from app.services import user_activity as ua

    uid = ws["rep2"]["user_id"]
    count = "SELECT COUNT(*) FROM user_activity_days WHERE user_id = :u AND day = UTC_DATE()"
    assert sql(count, u=uid) == 1          # signed in (and used the API) today
    sql("DELETE FROM user_activity_days WHERE user_id = :u AND day = UTC_DATE()", u=uid)
    assert client.get("/sales-reports/kpis", headers=ws["rep2"]["headers"]).status_code == 200
    assert sql(count, u=uid) == 0          # already written by this process today: no second write
    ua.forget(uid)
    assert client.get("/api/auth/me", headers=ws["rep2"]["headers"]).status_code == 200
    assert sql(count, u=uid) == 1          # a fresh process (empty cache) writes it again
    tenant = sql("SELECT tenant_id FROM user_activity_days WHERE user_id = :u AND day = UTC_DATE()", u=uid)
    assert tenant == ws["t"]["tenant_id"]
    # Signing in writes it too
    ua.forget(uid)
    sql("DELETE FROM user_activity_days WHERE user_id = :u", u=uid)
    r = client.post("/api/auth/login", json={"email": ws["rep2"]["email"], "password": f"rep2-pass-{RUN}"})
    assert r.status_code == 200 and sql(count, u=uid) == 1


def test_other_workspace_sees_nothing(client, ws):
    other = register_tenant(client, "cko")
    _, k = _kpis(client, other["headers"])
    assert k["crm_adoption"]["denominator"] == 1 and k["crm_adoption"]["numerator"] == 1
    assert k["lead_to_sql"]["denominator"] == 0 and k["reply_to_lead"]["denominator"] == 0
    assert client.get("/sales-reports/kpis", headers=other["headers"],
                      params={"member": ws["rep1"]["user_id"]}).status_code == 404
