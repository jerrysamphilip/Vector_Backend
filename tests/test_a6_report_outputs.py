"""Exact-output regression for the reports, custom reports, sales reports and CRM KPI endpoints
(refactor chunk A6, docs/REFACTOR_PLAN.md).

A fixed dataset (campaigns, mailboxes, contacts, emails and events, leads, deals, targets, activities,
tasks) is seeded into a fresh workspace with fixed dates in the past; every report is then requested
by four viewers (owner, manager, agent in the manager's team, agent outside any team) and compared
with tests/test_a6_report_outputs.golden.json, recorded from the code before the layered refactor.
Generated ids, the run suffix and today's date are replaced by stable placeholders first.

Regenerate the golden file only for an intended change:  A6_WRITE_GOLDEN=1 pytest tests/test_a6_report_outputs.py
"""

import io
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql

GOLDEN = Path(__file__).with_name("test_a6_report_outputs.golden.json")
D = datetime(2025, 5, 1, 9, 0, 0)          # dataset anchor: all activity in May-Nov 2025
FY = 2025                                   # fiscal year (April start): Apr 2025 - Mar 2026

# Fields measured against the clock (ages) or stamped by the database at insert time
_VOLATILE = {"updated_at", "stage_changed_at", "age_days", "days_idle", "days_in_stage", "sql_age_days"}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class _Ids:
    """Maps generated ids to readable labels for the normalised output."""

    def __init__(self):
        self.labels = {}

    def new(self, label):
        value = str(uuid.uuid4())
        self.labels[value] = label
        return value


def _member(tenant_id, label, role, ids, manager_id=None, sales_level=None):
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{label}-{RUN}@a6{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Six", email=email,
                    password_hash=hash_password(password), role=role, status="ACTIVE", auth_provider="local",
                    email_verified=True, manager_id=manager_id, sales_level=sales_level)
        db.add(user)
        db.commit()
        uid = user.user_id
    ids.labels[uid] = f"<user:{label}>"
    return uid, email, password


def _insert(table, **cols):
    names = ", ".join(cols)
    sql(f"INSERT INTO {table} ({names}) VALUES ({', '.join(':' + c for c in cols)})", **cols)


def _seed(client):
    ids = _Ids()
    t = register_tenant(client, "asix")
    tenant = t["tenant_id"]
    ids.labels[tenant] = "<tenant>"
    owner = sql("SELECT user_id FROM users WHERE email = :e", e=t["email"])
    ids.labels[owner] = "<user:owner>"
    mgr, mgr_email, mgr_pw = _member(tenant, "mgr", "MANAGER", ids, sales_level=2)
    rep, rep_email, rep_pw = _member(tenant, "rep", "AGENT", ids, manager_id=mgr, sales_level=3)
    solo, solo_email, solo_pw = _member(tenant, "solo", "AGENT", ids)
    viewers = {"owner": t["headers"], "mgr": auth(login(client, mgr_email, mgr_pw)),
               "rep": auth(login(client, rep_email, rep_pw)), "solo": auth(login(client, solo_email, solo_pw))}
    stages = {s["name"]: s for s in client.get("/sales/stages", headers=t["headers"]).json()}
    for s in stages.values():
        ids.labels[s["stage_id"]] = f"<stage:{s['name']}>"
    c = {"ids": ids, "tenant": tenant, "owner": owner, "mgr": mgr, "rep": rep, "solo": solo, "stages": stages}
    for step in (_seed_contacts, _seed_campaigns, _seed_mail, _seed_leads, _seed_deals, _seed_work):
        step(c)
    return {"ids": ids, "viewers": viewers, "users": {"owner": owner, "mgr": mgr, "rep": rep, "solo": solo},
            "camps": c["camps"]}


def _seed_contacts(c):
    ids, tenant, owner, mgr, rep, solo = (c[k] for k in ("ids", "tenant", "owner", "mgr", "rep", "solo"))
    # Mailboxes
    inboxes = []
    for i, (provider, status, warmup) in enumerate([("gmail", "ACTIVE", 1), ("outlook", "PAUSED", 0),
                                                    (None, "ACTIVE", 0)]):
        iid = ids.new(f"<inbox:{i}>")
        _insert("sending_inboxes", inbox_id=iid, tenant_id=tenant, email_address=f"box{i}@a6mail.example.com",
                provider=provider, status=status, warmup_enabled=warmup)
        inboxes.append(iid)

    # Accounts are referenced by id only (labels via the companies table are not needed here)
    # Contacts: owners spread over the team; two are duplicates (gmail dots / +tag), one shares a phone
    people = [("ann", owner, "Acme", "Pune", "IT"), ("bob", mgr, "Acme", "Delhi", "IT"),
              ("cat", rep, "Beta", "Delhi", "Retail"), ("dan", rep, "Beta", None, "Retail"),
              ("eve", solo, "Gamma", "Pune", None), ("fay", solo, "Gamma", "Pune", None),
              ("gus", None, "Delta", "Mumbai", "IT"), ("hal", rep, "Delta", "Mumbai", "IT")]
    prospects = {}
    for i, (name, own, company, country, industry) in enumerate(people):
        pid = ids.new(f"<contact:{name}>")
        email = f"{name}.{RUN}@a6c{RUN}.example.com"
        if name == "dan":
            email = f"j.doe{RUN}@gmail.com"
        if name == "hal":
            email = f"jdoe{RUN}+x@gmail.com"
        _insert("prospects", prospect_id=pid, tenant_id=tenant, email=email, first_name=name.capitalize(),
                last_name="Person", company_name=company, designation="VP" if i % 2 else "CTO", owner_id=own,
                poc_country=country, industry=industry, lead_source="Web" if i % 3 else "Event",
                lifecycle_stage="LEAD" if i % 2 else "SUBSCRIBER", lead_status="NEW" if i < 4 else "OPEN",
                consent_status="SUBSCRIBED", phone="+91 98765 4321%d" % (i % 3 if name in ("eve", "fay") else i),
                created_at=D + timedelta(days=i), updated_at=D + timedelta(days=i))
        prospects[name] = pid
    c["inboxes"], c["prospects"] = inboxes, prospects


def _seed_campaigns(c):
    ids, tenant, owner, mgr, rep, solo = (c[k] for k in ("ids", "tenant", "owner", "mgr", "rep", "solo"))
    prospects = c["prospects"]
    # Campaigns (created_by spreads them over the viewers)
    camps = {}
    for name, creator, status, day in [("Alpha", owner, "ACTIVE", 0), ("Bravo", mgr, "PAUSED", 1),
                                       ("Charlie", rep, "ACTIVE", 2), ("Delta", solo, "COMPLETED", 3),
                                       ("Echo", rep, "DRAFT", 4)]:
        cid = ids.new(f"<campaign:{name}>")
        _insert("campaigns", campaign_id=cid, tenant_id=tenant, campaign_name=f"{name} campaign", created_by=creator,
                status=status, created_at=D + timedelta(days=day), updated_at=D + timedelta(days=day))
        camps[name] = cid

    enrol = [("Alpha", "ann", "REPLIED", 2), ("Alpha", "bob", "OPENED", 1), ("Alpha", "cat", "ACTIVE", 1),
             ("Bravo", "bob", "COMPLETED", 3), ("Bravo", "dan", "BOUNCED", 1), ("Charlie", "cat", "REPLIED", 2),
             ("Charlie", "dan", "OPENED", 2), ("Charlie", "hal", "ACTIVE", 1), ("Delta", "eve", "REPLIED", 1),
             ("Delta", "fay", "ACTIVE", 1), ("Echo", "gus", "ACTIVE", 1)]
    for cname, pname, status, step in enrol:
        _insert("campaign_prospects", id=ids.new("<cp>"), campaign_id=camps[cname], prospect_id=prospects[pname],
                status=status, current_step=step, enrolled_at=D)
    c["camps"] = camps


def _seed_mail(c):
    ids, tenant, owner, mgr, rep, solo = (c[k] for k in ("ids", "tenant", "owner", "mgr", "rep", "solo"))
    prospects, camps, inboxes = c["prospects"], c["camps"], c["inboxes"]
    # Emails: (campaign, contact, inbox, day offset, hour, status, direction, events [(type, hours after)])
    mails = [
        ("Alpha", "ann", 0, 0, 9, "SENT", "OUTBOUND", [(2, 1), (2, 5), (3, 6), (4, 30)]),
        ("Alpha", "ann", 0, 3, 9, "SENT", "OUTBOUND", [(2, 2)]),
        ("Alpha", "bob", 0, 0, 10, "SENT", "OUTBOUND", [(2, 3)]),
        ("Alpha", "cat", 1, 1, 9, "SENT", "OUTBOUND", []),
        ("Alpha", "cat", 1, 9, 9, "SENT", "OUTBOUND", [(2, 4), (8, 5)]),
        ("Bravo", "bob", 1, 2, 11, "SENT", "OUTBOUND", [(2, 1), (4, 10)]),
        ("Bravo", "dan", 1, 2, 12, "BOUNCED", "OUTBOUND", [(5, 0)]),
        ("Bravo", "dan", 2, 4, 12, "FAILED", "OUTBOUND", []),
        ("Charlie", "cat", 2, 5, 9, "SENT", "OUTBOUND", [(2, 2), (4, 48), (8, 48)]),
        ("Charlie", "dan", 2, 5, 10, "SENT", "OUTBOUND", [(2, 3), (3, 4)]),
        ("Charlie", "hal", 2, 12, 10, "SENT", "OUTBOUND", []),
        ("Charlie", "cat", 2, 7, 9, "SENT", "INBOUND", []),
        ("Delta", "eve", 0, 20, 9, "SENT", "OUTBOUND", [(2, 1), (4, 2), (9, 3)]),
        ("Delta", "fay", 0, 20, 10, "COMPLAINED", "OUTBOUND", [(6, 5)]),
        ("Delta", "fay", 0, 40, 10, "SENT", "OUTBOUND", [(3, 1)]),        # outside May
        ("Echo", "gus", 1, 25, 9, "QUEUED", "OUTBOUND", []),
    ]
    for cname, pname, inbox, day, hour, status, direction, events in mails:
        mid = ids.new("<msg>")
        sent_at = D.replace(hour=hour) + timedelta(days=day)
        _insert("email_messages", message_id=mid, prospect_id=prospects[pname], campaign_id=camps[cname],
                inbox_id=inboxes[inbox], direction=direction, status=status, to_email="x@example.com", subject="Hi",
                sent_at=None if status == "QUEUED" else sent_at)
        for etype, after in events:
            _insert("email_events", event_id=ids.new("<event>"), message_id=mid, event_type=etype,
                    event_time=sent_at + timedelta(hours=after))


def _seed_leads(c):
    ids, tenant, owner, mgr, rep, solo = (c[k] for k in ("ids", "tenant", "owner", "mgr", "rep", "solo"))
    prospects, camps = c["prospects"], c["camps"]
    # Leads (owners across the team), some qualified, converted or disqualified
    leads = {}
    lead_rows = [("ann", owner, "SQL", "Campaign reply", "Alpha", 0, 3, None, None),
                 ("bob", mgr, "CONVERTED", "Web", "Bravo", 2, 6, 10, None),
                 ("cat", rep, "SQL", "Campaign reply", "Charlie", 8, 9, None, None),
                 ("dan", rep, "DISQUALIFIED", None, "Charlie", 9, None, None, "No budget"),
                 ("eve", solo, "CONTACTED", "Event", "Delta", 22, None, None, None),
                 ("hal", rep, "CONVERTED", "Web", None, 40, 50, 60, None),
                 ("gus", owner, "NEW", None, None, 100, None, None, None)]
    for pname, own, stage, source, cname, created, qualified, converted, reason in lead_rows:
        lid = ids.new(f"<lead:{pname}>")
        _insert("leads", lead_id=lid, tenant_id=tenant, prospect_id=prospects[pname], owner_id=own, stage=stage,
                source=source, campaign_id=camps.get(cname), created_at=D + timedelta(days=created),
                updated_at=D + timedelta(days=created),
                qualified_at=D + timedelta(days=qualified) if qualified is not None else None,
                converted_at=D + timedelta(days=converted) if converted is not None else None,
                disqualified_reason=reason, recycle_at=D + timedelta(days=200) if reason else None,
                next_step_at=D + timedelta(days=created + 5))
        leads[pname] = lid
    c["leads"] = leads


def _seed_deals(c):
    ids, tenant, owner, mgr, rep, solo = (c[k] for k in ("ids", "tenant", "owner", "mgr", "rep", "solo"))
    camps, leads, stages = c["camps"], c["leads"], c["stages"]
    open_stages = [s for s in stages.values() if not s.get("is_won") and not s.get("is_lost")]
    won_stage = next(s for s in stages.values() if s.get("is_won"))
    lost_stage = next(s for s in stages.values() if s.get("is_lost"))
    # Deals
    deal_rows = [("Bob deal", mgr, won_stage, "WON", "NEW", "COMMIT", 50000, 30, 28, "Alpha", "bob", "Price"),
                 ("Hal deal", rep, won_stage, "WON", "EXISTING", "COMMIT", 20000, 70, 65, None, "hal", None),
                 ("Cat deal", rep, open_stages[1], "OPEN", "NEW", "COMMIT", 30000, 120, None, "Charlie", "cat", None),
                 ("Ann deal", owner, open_stages[0], "OPEN", "NEW", "BEST_CASE", 15000, 150, None, "Alpha", "ann",
                  None),
                 ("Lost deal", rep, lost_stage, "LOST", "NEW", "OMITTED", 8000, 60, 55, "Charlie", "dan", "Timing"),
                 ("Solo deal", solo, open_stages[0], "OPEN", "EXISTING", "PIPELINE", 12000, 200, None, "Delta",
                  None, None),
                 ("Undated", owner, open_stages[0], "OPEN", "NEW", "PIPELINE", 5000, None, None, None, None, None)]
    for name, own, stage, status, ctype, cat, amount, close, closed, cname, lead_of, reason in deal_rows:
        oid = ids.new(f"<deal:{name}>")
        _insert("opportunities", opportunity_id=oid, tenant_id=tenant, name=name, owner_id=own,
                stage_id=stage["stage_id"], status=status, client_type=ctype, forecast_category=cat,
                forecast_category_manual=0, amount=amount,
                close_date=(D + timedelta(days=close)).date() if close is not None else None,
                closed_at=D + timedelta(days=closed) if closed is not None else None,
                created_at=D + timedelta(days=(closed or 25) - 20), updated_at=D,
                campaign_id=camps.get(cname), lead_id=leads.get(lead_of), closed_reason=reason)
        if lead_of in ("bob", "hal"):
            sql("UPDATE leads SET opportunity_id = :o WHERE lead_id = :l", o=oid, l=leads[lead_of])


def _seed_work(c):
    ids, tenant, owner, mgr, rep, solo = (c[k] for k in ("ids", "tenant", "owner", "mgr", "rep", "solo"))
    prospects = c["prospects"]
    for user, quarter, amount in [(owner, 1, 10000), (mgr, 1, 40000), (mgr, 2, 30000), (rep, 2, 25000),
                                  (rep, 3, 25000), (solo, 3, 5000)]:
        _insert("sales_targets", target_id=ids.new("<target>"), tenant_id=tenant, user_id=user, fy=FY,
                quarter=quarter, amount=amount)

    for i, (who, kind, day) in enumerate([(rep, "CALL", 3), (rep, "CALL", 4), (rep, "MEETING", 5),
                                          (mgr, "NOTE", 6), (solo, "CALL", 7), (owner, "MEETING", 40)]):
        _insert("contact_activities", activity_id=ids.new("<activity>"), tenant_id=tenant, activity_type=kind,
                created_by=who, occurred_at=D + timedelta(days=day), source="MANUAL" if i % 2 else "EMAIL",
                prospect_id=prospects["cat"], created_at=D, updated_at=D)

    for i, (who, status, day) in enumerate([(rep, "DONE", 5), (rep, "OPEN", 6), (mgr, "DONE", 7),
                                            (solo, "DONE", 50)]):
        _insert("crm_tasks", task_id=ids.new("<task>"), tenant_id=tenant, title=f"Task {i}", status=status,
                priority="HIGH" if i % 2 else "NORMAL", task_type="CALL" if i % 2 else "TODO", owner_id=who,
                completed_at=D + timedelta(days=day) if status == "DONE" else None,
                due_at=D + timedelta(days=day), created_at=D + timedelta(days=day - 1), updated_at=D)

    for who, day in [(owner, 0), (owner, 1), (mgr, 2), (rep, 8), (solo, 15)]:
        _insert("user_activity_days", tenant_id=tenant, user_id=who, day=(D + timedelta(days=day)).date())



# ── Normalisation ─────────────────────────────────────────────

def _normalise(value, ids):
    today = date.today().isoformat()
    if isinstance(value, dict):
        return {_normalise(k, ids): ("<volatile>" if k in _VOLATILE else _normalise(v, ids))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v, ids) for v in value]
    if isinstance(value, str):
        value = _UUID.sub(lambda m: ids.labels.get(m.group(0), "<uuid>"), value)
        for full, label in ids.labels.items():   # file names carry an id's first 8 characters
            value = value.replace(f"_{full[:8]}.", f"_{label}.")
        return value.replace(RUN, "<RUN>").replace(today, "<today>")
    return value


def _canonical(value):
    """Lists of records in a canonical order. Most reports sort in Python over rows SQL returns in no
    fixed order, so records with equal sort keys may swap between runs; ranks, dates and keys are
    fields of the records and are still compared. Orderings that matter are asserted separately."""
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [_canonical(v) for v in value]
        if items and all(isinstance(v, dict) for v in items):
            items.sort(key=lambda v: json.dumps(v, sort_keys=True, default=str))
        return items
    return value


def _canonical_lines(lines: list) -> list:
    """CSV / table text: data lines sorted within each section (a section starts after a blank
    line or a ``===`` marker; its first line is the header)."""
    out, block = [], []

    def flush():
        out.extend(block[:1] + sorted(block[1:]))
        block.clear()

    for line in lines:
        if not line or line.startswith("===") or line.startswith("Period,"):
            flush()
            out.append(line)
        else:
            block.append(line)
    flush()
    return out


def _docx_text(content: bytes) -> list:
    from docx import Document

    doc = Document(io.BytesIO(content))
    lines = [p.text for p in doc.paragraphs if p.text and not p.text.startswith("Generated:")]
    for table in doc.tables:
        lines.append("")
        lines.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows)
    return lines


def _capture(client, headers, method, path, ids, **kw):
    r = client.request(method, path, headers=headers, **kw)
    ctype = r.headers.get("content-type", "")
    if "json" in ctype:
        body = r.json()
    elif "wordprocessingml" in ctype:
        body = _docx_text(r.content)
    else:
        body = r.text.splitlines()
    body = _normalise(body, ids)
    is_text = isinstance(body, list) and all(isinstance(line, str) for line in body)
    body = _canonical_lines(body) if is_text else _canonical(body)
    return {"status": r.status_code, "body": body,
            "disposition": _normalise(r.headers.get("content-disposition"), ids)}


@pytest.fixture(scope="module")
def seeded(client):
    return _seed(client)


@pytest.fixture(scope="module")
def outputs(client, seeded):
    s = seeded
    ids, users = s["ids"], s["users"]
    rng = {"start_date": "2025-05-01", "end_date": "2025-05-31"}
    srng = {"date_from": "2025-05-01", "date_to": "2025-07-31"}
    out = {}
    gets = [
        ("reports/dashboard-summary", "/reports/dashboard-summary", rng),
        ("reports/dashboard-summary/weekly", "/reports/dashboard-summary", {**rng, "granularity": "weekly"}),
        ("reports/global-analytics", "/reports/global-analytics", rng),
        ("reports/campaign-comparison", "/reports/campaign-comparison", {}),
        ("reports/campaign-comparison/range", "/reports/campaign-comparison",
         {**rng, "sort_by": "campaign_name", "sort_dir": "asc", "page_size": 2, "page": 2}),
        ("reports/campaign-comparison/reply", "/reports/campaign-comparison",
         {**rng, "sort_by": "reply_rate", "sort_dir": "asc"}),
        ("reports/campaign-comparison/name", "/reports/campaign-comparison", {"sort_by": "campaign_name"}),
        ("reports/campaign-comparison/bogus", "/reports/campaign-comparison", {"sort_by": "bogus"}),
        ("reports/provider-performance", "/reports/provider-performance", rng),
        ("reports/export/global-analytics", "/reports/export/global-analytics", rng),
        ("reports/export/global-analytics/docx", "/reports/export/global-analytics/docx", rng),
        ("reports/export/campaign-comparison", "/reports/export/campaign-comparison", {}),
        ("reports/export/deliverability", "/reports/export/deliverability", {}),
        ("reports/export/prospects/alpha", f"/reports/export/prospects/{s['camps']['Alpha']}", {}),
        ("reports/export/prospects/charlie", f"/reports/export/prospects/{s['camps']['Charlie']}", {}),
        ("sales-reports/dashboard", "/sales-reports/dashboard", srng),
        ("sales-reports/funnel", "/sales-reports/funnel", srng),
        ("sales-reports/leads", "/sales-reports/leads", {"date_from": "2025-05-01", "date_to": "2025-09-30"}),
        ("sales-reports/pipeline", "/sales-reports/pipeline", {}),
        ("sales-reports/pipeline/new", "/sales-reports/pipeline", {"client_type": "NEW"}),
        ("sales-reports/forecast", "/sales-reports/forecast", {"fy": FY}),
        ("sales-reports/forecast/existing", "/sales-reports/forecast", {"fy": FY, "client_type": "EXISTING"}),
        ("sales-reports/team-performance", "/sales-reports/team-performance", srng),
        ("sales-reports/targets", "/sales-reports/targets", {"fy": FY}),
        ("sales-reports/targets/q2", "/sales-reports/targets", {"fy": FY, "quarter": 2}),
        ("sales-reports/leaderboard", "/sales-reports/leaderboard", srng),
        ("sales-reports/campaign-roi", "/sales-reports/campaign-roi", {}),
        ("sales-reports/campaign-roi/range", "/sales-reports/campaign-roi", srng),
        ("sales-reports/sql-to-win", "/sales-reports/sql-to-win", {"fy": FY}),
        ("sales-reports/sql-to-win/range", "/sales-reports/sql-to-win", srng),
        ("sales-reports/kpis", "/sales-reports/kpis", {"fy": FY, "quarter": 1}),
        ("sales-reports/kpis/range", "/sales-reports/kpis", srng),
        ("sales-reports/kpis/year", "/sales-reports/kpis", {"fy": FY}),
        ("reports/custom/meta", "/reports/custom/meta", {}),
        ("reports/custom", "/reports/custom", {}),
    ]
    for viewer, headers in s["viewers"].items():
        for key, path, params in gets:
            out[f"{viewer} GET {key}"] = _capture(client, headers, "GET", path, ids, params=params)
    owner_h, mgr_h = s["viewers"]["owner"], s["viewers"]["mgr"]
    # Scope options and refusals
    extra = [
        ("owner", "/reports/global-analytics", {**rng, "user_id": users["rep"]}),
        ("owner", "/reports/dashboard-summary", {**rng, "user_id": users["mgr"]}),
        ("owner", "/reports/export/global-analytics", {**rng, "user_id": users["rep"]}),
        ("owner", "/reports/export/campaign-comparison", {"user_id": users["rep"]}),
        ("mgr", "/reports/global-analytics", {**rng, "user_id": users["rep"]}),
        ("mgr", "/reports/global-analytics", {**rng, "user_id": users["solo"]}),
        ("rep", "/reports/export/prospects/" + s["camps"]["Alpha"], {}),
        ("solo", "/reports/export/prospects/" + s["camps"]["Delta"], {}),
        ("owner", "/reports/export/prospects/" + str(uuid.uuid4()), {}),
        ("owner", "/sales-reports/dashboard", {**srng, "member": users["mgr"]}),
        ("owner", "/sales-reports/team-performance", {**srng, "member": users["mgr"]}),
        ("owner", "/sales-reports/kpis", {"fy": FY, "member": users["mgr"]}),
        ("mgr", "/sales-reports/funnel", {**srng, "member": users["solo"]}),
        ("owner", "/sales-reports/funnel", {"date_from": "2025-06-01", "date_to": "2025-05-01"}),
        ("owner", "/sales-reports/kpis", {"quarter": 2}),
    ]
    for viewer, path, params in extra:
        key = f"{viewer} GET {path} {sorted(params.items())}"
        out[_normalise(key, ids)] = _capture(client, s["viewers"][viewer], "GET", path, ids, params=params)

    # Custom reports: ad-hoc runs over every object, filters, matrix and money measures
    runs = [
        {"object": "leads", "group_by": "stage"},
        {"object": "leads", "group_by": "owner", "group_by_2": "stage"},
        {"object": "leads", "group_by": "created_month", "date_field": "created_at", "date_from": "2025-05-01",
         "date_to": "2025-06-30"},
        {"object": "leads", "group_by": "source", "filters": [{"field": "stage", "operator": "is_not",
                                                               "value": ["NEW", "SQL"]}]},
        {"object": "leads", "group_by": "campaign", "filters": [{"field": "owner", "operator": "is",
                                                                 "value": "me"}]},
        {"object": "deals", "group_by": "stage", "measure": "sum_amount"},
        {"object": "deals", "group_by": "status", "group_by_2": "client_type", "measure": "avg_amount"},
        {"object": "deals", "group_by": "close_month", "measure": "count",
         "filters": [{"field": "forecast_category", "operator": "is_not_empty"}]},
        {"object": "deals", "group_by": "company"},
        {"object": "contacts", "group_by": "country", "filters": [{"field": "industry", "operator": "is_empty"}]},
        {"object": "contacts", "group_by": "lifecycle_stage", "group_by_2": "owner"},
        {"object": "contacts", "group_by": "industry", "filters": [{"field": "country", "operator": "contains",
                                                                   "value": "un"}]},
        {"object": "activities", "group_by": "activity_type", "date_field": "occurred_at",
         "date_from": "2025-05-01", "date_to": "2025-05-31"},
        {"object": "activities", "group_by": "month", "group_by_2": "source"},
        {"object": "tasks", "group_by": "status", "group_by_2": "priority"},
        {"object": "tasks", "group_by": "task_type", "filters": [{"field": "status", "operator": "is",
                                                                  "value": "DONE"}]},
        {"object": "nope", "group_by": "x"},
        {"object": "leads", "group_by": "nope"},
        {"object": "leads", "group_by": "stage", "measure": "sum_amount"},
        {"object": "leads", "group_by": "stage", "date_field": "nope"},
        {"object": "leads", "group_by": "stage", "filters": [{"field": "stage", "operator": "nope"}]},
        {"object": "leads", "group_by": "stage", "filters": [{"field": "stage", "operator": "contains",
                                                              "value": ["a"]}]},
    ]
    for viewer in ("owner", "mgr", "rep"):
        for i, d in enumerate(runs):
            out[f"{viewer} POST reports/custom/run #{i}"] = _capture(client, s["viewers"][viewer], "POST",
                                                                       "/reports/custom/run", ids, json=d)
    # Saved reports: create, list, run, share, update, delete
    r = client.post("/reports/custom", headers=mgr_h, json={"name": "  Team leads ", "description": "d",
                                                             "definition": runs[1], "shared": False})
    out["mgr POST reports/custom"] = {"status": r.status_code, "body": _normalise(r.json(), ids)}
    rid = r.json()["report_id"]
    ids.labels[rid] = "<report:team>"
    out["mgr POST reports/custom"]["body"] = _normalise(r.json(), ids)
    for viewer in ("owner", "mgr", "rep"):
        out[f"{viewer} GET saved (private)"] = _capture(client, s["viewers"][viewer], "GET",
                                                        f"/reports/custom/{rid}", ids)
    out["rep PATCH saved (private)"] = _capture(client, s["viewers"]["rep"], "PATCH", f"/reports/custom/{rid}",
                                                ids, json={"shared": True})
    out["mgr PATCH saved"] = _capture(client, mgr_h, "PATCH", f"/reports/custom/{rid}", ids,
                                      json={"shared": True, "name": " ", "description": "dd",
                                            "definition": runs[5]})
    for viewer in ("owner", "mgr", "rep", "solo"):
        out[f"{viewer} GET saved (shared)"] = _capture(client, s["viewers"][viewer], "GET",
                                                       f"/reports/custom/{rid}", ids)
        out[f"{viewer} GET reports/custom (after share)"] = _capture(client, s["viewers"][viewer], "GET",
                                                                     "/reports/custom", ids)
    out["rep DELETE saved"] = _capture(client, s["viewers"]["rep"], "DELETE", f"/reports/custom/{rid}", ids)
    out["rep POST reports/custom"] = _capture(client, s["viewers"]["rep"], "POST", "/reports/custom", ids,
                                              json={"name": "x", "definition": runs[0]})
    out["mgr POST reports/custom (no name)"] = _capture(client, mgr_h, "POST", "/reports/custom", ids,
                                                        json={"name": " ", "definition": runs[0]})
    out["mgr POST reports/custom (bad def)"] = _capture(client, mgr_h, "POST", "/reports/custom", ids,
                                                        json={"name": "bad", "definition": runs[17]})
    out["owner DELETE saved"] = _capture(client, owner_h, "DELETE", f"/reports/custom/{rid}", ids)
    out["owner GET saved (deleted)"] = _capture(client, owner_h, "GET", f"/reports/custom/{rid}", ids)
    return out


def test_report_outputs_match_golden(outputs):
    if os.environ.get("A6_WRITE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(outputs, indent=1, sort_keys=True, default=str) + "\n")
    golden = json.loads(GOLDEN.read_text())
    current = json.loads(json.dumps(outputs, sort_keys=True, default=str))
    assert sorted(current) == sorted(golden)
    mismatches = [key for key in golden if golden[key] != current[key]]
    assert not mismatches, "\n".join(
        f"{k}:\n  golden  {json.dumps(golden[k], sort_keys=True)[:1500]}\n  current {json.dumps(current[k], sort_keys=True)[:1500]}"
        for k in mismatches[:5])


def test_report_orderings(client, seeded):
    """The orderings the golden comparison does not pin (it compares records order-insensitively)."""
    h = seeded["viewers"]["owner"]

    def names(**params):
        r = client.get("/reports/campaign-comparison", headers=h, params=params)
        assert r.status_code == 200, r.text
        return [i["campaign_name"].split()[0] for i in r.json()["items"]]

    assert names(sort_by="campaign_name", sort_dir="asc") == ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    assert names(sort_by="campaign_name") == ["Echo", "Delta", "Charlie", "Bravo", "Alpha"]
    assert names(sort_by="bogus") == ["Echo", "Delta", "Charlie", "Bravo", "Alpha"]       # created_at desc
    assert names(sort_by="sent_count", sort_dir="asc")[0] == "Echo"
    assert names(sort_by="bounce_rate")[0] == "Bravo"
    r = client.get("/reports/global-analytics", headers=h,
                   params={"start_date": "2025-05-01", "end_date": "2025-05-31", "granularity": "weekly"})
    dates = [p["date"] for p in r.json()["timeseries"]]
    assert dates == ["2025-05-01", "2025-05-08", "2025-05-15", "2025-05-22", "2025-05-29"]
    r = client.post("/reports/custom/run", headers=h, json={"object": "leads", "group_by": "created_month"})
    months = [row["key"] for row in r.json()["rows"]]
    assert months == sorted(months) and len(months) > 2
    r = client.post("/reports/custom/run", headers=h, json={"object": "deals", "group_by": "status",
                                                            "measure": "sum_amount"})
    values = [row["value"] for row in r.json()["rows"]]
    assert values == sorted(values, reverse=True)
    r = client.get("/reports/export/deliverability", headers=h)
    sent = [int(line.split(",")[2]) for line in r.text.splitlines()[1:]]
    assert sent == sorted(sent, reverse=True)
    r = client.get("/sales-reports/leaderboard", headers=h, params={"date_from": "2025-05-01",
                                                                     "date_to": "2025-07-31"})
    rows = r.json()["rows"]
    assert [row["rank"] for row in rows] == list(range(1, len(rows) + 1))
    assert [row["won_amount"] for row in rows] == sorted((row["won_amount"] for row in rows), reverse=True)


def test_csv_exports_neutralise_spreadsheet_formulas(client, seeded):
    """A campaign or mailbox name starting with = + - @ (or tab / CR) is exported with a leading quote,
    so Excel / Sheets show it as text instead of evaluating it; the export's own headers are unchanged."""
    tenant = sql("SELECT tenant_id FROM users WHERE user_id = :u", u=seeded["users"]["owner"])
    _insert("campaigns", campaign_id=str(uuid.uuid4()), tenant_id=tenant, campaign_name='=HYPERLINK("http://x")',
            created_by=seeded["users"]["owner"], status="DRAFT", created_at=D, updated_at=D)
    h = seeded["viewers"]["owner"]
    r = client.get("/reports/export/campaign-comparison", headers=h)
    assert r.status_code == 200
    lines = r.text.splitlines()
    assert lines[0].startswith("Campaign Name,")
    assert any(line.startswith('"\'=HYPERLINK(""http://x"")"') for line in lines), lines
    r = client.get("/reports/export/global-analytics", headers=h,
                   params={"start_date": "2025-05-01", "end_date": "2025-05-02"})
    assert r.text.splitlines()[0] == "=== OVERALL SUMMARY ==="
    assert "'=HYPERLINK" in r.text
