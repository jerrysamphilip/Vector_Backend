"""CRM data-integrity fixes: CSV formula injection, won deals, purge, merge."""

import pytest

from tests.conftest import RUN, sql


def _contact(client, headers, email, **kw):
    r = client.post("/contacts", headers=headers, json={"email": email, **kw})
    assert r.status_code in (200, 201), r.text
    return r.json()["prospect_id"]


@pytest.fixture(scope="module")
def stages(client, tenant_a):
    r = client.get("/sales/stages", headers=tenant_a["headers"])
    assert r.status_code == 200, r.text
    data = r.json()
    won = next(s for s in data if s["is_won"])
    first_open = next(s for s in data if s["status"] == "OPEN")
    return {"won": won, "open": first_open}


def test_export_neutralises_formulas(client, tenant_a):
    h = tenant_a["headers"]
    _contact(client, h, f"f.{RUN}@formula{RUN}.com", first_name='=HYPERLINK("http://x","y")', last_name="+cmd")
    r = client.get(f"/contacts/export?format=csv&columns=full_name,email&q=formula{RUN}", headers=h)
    assert r.status_code == 200
    raw = r.content
    assert b"'=HYPERLINK" in raw
    assert b"\n=HYPERLINK" not in raw and b',=HYPERLINK' not in raw


def test_deal_created_as_won_gets_closed_at(client, tenant_a, stages):
    h = tenant_a["headers"]
    p = _contact(client, h, f"d.{RUN}@deal{RUN}.com", first_name="Dee")
    r = client.post("/opportunities", headers=h, json={
        "name": f"Won {RUN}", "prospect_id": p, "stage_id": stages["won"]["stage_id"],
        "amount": "5000", "closed_reason": "Price"})
    assert r.status_code == 201, r.text
    assert sql("SELECT closed_at IS NOT NULL FROM opportunities WHERE opportunity_id = :o",
               o=r.json()["opportunity_id"]) == 1


def test_purge_contact_with_lead_and_deal(client, tenant_a, stages):
    h = tenant_a["headers"]
    p = _contact(client, h, f"purge.{RUN}@purge{RUN}.com", first_name="Pur")
    r = client.post("/opportunities", headers=h, json={
        "name": f"Won purge {RUN}", "prospect_id": p, "stage_id": stages["won"]["stage_id"], "amount": "10",
        "closed_reason": "Price"})
    assert r.status_code == 201, r.text
    deal_id = r.json()["opportunity_id"]
    r = client.post("/leads", headers=h, json={"prospect_id": p, "source": "Test"})
    assert r.status_code in (200, 201), r.text
    assert client.delete(f"/contacts/{p}", headers=h).status_code in (200, 204)
    sql("UPDATE prospects SET deleted_at = NOW() - INTERVAL 100 DAY WHERE prospect_id = :p", p=p)

    from app.core.database import SessionLocal
    from app.services.crm import purge_expired
    with SessionLocal() as db:
        assert purge_expired(db) >= 1
        db.commit()

    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=p) == 0
    assert sql("SELECT COUNT(*) FROM leads WHERE prospect_id = :p", p=p) == 0
    # The won deal is business history: kept, with the contact link cleared
    assert sql("SELECT COUNT(*) FROM opportunities WHERE opportunity_id = :o", o=deal_id) == 1
    assert sql("SELECT prospect_id FROM opportunities WHERE opportunity_id = :o", o=deal_id) is None


def test_merge_moves_lead_and_deal(client, tenant_a, stages):
    h = tenant_a["headers"]
    x = _contact(client, h, f"mx.{RUN}@merge{RUN}.com", first_name="Max", last_name="M")
    y = _contact(client, h, f"my.{RUN}@merge{RUN}.com", first_name="Max", last_name="M")
    r = client.post("/leads", headers=h, json={"prospect_id": y, "source": "Test"})
    assert r.status_code in (200, 201), r.text
    lead_id = r.json()["lead_id"]
    r = client.post("/opportunities", headers=h, json={
        "name": f"M {RUN}", "prospect_id": y, "stage_id": stages["open"]["stage_id"]})
    assert r.status_code == 201, r.text
    deal_id = r.json()["opportunity_id"]

    r = client.post("/contacts/merge", headers=h, json={"primary_id": x, "duplicate_ids": [y], "choices": {}})
    assert r.status_code == 200, r.text
    assert sql("SELECT prospect_id FROM leads WHERE lead_id = :l", l=lead_id) == x
    assert sql("SELECT prospect_id FROM opportunities WHERE opportunity_id = :o", o=deal_id) == x


def test_other_tenant_cannot_see_contacts(client, tenant_a, tenant_b):
    p = _contact(client, tenant_a["headers"], f"iso.{RUN}@iso{RUN}.com", first_name="Iso")
    assert client.get(f"/contacts/{p}", headers=tenant_b["headers"]).status_code == 404
    assert client.get(f"/contacts/{p}", headers=tenant_a["headers"]).status_code == 200
