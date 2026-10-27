"""SES webhook hardening (6): body cap, own JSON parsing, no internals in error responses (no database)."""
import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routers import ses_webhook_router as mod


def _request(body: bytes, *, content_length=True, chunk=64 * 1024, headers=None) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if content_length:
        raw_headers.append((b"content-length", str(len(body)).encode()))
    chunks = [body[i:i + chunk] for i in range(0, len(body), chunk)] or [b""]
    sent = {"n": 0}

    async def receive():
        i = sent["n"]
        sent["n"] += 1
        if i >= len(chunks):
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": chunks[i], "more_body": i < len(chunks) - 1}

    scope = {"type": "http", "method": "POST", "path": "/webhooks/ses/notifications", "headers": raw_headers,
             "query_string": b""}
    req = Request(scope, receive)
    _READS[id(req)] = sent  # how many chunks the handler pulled
    return req


_READS: dict = {}


def _read(req: Request):
    return asyncio.run(mod._read_json_body(req))


def test_small_json_body_is_parsed():
    assert _read(_request(b'{"a": 1}')) == {"a": 1}


def test_declared_length_over_cap_is_413_without_reading():
    req = _request(b"{}", headers={"content-length": str(mod.MAX_WEBHOOK_BODY_BYTES + 1)}, content_length=False)
    with pytest.raises(HTTPException) as exc:
        _read(req)
    assert exc.value.status_code == 413
    assert _READS[id(req)]["n"] == 0


def test_streamed_body_over_cap_is_413_even_without_content_length():
    body = b'{"x": "' + b"a" * (3 * mod.MAX_WEBHOOK_BODY_BYTES) + b'"}'
    req = _request(body, content_length=False)
    with pytest.raises(HTTPException) as exc:
        _read(req)
    assert exc.value.status_code == 413
    # Stopped once over the cap, not after buffering everything
    assert _READS[id(req)]["n"] <= mod.MAX_WEBHOOK_BODY_BYTES // (64 * 1024) + 1


def test_lying_content_length_is_still_capped():
    body = b"a" * (mod.MAX_WEBHOOK_BODY_BYTES + 1)
    req = _request(body, content_length=False, headers={"content-length": "10"})
    with pytest.raises(HTTPException) as exc:
        _read(req)
    assert exc.value.status_code == 413


def test_body_at_cap_is_accepted():
    payload = b'"' + b"a" * (mod.MAX_WEBHOOK_BODY_BYTES - 2) + b'"'
    assert len(payload) == mod.MAX_WEBHOOK_BODY_BYTES
    assert len(_read(_request(payload))) == mod.MAX_WEBHOOK_BODY_BYTES - 2


@pytest.mark.parametrize("body", [b"", b"not json", b"{", b"\xff\xfe"])
def test_invalid_json_is_400(body):
    with pytest.raises(HTTPException) as exc:
        _read(_request(body))
    assert exc.value.status_code == 400


def test_invalid_content_length_is_400():
    with pytest.raises(HTTPException) as exc:
        _read(_request(b"{}", content_length=False, headers={"content-length": "abc"}))
    assert exc.value.status_code == 400


def _raw_unauthenticated_locally(monkeypatch):
    monkeypatch.setattr(mod.settings, "SES_WEBHOOK_TOKEN", "", raising=False)
    monkeypatch.setattr(mod, "is_deployed", lambda: False)
    monkeypatch.setattr(type(mod.settings), "is_production", property(lambda self: False))


def test_processing_error_does_not_leak_details(monkeypatch, caplog):
    _raw_unauthenticated_locally(monkeypatch)
    secret = "pymysql.err.OperationalError: (1054, \"Unknown column 'x' in /srv/app/secret.py\")"

    async def boom(message, db):
        raise RuntimeError(secret)
    monkeypatch.setattr(mod, "_handle_delivery", boom)
    event = json.dumps({"notificationType": "Delivery", "mail": {}, "delivery": {}}).encode()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.handle_ses_notification(_request(event), db=MagicMock(), x_amz_sns_message_type=None))
    assert exc.value.status_code == 500
    assert secret not in str(exc.value.detail) and "1054" not in str(exc.value.detail)
    assert exc.value.detail == "Error processing notification"
    assert secret in caplog.text  # still in the server log


def test_subscription_confirmation_error_is_generic(monkeypatch):
    monkeypatch.setattr(mod, "verify_sns_signature", lambda body, arn: _true())
    monkeypatch.setattr(type(mod.settings), "is_production", property(lambda self: False))
    monkeypatch.setattr(mod, "is_sns_url", lambda url: True)

    class FailingClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, timeout=None):
            raise OSError("connect to 10.0.0.12:443 failed: internal-host.local")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FailingClient)
    body = json.dumps({"Type": "SubscriptionConfirmation", "SubscribeURL": "https://sns.example/x"}).encode()
    out = asyncio.run(mod.handle_ses_notification(_request(body), db=MagicMock(), x_amz_sns_message_type=None))
    assert out == {"status": "error", "message": "Subscription confirmation failed"}


async def _true():
    return True


def test_outcome_statuses_include_sending():
    assert set(mod._OUTCOME_STATUSES) == {"SENT", "SENDING"}
