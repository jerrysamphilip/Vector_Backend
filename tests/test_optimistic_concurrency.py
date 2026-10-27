"""Optimistic concurrency (rule T-3), in-process API tests (FastAPI TestClient + the test MySQL schema).

For each guarded record (contact, company, lead, opportunity incl. stage move, proposal incl. status,
quote lines and discount approval, campaign incl. pause / resume, sequence step):
  - an update with the current row_version succeeds and the version goes up by one;
  - a stale version gets 409 {"code": "STALE_VERSION", "current_version": N, ...};
  - two sequential updates sending the same version: the second one gets 409;
  - the record is unchanged after a 409.
Also: If-Match works like the body field, a missing version is still accepted (transition) and
bumps the version, OCC_REQUIRE_VERSION turns a missing version into 428.
"""
import pytest

from tests.conftest import RUN, register_tenant, sql


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "occ")


def _stale(r, current):
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "STALE_VERSION"
    assert detail["current_version"] == current
    assert "changed by someone else" in detail["message"]


def _contact(client, h, label):
    r = client.post("/contacts", headers=h, json={"email": f"{label}-{RUN}@occ{RUN}.example.com", "first_name": label})
    assert r.status_code == 201, r.text
    return r.json()


def _check(client, h, method, url, get_url, field, first, second, *, extra=None, read=None):
    """The four T-3 checks for one record; returns the final version."""
    read = read or (lambda body: body[field])
    send = getattr(client, method)
    v = client.get(get_url, headers=h).json()["row_version"]
    assert v >= 1
    r = send(url, headers=h, json={field: first, "expected_version": v, **(extra or {})})
    assert r.status_code == 200, r.text
    assert r.json()["row_version"] == v + 1
    # The same (now stale) version again: refused, record unchanged
    r = send(url, headers=h, json={field: second, "expected_version": v, **(extra or {})})
    _stale(r, v + 1)
    body = client.get(get_url, headers=h).json()
    assert read(body) == first and body["row_version"] == v + 1
    # Way off: still 409 with the current version
    _stale(send(url, headers=h, json={field: second, "expected_version": v + 50, **(extra or {})}), v + 1)
    return v + 1


# ── Contacts ─────────────────────────────────────────────────

def test_occ_contact_patch(client, ws):
    h = ws["headers"]
    c = _contact(client, h, "Ann")
    url = f"/contacts/{c['prospect_id']}"
    v = _check(client, h, "patch", url, url, "designation", "CTO", "CEO")
    # If-Match header (quoted and weak forms) works like the body field
    r = client.patch(url, headers={**h, "If-Match": f'"{v}"'}, json={"designation": "VP"})
    assert r.status_code == 200 and r.json()["row_version"] == v + 1, r.text
    _stale(client.patch(url, headers={**h, "If-Match": f'W/"{v}"'}, json={"designation": "No"}), v + 1)
    assert client.patch(url, headers={**h, "If-Match": "abc"}, json={"designation": "No"}).status_code == 400
    # Body and header disagree
    r = client.patch(url, headers={**h, "If-Match": str(v + 1)}, json={"designation": "No", "expected_version": v})
    assert r.status_code == 400
    assert client.get(url, headers=h).json()["designation"] == "VP"


def test_occ_contact_without_version_is_accepted_and_bumps(client, ws, monkeypatch):
    h = ws["headers"]
    c = _contact(client, h, "Bob")
    url = f"/contacts/{c['prospect_id']}"
    v = client.get(url, headers=h).json()["row_version"]
    r = client.patch(url, headers=h, json={"lead_source": "Fair"})
    assert r.status_code == 200 and r.json()["row_version"] == v + 1, r.text
    # A client still holding the old version is now stale
    _stale(client.patch(url, headers=h, json={"lead_source": "Web", "expected_version": v}), v + 1)
    monkeypatch.setenv("OCC_REQUIRE_VERSION", "true")
    r = client.patch(url, headers=h, json={"lead_source": "Web"})
    assert r.status_code == 428 and r.json()["detail"]["code"] == "VERSION_REQUIRED", r.text
    assert client.patch(url, headers=h, json={"lead_source": "Web", "expected_version": v + 1}).status_code == 200


def test_occ_contact_list_and_bulk_edit(client, ws):
    h = ws["headers"]
    c = _contact(client, h, "Cat")
    url = f"/contacts/{c['prospect_id']}"
    v = client.get(url, headers=h).json()["row_version"]
    # Bulk edit takes no version (set-based) but bumps it, so an open form cannot undo it
    r = client.post("/contacts/bulk", headers=h, json={"prospect_ids": [c["prospect_id"]], "action": "add_tags",
                                                      "tags": ["vip"]})
    assert r.status_code == 200, r.text
    assert client.get(url, headers=h).json()["row_version"] == v + 1
    _stale(client.patch(url, headers=h, json={"designation": "x", "expected_version": v}), v + 1)


def test_occ_parallel_saves_with_same_version_one_wins(client, ws):
    """Two saves racing with the same version: the conditional UPDATE lets exactly one through."""
    from concurrent.futures import ThreadPoolExecutor

    h = ws["headers"]
    c = _contact(client, h, "Race")
    url = f"/contacts/{c['prospect_id']}"
    v = client.get(url, headers=h).json()["row_version"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda title: client.patch(url, headers=h, json={
            "designation": title, "expected_version": v}), [f"T{i}" for i in range(4)]))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409, 409, 409], [r.text for r in results]
    winner = next(r for r in results if r.status_code == 200).json()
    body = client.get(url, headers=h).json()
    assert body["designation"] == winner["designation"] and body["row_version"] == v + 1


def test_occ_unknown_record_is_404(client, ws):
    r = client.patch("/contacts/does-not-exist", headers=ws["headers"], json={"designation": "x", "expected_version": 1})
    assert r.status_code == 404


# ── Companies ────────────────────────────────────────────────

def test_occ_account_patch(client, ws):
    h = ws["headers"]
    r = client.post("/accounts", headers=h, json={"name": f"Occ Co {RUN}"})
    assert r.status_code == 201, r.text
    url = f"/accounts/{r.json()['account_id']}"
    _check(client, h, "patch", url, url, "city", "Paris", "Rome")


# ── Leads, opportunities, proposals, quotes ──────────────────

@pytest.fixture(scope="module")
def deal(client, ws):
    h = ws["headers"]
    c = _contact(client, h, "Deal")
    r = client.post("/leads", headers=h, json={"prospect_id": c["prospect_id"], "source": "Test"})
    assert r.status_code == 201, r.text
    lead_id = r.json()["lead_id"]
    r = client.post("/opportunities", headers=h, json={"name": f"Occ deal {RUN}", "prospect_id": c["prospect_id"],
                                                      "amount": 1000})
    assert r.status_code == 201, r.text
    opp = r.json()
    r = client.post("/proposals", headers=h, json={"opportunity_id": opp["opportunity_id"], "title": "Q1"})
    assert r.status_code == 201, r.text
    return {"lead_id": lead_id, "opp": opp, "proposal_id": r.json()["proposal_id"]}


def test_occ_lead_patch(client, ws, deal):
    url = f"/leads/{deal['lead_id']}"
    _check(client, ws["headers"], "patch", url, url, "next_step", "Call", "Email")


def test_occ_opportunity_patch_and_stage_move(client, ws, deal):
    h = ws["headers"]
    url = f"/opportunities/{deal['opp']['opportunity_id']}"
    v = _check(client, h, "patch", url, url, "next_step", "Demo", "Trial")
    # Board drag-and-drop: a stage move from a stale board is refused
    stages = [s for s in client.get("/sales/stages", headers=h).json()
              if not s.get("is_won") and not s.get("is_lost")]
    target = next(s["stage_id"] for s in stages if s["stage_id"] != deal["opp"]["stage_id"])
    _stale(client.patch(url, headers=h, json={"stage_id": target, "expected_version": v - 1}), v)
    assert client.get(url, headers=h).json()["stage_id"] == deal["opp"]["stage_id"]
    r = client.patch(url, headers=h, json={"stage_id": target, "expected_version": v})
    assert r.status_code == 200 and r.json()["stage_id"] == target and r.json()["row_version"] == v + 1, r.text


def _proposal(client, h, proposal_id):
    items = client.get("/proposals", headers=h).json()["items"]
    return next(p for p in items if p["proposal_id"] == proposal_id)


def test_occ_proposal_patch_status_lines_and_approval(client, ws, deal):
    h = ws["headers"]
    pid = deal["proposal_id"]
    url = f"/proposals/{pid}"
    v = _proposal(client, h, pid)["row_version"]
    r = client.patch(url, headers=h, json={"notes": "first", "expected_version": v})
    assert r.status_code == 200 and r.json()["row_version"] == v + 1, r.text
    _stale(client.patch(url, headers=h, json={"status": "SENT", "expected_version": v}), v + 1)
    p = _proposal(client, h, pid)
    assert p["notes"] == "first" and p["status"] == "DRAFT" and p["row_version"] == v + 1
    v += 1

    # Quote lines carry the proposal's version too
    lines = {"lines": [{"description": "Licence", "quantity": 1, "unit_price": 500, "discount_pct": 0}]}
    r = client.put(f"/proposals/{pid}/lines", headers=h, json={**lines, "expected_version": v})
    assert r.status_code == 200 and r.json()["row_version"] == v + 1, r.text
    _stale(client.put(f"/proposals/{pid}/lines", headers=h, json={"lines": [], "expected_version": v}), v + 1)
    assert len(client.get(f"/proposals/{pid}/lines", headers=h).json()["lines"]) == 1
    v += 1

    # Discount approval: a stale submit is refused before any state change
    r = client.put("/quotes/approval-settings", headers=h, json={"discount_approval_threshold": 10})
    assert r.status_code == 200, r.text
    disc = {"lines": [{"description": "Licence", "quantity": 1, "unit_price": 500, "discount_pct": 30}]}
    r = client.put(f"/proposals/{pid}/lines", headers=h, json={**disc, "expected_version": v})
    assert r.status_code == 200, r.text
    v = r.json()["row_version"]
    _stale(client.post(f"/proposals/{pid}/approval/submit", headers=h, json={"expected_version": v - 1}), v)
    assert client.get(f"/proposals/{pid}/approval", headers=h).json()["approval_status"] != "PENDING"
    r = client.post(f"/proposals/{pid}/approval/submit", headers=h, json={"note": "please", "expected_version": v})
    assert r.status_code == 200 and r.json()["row_version"] == v + 1, r.text
    v += 1
    _stale(client.post(f"/proposals/{pid}/approval/reject", headers=h, json={"reason": "no", "expected_version": v - 1}), v)
    r = client.post(f"/proposals/{pid}/approval/approve", headers=h, json={"expected_version": v})
    assert r.status_code == 200 and r.json()["approval_status"] == "APPROVED", r.text
    assert r.json()["row_version"] == v + 1
    client.put("/quotes/approval-settings", headers=h, json={"discount_approval_threshold": None})


# ── Campaigns and sequence steps ─────────────────────────────

@pytest.fixture(scope="module")
def campaign(client, ws):
    r = client.post("/campaigns", headers=ws["headers"], json={"campaign_name": f"Occ campaign {RUN}"})
    assert r.status_code == 201, r.text
    return r.json()


def test_occ_campaign_put(client, ws, campaign):
    url = f"/campaigns/{campaign['campaign_id']}"
    _check(client, ws["headers"], "put", url, url, "sender_name", "Alice", "Mallory")


def test_occ_campaign_pause_resume(client, ws, campaign):
    h = ws["headers"]
    cid = campaign["campaign_id"]
    sql("UPDATE campaigns SET status = 'ACTIVE' WHERE campaign_id = :c", c=cid)
    # Resume needs an active sending inbox (item 12); linked in SQL so the version is unchanged
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@occ{RUN}.example.com", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass", "imap_host": "127.0.0.1",
        "imap_port": 1, "imap_username": "sender", "imap_password": "fixture-imap-pass", "warmup_enabled": False})
    assert r.status_code == 201, r.text
    sql("INSERT INTO campaign_inboxes (campaign_id, inbox_id) VALUES (:c, :i)", c=cid, i=r.json()["inbox_id"])
    v = client.get(f"/campaigns/{cid}", headers=h).json()["row_version"]
    _stale(client.post(f"/campaigns/{cid}/pause", headers=h, json={"expected_version": v - 1}), v)
    assert client.get(f"/campaigns/{cid}", headers=h).json()["status"] == "ACTIVE"
    r = client.post(f"/campaigns/{cid}/pause", headers=h, json={"expected_version": v})
    assert r.status_code == 200 and r.json()["new_status"] == "PAUSED" and r.json()["row_version"] == v + 1, r.text
    # Resume from a screen that still shows the old version: refused, still paused
    _stale(client.post(f"/campaigns/{cid}/resume", headers=h, json={"expected_version": v}), v + 1)
    assert client.get(f"/campaigns/{cid}", headers=h).json()["status"] == "PAUSED"
    r = client.post(f"/campaigns/{cid}/resume", headers={**h, "If-Match": str(v + 1)})
    assert r.status_code == 200 and r.json()["new_status"] == "ACTIVE" and r.json()["row_version"] == v + 2, r.text
    # The list shows the version too
    items = client.get("/campaigns", headers=h).json()["items"]
    assert next(i for i in items if i["campaign_id"] == cid)["row_version"] == v + 2


def test_occ_sequence_step_put(client, ws, campaign):
    h = ws["headers"]
    cid = campaign["campaign_id"]
    r = client.post(f"/campaigns/{cid}/sequences", headers=h, json={"step_number": 1, "wait_days": 0})
    assert r.status_code == 201, r.text
    step = r.json()
    assert step["row_version"] == 1
    url = f"/campaigns/sequences/{step['sequence_id']}"

    def current():
        steps = client.get(f"/campaigns/{cid}/sequences", headers=h).json()["steps"]
        return next(s for s in steps if s["sequence_id"] == step["sequence_id"])

    r = client.put(url, headers=h, json={"wait_days": 3, "expected_version": 1})
    assert r.status_code == 200 and r.json()["row_version"] == 2, r.text
    _stale(client.put(url, headers=h, json={"wait_days": 9, "expected_version": 1}), 2)
    s = current()
    assert s["wait_days"] == 3 and s["row_version"] == 2
