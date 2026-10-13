"""Pure helpers of app/core/observability.py: metric labels, request-id validation, Sentry scrubbing."""
import pytest

from app.core import observability as ob


def test_label_defaults_trims_and_truncates():
    assert ob._label(None) == "unknown"
    assert ob._label("   ") == "unknown"
    assert ob._label("", default="n/a") == "n/a"
    assert ob._label("  sent ") == "sent"
    assert ob._label("x" * 100) == "x" * 40
    assert ob._label("abcdef", limit=3) == "abc"


@pytest.mark.parametrize("value", ["abc-123", "req_1.2:3", "A" * 128, "0"])
def test_request_id_accepted(value):
    assert ob._REQUEST_ID_OK.match(value)


@pytest.mark.parametrize("value", ["", "has space", "A" * 129, "semi;colon", "new\nline", "<script>"])
def test_request_id_rejected(value):
    assert not ob._REQUEST_ID_OK.match(value)


def test_scrub_event_removes_bodies_credentials_and_user():
    event = {"request": {"data": {"password": "p"}, "cookies": {"s": "1"}, "query_string": "token=abc",
                         "headers": {"Authorization": "Bearer x", "Cookie": "s=1", "X-Webhook-Token": "t",
                                     "Accept": "application/json"}},
             "user": {"email": "pat@x.example"}}
    out = ob._scrub_event(event)
    req = out["request"]
    assert "data" not in req and "cookies" not in req and "user" not in out
    assert req["query_string"] == "[Filtered]"
    assert req["headers"]["Authorization"] == req["headers"]["Cookie"] == req["headers"]["X-Webhook-Token"] == "[Filtered]"
    assert req["headers"]["Accept"] == "application/json"


def test_scrub_event_tags_the_request_id():
    token = ob._request_id.set("rid-123")
    try:
        assert ob._scrub_event({})["tags"] == {"request_id": "rid-123"}
        assert ob._scrub_event({"tags": {"request_id": "keep"}})["tags"]["request_id"] == "keep"
    finally:
        ob._request_id.reset(token)
    assert "tags" not in ob._scrub_event({})
    assert ob.get_request_id() == "-"
