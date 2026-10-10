"""Sending domains are tenant-owned: two tenants on the same domain name never share
domain records, stats, provider data or auto-pause decisions."""

import uuid
from datetime import datetime, timedelta

import pytest

from app.core.database import SessionLocal
from tests.conftest import RUN, sql


def _make_inbox(client, tenant, domain, local="sender"):
    r = client.post("/inboxes", headers=tenant["headers"], json={
        "email_address": f"{local}@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": local, "smtp_password": "fixture-smtp-pass",
        "imap_host": "127.0.0.1", "imap_port": 1, "imap_username": local,
        "imap_password": "fixture-imap-pass", "warmup_enabled": False,
    })
    assert r.status_code == 201, r.text
    return r.json()["inbox_id"]


def _make_active_campaign(client, tenant, inbox_id, label):
    r = client.post("/campaigns", headers=tenant["headers"], json={"campaign_name": f"{label} {RUN}"})
    assert r.status_code == 201, r.text
    campaign_id = r.json()["campaign_id"]
    sql("UPDATE campaigns SET status = 'ACTIVE' WHERE campaign_id = :c", c=campaign_id)
    sql("INSERT IGNORE INTO campaign_inboxes (campaign_id, inbox_id) VALUES (:c, :i)", c=campaign_id, i=inbox_id)
    return campaign_id


def _make_prospect(client, tenant, label):
    r = client.post("/contacts", headers=tenant["headers"],
                    json={"email": f"{label}.{uuid.uuid4().hex[:6]}@rcpt{RUN}.example.com", "first_name": "Pat"})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _send(inbox_id, campaign_id, prospect_id, from_email, n, bounced=0):
    """Insert n outbound messages sent in the last hour; the first `bounced` are hard bounces."""
    from app.models.email_message import EmailEvent, EmailMessage
    ids = []
    with SessionLocal() as db:
        for k in range(n):
            hard = k < bounced
            msg = EmailMessage(
                campaign_id=campaign_id, prospect_id=prospect_id, inbox_id=inbox_id,
                direction="OUTBOUND", status="BOUNCED" if hard else "SENT",
                last_error_code="BOUNCE_Permanent" if hard else None,
                subject="hi", body_text="hi", to_email=f"r{k}@rcpt{RUN}.example.com",
                from_email=from_email, sent_at=datetime.utcnow() - timedelta(minutes=5),
            )
            db.add(msg)
            db.flush()
            if hard:
                db.add(EmailEvent(message_id=msg.message_id, event_type=EmailEvent.EVENT_BOUNCE,
                                  event_time=datetime.utcnow()))
            ids.append(msg.message_id)
        db.commit()
    return ids


def _domain_rows(domain):
    from sqlalchemy import text
    from app.core.database import engine
    with engine.connect() as conn:
        return {r[0]: r for r in conn.execute(text(
            "SELECT tenant_id, domain_id, bounce_rate_24h, sends_24h FROM sending_domains "
            "WHERE domain_name = :d"), {"d": domain})}


@pytest.fixture(scope="module")
def shared(client, tenant_a, tenant_b):
    """Both tenants have a mailbox, an active campaign and a contact on the same domain."""
    domain = f"shared{RUN}.example.com"
    out = {"domain": domain}
    for key, tenant in (("a", tenant_a), ("b", tenant_b)):
        inbox = _make_inbox(client, tenant, domain, local=f"sales-{key}")
        out[key] = {
            "inbox_id": inbox,
            "from": f"sales-{key}@{domain}",
            "campaign_id": _make_active_campaign(client, tenant, inbox, f"Shared {key}"),
            "prospect_id": _make_prospect(client, tenant, f"shared-{key}"),
        }
    return out


def test_each_tenant_gets_its_own_domain_record(client, tenant_a, tenant_b, shared):
    rows = _domain_rows(shared["domain"])
    assert set(rows) == {tenant_a["tenant_id"], tenant_b["tenant_id"]}
    assert rows[tenant_a["tenant_id"]][1] != rows[tenant_b["tenant_id"]][1]

    for key, tenant in (("a", tenant_a), ("b", tenant_b)):
        r = client.get("/deliverability/domains", headers=tenant["headers"])
        assert r.status_code == 200, r.text
        mine = [d for d in r.json() if d["domain_name"] == shared["domain"]]
        assert len(mine) == 1
        assert mine[0]["domain_id"] == rows[tenant["tenant_id"]][1]
        # Only the caller's own mailboxes and campaigns are associated
        assert mine[0]["associated_inboxes"] == [shared[key]["from"]]


def test_stats_and_sent_log_only_include_callers_mail(client, tenant_a, tenant_b, shared):
    a_ids = _send(shared["a"]["inbox_id"], shared["a"]["campaign_id"], shared["a"]["prospect_id"],
                  shared["a"]["from"], 3)
    b_ids = _send(shared["b"]["inbox_id"], shared["b"]["campaign_id"], shared["b"]["prospect_id"],
                  shared["b"]["from"], 2)

    params = {"domain": shared["domain"]}
    sa = client.get("/deliverability/statistics", headers=tenant_a["headers"], params=params)
    sb = client.get("/deliverability/statistics", headers=tenant_b["headers"], params=params)
    assert sa.status_code == 200 and sb.status_code == 200, (sa.text, sb.text)
    assert sa.json()["summary"]["total_attempts"] == 3
    assert sb.json()["summary"]["total_attempts"] == 2

    # Without a domain: the tenant's own aggregate (never the account-wide SES numbers)
    agg_b = client.get("/deliverability/statistics", headers=tenant_b["headers"])
    assert agg_b.status_code == 200, agg_b.text
    assert agg_b.json()["summary"]["total_attempts"] >= 2
    agg_a = client.get("/deliverability/statistics", headers=tenant_a["headers"]).json()
    a_total = sql(
        "SELECT COUNT(*) FROM email_messages m LEFT JOIN sending_inboxes i ON i.inbox_id = m.inbox_id "
        "LEFT JOIN campaigns c ON c.campaign_id = m.campaign_id WHERE m.direction = 'OUTBOUND' "
        "AND m.sent_at >= UTC_TIMESTAMP() - INTERVAL 14 DAY AND (i.tenant_id = :t OR c.tenant_id = :t)",
        t=tenant_a["tenant_id"])
    assert agg_a["summary"]["total_attempts"] == a_total

    path = f"/deliverability/domains/{shared['domain']}/sent-log"
    log_a = {e["message_id"] for e in client.get(path, headers=tenant_a["headers"]).json()}
    log_b = {e["message_id"] for e in client.get(path, headers=tenant_b["headers"]).json()}
    assert set(a_ids) <= log_a and not (set(b_ids) & log_a)
    assert set(b_ids) <= log_b and not (set(a_ids) & log_b)


def test_domain_reads_404_for_a_tenant_not_on_the_domain(client, tenant_b, tenant_a_data):
    domain = tenant_a_data["domain"]  # only tenant A has a mailbox there
    hb = tenant_b["headers"]
    assert client.get("/deliverability/statistics", headers=hb, params={"domain": domain}).status_code == 404
    assert client.post(f"/deliverability/domains/{domain}/scans", headers=hb).status_code == 404
    assert client.delete(f"/deliverability/domains/{domain}", headers=hb).status_code == 404
    assert client.get("/deliverability/integrations/metrics", headers=hb,
                      params={"provider": "GOOGLE_POSTMASTER", "domain_name": domain}).status_code == 404
    assert sql("SELECT COUNT(*) FROM sending_domains WHERE domain_name = :d", d=domain) == 1


def test_ingest_is_per_tenant(client, tenant_a, tenant_b, shared):
    domain = shared["domain"]
    r = client.post("/deliverability/integrations/ingest", headers=tenant_a["headers"], json={
        "provider": "GOOGLE_POSTMASTER", "domain_name": domain,
        "payload": [{"date": "2026-10-01", "spam_rate": 0.2, "reputation_score": 40}],
    })
    assert r.status_code == 202, r.text  # used to be 403 because the domain is shared

    q = {"provider": "GOOGLE_POSTMASTER", "domain_name": domain}
    ma = client.get("/deliverability/integrations/metrics", headers=tenant_a["headers"], params=q)
    mb = client.get("/deliverability/integrations/metrics", headers=tenant_b["headers"], params=q)
    assert ma.status_code == 200 and mb.status_code == 200
    assert len(ma.json()) == 1 and ma.json()[0]["spam_rate"] == 0.2
    assert mb.json() == []
    assert sql("SELECT tenant_id FROM external_reputation_metrics WHERE domain_name = :d", d=domain) \
        == tenant_a["tenant_id"]

    status_b = client.get("/deliverability/integrations/status", headers=tenant_b["headers"]).json()
    assert status_b["GOOGLE_POSTMASTER"]["last_run"] is None or \
        status_b["GOOGLE_POSTMASTER"]["last_run"]["run_id"] != r.json()["run_id"]


def test_deleting_own_domain_leaves_other_tenant_untouched(client, tenant_a, tenant_b):
    domain = f"del{RUN}.example.com"
    _make_inbox(client, tenant_a, domain)
    _make_inbox(client, tenant_b, domain)
    rows = _domain_rows(domain)
    assert set(rows) == {tenant_a["tenant_id"], tenant_b["tenant_id"]}
    b_domain_id = rows[tenant_b["tenant_id"]][1]

    # A shared domain no longer needs a platform admin: each tenant manages its own record
    r = client.delete(f"/deliverability/domains/{domain}", headers=tenant_a["headers"])
    assert r.status_code == 204, r.text
    rows = _domain_rows(domain)
    assert set(rows) == {tenant_b["tenant_id"]}
    assert rows[tenant_b["tenant_id"]][1] == b_domain_id
    assert client.delete(f"/deliverability/domains/{domain}", headers=tenant_a["headers"]).status_code == 404

    listed_b = [d["domain_name"] for d in client.get("/deliverability/domains", headers=tenant_b["headers"]).json()]
    assert domain in listed_b
    listed_a = [d["domain_name"] for d in client.get("/deliverability/domains", headers=tenant_a["headers"]).json()]
    assert domain not in listed_a


def test_auto_pause_counts_and_pauses_only_the_sending_tenant(client, tenant_a, tenant_b):
    from app.services.send_safety import check_domain_health, on_risk_event

    domain = f"pause{RUN}.example.com"
    side = {}
    for key, tenant in (("a", tenant_a), ("b", tenant_b)):
        inbox = _make_inbox(client, tenant, domain, local=f"out-{key}")
        side[key] = {
            "inbox_id": inbox,
            "campaign_id": _make_active_campaign(client, tenant, inbox, f"Pause {key}"),
            "prospect_id": _make_prospect(client, tenant, f"pause-{key}"),
            "from": f"out-{key}@{domain}",
        }
    # A: 10 sends, 5 hard bounces (over the limit). B: 10 clean sends on the same domain.
    _send(side["a"]["inbox_id"], side["a"]["campaign_id"], side["a"]["prospect_id"], side["a"]["from"], 10, bounced=5)
    _send(side["b"]["inbox_id"], side["b"]["campaign_id"], side["b"]["prospect_id"], side["b"]["from"], 10)

    with SessionLocal() as db:
        assert check_domain_health(db, domain, "test", tenant_id=tenant_b["tenant_id"]) == 0
        # A bounce event without a campaign, attributed to B, must not pause anything of B's
        on_risk_event(db, None, domain, "hard bounce", tenant_id=tenant_b["tenant_id"])
    assert sql("SELECT status FROM campaigns WHERE campaign_id = :c", c=side["b"]["campaign_id"]) == "ACTIVE"

    with SessionLocal() as db:
        assert check_domain_health(db, domain, "hard bounce", tenant_id=tenant_a["tenant_id"]) == 1

    assert sql("SELECT status FROM campaigns WHERE campaign_id = :c", c=side["a"]["campaign_id"]) == "PAUSED"
    assert sql("SELECT auto_paused FROM campaigns WHERE campaign_id = :c", c=side["a"]["campaign_id"]) == 1
    assert sql("SELECT status FROM campaigns WHERE campaign_id = :c", c=side["b"]["campaign_id"]) == "ACTIVE"
    assert sql("SELECT status FROM sending_inboxes WHERE inbox_id = :i", i=side["b"]["inbox_id"]) == "ACTIVE"

    rows = _domain_rows(domain)
    assert rows[tenant_a["tenant_id"]][2] == pytest.approx(0.5)
    assert rows[tenant_a["tenant_id"]][3] == 10
    assert rows[tenant_b["tenant_id"]][2] == 0
    assert rows[tenant_b["tenant_id"]][3] == 10

    # The alert belongs to A only
    assert sql("SELECT COUNT(*) FROM reputation_alerts WHERE domain_name = :d AND tenant_id = :t",
               d=domain, t=tenant_a["tenant_id"]) == 1
    assert sql("SELECT COUNT(*) FROM reputation_alerts WHERE domain_name = :d AND tenant_id = :t",
               d=domain, t=tenant_b["tenant_id"]) == 0
    alerts_b = client.get("/deliverability/alerts", headers=tenant_b["headers"]).json()
    assert all(a["domain_name"] != domain for a in alerts_b)
    alerts_a = client.get("/deliverability/alerts", headers=tenant_a["headers"]).json()
    assert any(a["domain_name"] == domain and a["alert_type"] == "AUTO_PAUSE" for a in alerts_a)


def test_account_wide_ses_figures_are_platform_admin_only(client, tenant_a):
    assert client.get("/deliverability/statistics/account", headers=tenant_a["headers"]).status_code == 403
    assert client.get("/inboxes/ses/quota", headers=tenant_a["headers"]).status_code == 403
