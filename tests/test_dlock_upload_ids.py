"""D2 (live regression ev6, API sweep): the upload id in the path of
POST /uploads/{upload_id}/confirmations and /revalidations is the source of truth.

The confirmation used to ignore the path and save under ``payload.upload_id``; the revalidation ignored it
entirely. Now: a body ``upload_id`` that differs from the path is a 400; a path id that is not an upload id
(not a UUID) or that names another tenant's upload is a 404; the response shape is unchanged.
"""
import uuid

import pytest

from tests.conftest import RUN, register_tenant, sql


@pytest.fixture(scope="module")
def ws_a(client):
    return register_tenant(client, "dlua")


@pytest.fixture(scope="module")
def ws_b(client):
    return register_tenant(client, "dlub")


def _row(n=1):
    return {"row": n, "email": f"up{n}.{uuid.uuid4().hex[:8]}@dlockup{RUN}.example.com", "first_name": f"Up{n}",
            "last_name": "Loader", "company_name": f"Upload Co {RUN}", "status": "ACCEPTED"}


def _confirm(client, ws, path_id, body_id=None, records=None):
    return client.post(f"/uploads/{path_id}/confirmations", headers=ws["headers"], json={
        "upload_id": path_id if body_id is None else body_id, "title": f"Upload {uuid.uuid4().hex[:6]}",
        "records": records if records is not None else [_row()]})


def _revalidate(client, ws, path_id, records=None):
    return client.post(f"/uploads/{path_id}/revalidations", headers=ws["headers"],
                       json={"records": records if records is not None else [_row()]})


def _lists(list_id):
    return sql("SELECT COUNT(*) FROM prospect_lists WHERE list_id = :l", l=list_id)


def _contacts_with(tenant_id, email):
    return sql("SELECT COUNT(*) FROM prospects WHERE tenant_id = :t AND email = :e", t=tenant_id, e=email)


def test_confirm_happy_path_saves_under_the_path_id(client, ws_a):
    upload_id = str(uuid.uuid4())
    r = _confirm(client, ws_a, upload_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["list_id"] == upload_id and body["status"] == "CONFIRMED" and body["inserted"] == 1
    assert set(body) >= {"list_id", "inserted", "new_prospects", "updated", "skipped_unsubscribed", "rejected",
                         "failed", "not_added", "status"}
    assert _lists(upload_id) == 1
    again = _confirm(client, ws_a, upload_id)
    assert again.status_code == 200 and again.json()["already_confirmed"] is True


def test_confirm_with_a_different_body_id_is_rejected(client, ws_a):
    path_id, body_id = str(uuid.uuid4()), str(uuid.uuid4())
    row = _row()
    r = _confirm(client, ws_a, path_id, body_id=body_id, records=[row])
    assert r.status_code == 400, r.text
    assert "upload_id" in r.text
    assert _lists(path_id) == 0 and _lists(body_id) == 0
    assert _contacts_with(ws_a["tenant_id"], row["email"]) == 0


def test_confirm_with_an_unknown_path_id_is_404(client, ws_a):
    row = _row()
    r = _confirm(client, ws_a, "not-an-upload-id", records=[row])
    assert r.status_code == 404, r.text
    assert _contacts_with(ws_a["tenant_id"], row["email"]) == 0


def test_confirm_with_another_tenants_upload_id_is_404(client, ws_a, ws_b):
    theirs = str(uuid.uuid4())
    assert _confirm(client, ws_b, theirs).status_code == 200
    members = sql("SELECT COUNT(*) FROM prospect_list_members WHERE list_id = :l", l=theirs)
    row = _row()
    r = _confirm(client, ws_a, theirs, records=[row])
    assert r.status_code == 404, r.text
    assert sql("SELECT tenant_id FROM prospect_lists WHERE list_id = :l", l=theirs) == ws_b["tenant_id"]
    assert sql("SELECT COUNT(*) FROM prospect_list_members WHERE list_id = :l", l=theirs) == members
    assert _contacts_with(ws_a["tenant_id"], row["email"]) == 0
    # ... and with tenant B's id only in the body, against an id of one's own in the path
    r = _confirm(client, ws_a, str(uuid.uuid4()), body_id=theirs, records=[row])
    assert r.status_code == 400, r.text


def test_revalidate_checks_the_path_id(client, ws_a, ws_b):
    ok = _revalidate(client, ws_a, str(uuid.uuid4()))
    assert ok.status_code == 200, ok.text
    assert ok.json()["records"][0]["status"] == "ACCEPTED"
    assert _revalidate(client, ws_a, "not-an-upload-id").status_code == 404
    theirs = str(uuid.uuid4())
    assert _confirm(client, ws_b, theirs).status_code == 200
    r = _revalidate(client, ws_a, theirs)
    assert r.status_code == 404, r.text
    # One's own confirmed upload can still be re-validated
    assert _revalidate(client, ws_b, theirs).status_code == 200
