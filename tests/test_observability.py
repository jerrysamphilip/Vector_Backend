"""Request IDs and the Prometheus endpoint."""

import logging


def test_request_id_generated_and_echoed(client):
    r = client.get("/health")
    generated = r.headers.get("x-request-id")
    assert generated and len(generated) >= 16
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"


def test_unsafe_request_id_replaced(client):
    r = client.get("/health", headers={"X-Request-ID": "bad id\twith spaces" + "x" * 200})
    assert r.headers["x-request-id"] != "bad id\twith spaces" + "x" * 200


def test_request_id_in_log_records(client, caplog):
    with caplog.at_level(logging.INFO):
        client.post("/api/auth/login", headers={"X-Request-ID": "trace-me-42"},
                    json={"email": "x@example.com", "password": ["x"]})
    from app.core.observability import RequestIdFilter
    f = RequestIdFilter()
    records = [r for r in caplog.records if "422 validation error" in r.getMessage()]
    assert records
    for rec in records:
        f.filter(rec)
    assert any(rec.request_id == "trace-me-42" for rec in records)


def test_metrics_endpoint_uses_route_templates(client, tenant_a):
    client.get("/inboxes/some-random-id-1", headers=tenant_a["headers"])
    client.get("/inboxes/some-random-id-2", headers=tenant_a["headers"])
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert "http_requests_total" in body
    assert 'route="/inboxes/{inbox_id}"' in body
    assert "some-random-id" not in body


def test_business_metric_helpers(client):
    from app.core.observability import (record_email_sent, record_imap_sync, record_job_heartbeat,
                                        record_ses_event)
    record_email_sent("sent")
    record_ses_event("Complaint")
    record_ses_event("SomethingNew")
    record_imap_sync("ok")
    record_job_heartbeat("test_job")
    body = client.get("/metrics").text
    assert 'emails_sent_total{result="sent"}' in body
    assert 'ses_webhook_events_total{type="Complaint"}' in body
    assert 'ses_webhook_events_total{type="other"}' in body
    assert 'imap_sync_runs_total{result="ok"}' in body
    assert 'job_last_success_timestamp{job="test_job"}' in body


def test_metrics_token(client, monkeypatch):
    from app.core import observability
    from app.core.config import settings
    monkeypatch.setattr(settings, "METRICS_TOKEN", "s3cret")
    # The token is read when the app is built; check the guard on a fresh app
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    mini = FastAPI()
    observability.install_observability(mini)
    c = TestClient(mini)
    assert c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_sentry_scrubber_strips_sensitive_data():
    from app.core.observability import _scrub_event
    event = {"request": {"data": {"password": "p"}, "cookies": {"s": "1"}, "query_string": "token=abc",
                         "headers": {"Authorization": "Bearer x", "Cookie": "s=1", "User-Agent": "ua"}},
             "user": {"email": "a@b.c"}}
    out = _scrub_event(event)
    assert "data" not in out["request"] and "cookies" not in out["request"]
    assert out["request"]["query_string"] == "[Filtered]"
    assert out["request"]["headers"]["Authorization"] == "[Filtered]"
    assert out["request"]["headers"]["Cookie"] == "[Filtered]"
    assert out["request"]["headers"]["User-Agent"] == "ua"
    assert "user" not in out
