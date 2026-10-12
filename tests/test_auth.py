"""Authentication: suspended tenants, request-body logging, health endpoints."""

import logging

from tests.conftest import RUN, auth, register_tenant, sql


def test_suspended_tenant_blocked(client):
    t = register_tenant(client, "susp")
    assert client.get("/inboxes", headers=t["headers"]).status_code == 200
    sql("UPDATE tenants SET status = 'SUSPENDED' WHERE tenant_id = :t", t=t["tenant_id"])
    try:
        assert client.get("/inboxes", headers=t["headers"]).status_code == 403
        r = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]})
        assert r.status_code == 403
    finally:
        sql("UPDATE tenants SET status = 'ACTIVE' WHERE tenant_id = :t", t=t["tenant_id"])
    r = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]})
    assert r.status_code == 200


def test_wrong_password_rejected(client, tenant_a):
    r = client.post("/api/auth/login", json={"email": tenant_a["email"], "password": "wrong-password"})
    assert r.status_code == 401


def test_missing_or_bad_token_rejected(client):
    assert client.get("/inboxes").status_code in (401, 403)
    assert client.get("/inboxes", headers=auth("not-a-jwt")).status_code == 401


def test_validation_error_does_not_log_body(client, caplog):
    secret = f"not-a-string-SECRET-{RUN}"
    with caplog.at_level(logging.INFO):
        r = client.post("/api/auth/login", json={"email": "x@example.com", "password": [secret]})
    assert r.status_code == 422
    assert "422 validation error" in caplog.text  # the error is logged...
    assert secret not in caplog.text              # ...without the request body


def test_health_endpoints(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "healthy"
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "database": "ok"}
