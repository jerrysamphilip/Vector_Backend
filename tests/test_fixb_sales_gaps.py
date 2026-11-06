"""
Sales UAT gap fixes (fix agent B), functional (MySQL):

  * S1  converting a lead straight into a won/lost stage is refused (400): the close reason (BR-SF-05)
        is recorded when the deal is closed, never skipped by the convert shortcut
  * S3  GET /opportunities?no_close_date=true lists the deals without a close date, matching the
        forecast's "open without close date" count
  * S5  a custom report whose To is before its From is a 400, like the other sales reports
"""
import uuid

import pytest

from tests.conftest import RUN, register_tenant, sql


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "fixb")


@pytest.fixture(scope="module")
def stages(client, ws):
    data = client.get("/sales/stages", headers=ws["headers"]).json()
    return {"open": next(s for s in data if s["status"] == "OPEN"),
            "won": next(s for s in data if s["status"] == "WON"),
            "lost": next(s for s in data if s["status"] == "LOST")}


def _sql_lead(client, h, label):
    r = client.post("/contacts", headers=h, json={"email": f"{label}.{uuid.uuid4().hex[:6]}.{RUN}@fixb{RUN}.example.com",
                                                  "first_name": label.capitalize()})
    assert r.status_code == 201, r.text
    r = client.post("/leads", headers=h, json={"prospect_id": r.json()["prospect_id"], "source": "Test"})
    assert r.status_code == 201, r.text
    lid = r.json()["lead_id"]
    r = client.patch(f"/leads/{lid}", headers=h, json={
        "qualification": {"budget": True, "authority": True, "need": True, "timeline": True}, "stage": "SQL"})
    assert r.status_code == 200, r.text
    return lid


# ── S1 ──

def test_convert_into_a_closed_stage_is_refused(client, ws, stages):
    h = ws["headers"]
    lid = _sql_lead(client, h, "conv")
    for closed in ("won", "lost"):
        r = client.post(f"/leads/{lid}/convert", headers=h, json={"stage_id": stages[closed]["stage_id"]})
        assert r.status_code == 400, r.text
        assert "open stage" in r.json()["detail"]
    # Nothing was written: the lead is still an SQL with no deal
    assert sql("SELECT stage FROM leads WHERE lead_id = :l", l=lid) == "SQL"
    assert sql("SELECT COUNT(*) FROM opportunities WHERE lead_id = :l", l=lid) == 0

    r = client.post(f"/leads/{lid}/convert", headers=h, json={"stage_id": stages["open"]["stage_id"]})
    assert r.status_code == 201, r.text
    assert r.json()["stage_id"] == stages["open"]["stage_id"]
    assert sql("SELECT stage FROM leads WHERE lead_id = :l", l=lid) == "CONVERTED"


def test_convert_without_a_stage_still_uses_the_first_open_stage(client, ws, stages):
    lid = _sql_lead(client, ws["headers"], "convdef")
    r = client.post(f"/leads/{lid}/convert", headers=ws["headers"], json={})
    assert r.status_code == 201, r.text
    assert r.json()["stage_id"] == stages["open"]["stage_id"]


# ── S3 ──

def test_deal_list_filters_to_deals_without_a_close_date(client, ws, stages):
    h = ws["headers"]
    made = {}
    for key, close in (("undated", None), ("dated", "2026-12-15")):
        r = client.post("/opportunities", headers=h, json={"name": f"S3 {key} {RUN}", "amount": 1000,
                                                            "stage_id": stages["open"]["stage_id"], "close_date": close})
        assert r.status_code == 201, r.text
        made[key] = r.json()["opportunity_id"]

    r = client.get("/opportunities", headers=h, params={"no_close_date": "true", "status": "OPEN"})
    assert r.status_code == 200, r.text
    ids = {o["opportunity_id"] for o in r.json()["items"]}
    assert made["undated"] in ids and made["dated"] not in ids
    assert all(o["close_date"] is None for o in r.json()["items"])

    # Same deals as the forecast's "open without close date" tile
    fc = client.get("/sales-reports/forecast", headers=h)
    assert fc.status_code == 200, fc.text
    assert fc.json()["open_without_close_date"]["count"] == r.json()["total"]

    # Without the filter both are listed
    ids = {o["opportunity_id"] for o in client.get("/opportunities", headers=h).json()["items"]}
    assert {made["undated"], made["dated"]} <= ids


# ── S5 ──

def test_custom_report_with_to_before_from_is_a_400(client, ws):
    h = ws["headers"]
    base = {"object": "leads", "group_by": "stage", "date_field": "created_at"}
    r = client.post("/reports/custom/run", headers=h, json={**base, "date_from": "2026-06-30", "date_to": "2026-05-01"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "date_from is after date_to"
    r = client.post("/reports/custom/run", headers=h, json={**base, "date_from": "2026-05-01", "date_to": "2026-05-01"})
    assert r.status_code == 200, r.text
