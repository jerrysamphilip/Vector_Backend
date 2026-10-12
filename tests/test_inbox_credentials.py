"""Editing an inbox's SMTP / IMAP credentials (passwords never returned, encrypted at rest)."""

import pytest

from tests.conftest import RUN, sql


@pytest.fixture(scope="module")
def inbox(client, tenant_a):
    r = client.post("/inboxes", headers=tenant_a["headers"], json={
        "email_address": f"creds@creds{RUN}.example.com", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "u0", "smtp_password": "initial-smtp", "warmup_enabled": False,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert "smtp_password" not in body and "imap_password" not in body
    return body["inbox_id"]


def test_edit_credentials_and_flags(client, tenant_a, inbox):
    r = client.put(f"/inboxes/{inbox}", headers=tenant_a["headers"], json={
        "smtp_host": " 127.0.0.1 ", "smtp_port": 1, "smtp_username": "u1", "smtp_password": "secret-smtp",
        "imap_host": "127.0.0.1", "imap_port": 1993, "imap_username": "u2", "imap_password": "secret-imap",
        "provider": "SMTP",
    })
    assert r.status_code == 200, r.text
    i = r.json()
    assert i["smtp_host"] == "127.0.0.1"  # trimmed
    assert i["imap_port"] == 1993
    assert i.get("has_smtp_password") is True and i.get("has_imap_password") is True
    assert "smtp_password" not in i and "imap_password" not in i
    assert "secret-smtp" not in r.text and "secret-imap" not in r.text


def test_blank_password_keeps_saved_one(client, tenant_a, inbox):
    r = client.put(f"/inboxes/{inbox}", headers=tenant_a["headers"],
                   json={"smtp_password": "", "imap_password": "", "smtp_username": "u1b"})
    assert r.status_code == 200, r.text
    i = r.json()
    assert i["has_smtp_password"] and i["has_imap_password"]
    assert i["smtp_username"] == "u1b"


def test_password_encrypted_at_rest(client, tenant_a, inbox):
    client.put(f"/inboxes/{inbox}", headers=tenant_a["headers"], json={"smtp_password": "secret-smtp"})
    stored = sql("SELECT smtp_password FROM sending_inboxes WHERE inbox_id = :i", i=inbox)
    assert stored and "secret-smtp" not in stored
    from app.core.database import SessionLocal
    from app.models.sending_inbox import SendingInbox
    with SessionLocal() as db:
        assert db.get(SendingInbox, inbox).smtp_password == "secret-smtp"


@pytest.mark.parametrize("field,value", [("smtp_port", 70000), ("imap_port", 0), ("smtp_port", -1)])
def test_port_validation(client, tenant_a, inbox, field, value):
    r = client.put(f"/inboxes/{inbox}", headers=tenant_a["headers"], json={field: value})
    assert r.status_code == 422


def test_connection_tests_report_a_status(client, tenant_a, inbox):
    # Points at a closed local port: only the reported status value is checked (no real mail server)
    r = client.post(f"/inboxes/{inbox}/test-smtp", headers=tenant_a["headers"])
    assert r.status_code == 200
    assert r.json().get("status") in ("connection_failed", "auth_failed", "connected")
    r = client.get(f"/inboxes/{inbox}/test-imap", headers=tenant_a["headers"])
    assert r.status_code == 200
    assert r.json().get("status") in ("connection_failed", "auth_failed", "connected")
