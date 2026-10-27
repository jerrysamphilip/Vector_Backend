"""Unit tests (no database, no network) for outbound webhooks (BR-SF-20 / RP-106b).

app/utils/webhooks.py: event catalogue, HMAC signature, backoff schedule, payload cap, URL / SSRF
validation; app/services/webhooks.py: one HTTP attempt (_send, with the socket call mocked) and how
an attempt's result is applied to a delivery and its endpoint (record_result).
"""
import hashlib
import hmac
import json
import socket
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.clock import utcnow
from app.utils import webhooks as wh


def _no_dns(host, port):
    raise AssertionError(f"unexpected DNS lookup of {host}")


# ── WH-U01..04 event catalogue ───────────────────────────────

def test_wh_u01_catalogue_has_the_wired_events_only():
    assert set(wh.EVENT_TYPES) == {
        "contact.created", "contact.updated", "contact.deleted", "lead.created", "lead.qualified",
        "deal.created", "deal.stage_changed", "deal.won", "deal.lost",
        "email.replied", "email.bounced", "email.unsubscribed", "campaign.paused"}
    assert wh.TEST_EVENT not in wh.EVENT_TYPES
    for label, description in wh.EVENT_TYPES.values():
        assert label and description


def test_wh_u02_clean_event_types_dedupes_and_orders_like_the_catalogue():
    assert wh.clean_event_types(["deal.won", "contact.created", "deal.won"]) == ["contact.created", "deal.won"]
    assert wh.clean_event_types([]) == []


@pytest.mark.parametrize("bad", [["contact.exploded"], ["contact.created", "x" * 500], [wh.TEST_EVENT], [None]])
def test_wh_u03_clean_event_types_rejects_unknown(bad):
    with pytest.raises(ValueError) as exc:
        wh.clean_event_types(bad)
    assert len(str(exc.value)) < 400  # echoed values are clipped


def test_wh_u04_secret_generation_and_hint():
    a, b = wh.new_secret(), wh.new_secret()
    assert a.startswith("whsec_") and len(a) >= 40 and a != b
    assert wh.secret_hint(a).endswith(a[-4:]) and a[6:-4] not in wh.secret_hint(a)
    assert wh.secret_hint(None) == "" and wh.secret_hint("short") == ""


# ── WH-U05..12 signature ─────────────────────────────────────

def test_wh_u05_signature_is_hmac_sha256_over_timestamp_dot_body():
    body = b'{"id":"e1"}'
    expected = hmac.new(b"whsec_k", b"1700000000." + body, hashlib.sha256).hexdigest()
    assert wh.compute_signature("whsec_k", 1700000000, body) == expected
    assert wh.sign("whsec_k", body, timestamp=1700000000) == f"t=1700000000,v1={expected}"


def test_wh_u06_verify_accepts_a_fresh_valid_signature():
    header = wh.sign("s3cret", b"payload", timestamp=1000)
    assert wh.verify("s3cret", header, b"payload", now=1000 + 299)


@pytest.mark.parametrize("secret, body, now", [
    ("other", b"payload", 1000),        # wrong secret
    ("s3cret", b"payload!", 1000),      # tampered body
    ("s3cret", b"payload", 1000 + 301),  # too old (replay)
    ("s3cret", b"payload", 1000 - 301),  # too far in the future
])
def test_wh_u07_verify_rejects_wrong_secret_body_or_stale_timestamp(secret, body, now):
    header = wh.sign("s3cret", b"payload", timestamp=1000)
    assert not wh.verify(secret, header, body, now=now)


@pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=00", "v1=deadbeef", "t=1000", "t=1000,v1="])
def test_wh_u08_verify_rejects_malformed_headers(header):
    assert not wh.verify("s3cret", header, b"payload", now=1000)


def test_wh_u09_verify_accepts_any_of_several_v1_signatures():
    good = wh.compute_signature("s3cret", 1000, b"x")
    assert wh.verify("s3cret", f"t=1000,v1={'0' * 64},v1={good}", b"x", now=1000)


def test_wh_u10_parse_signature():
    assert wh.parse_signature("t=12, v1=ab ,v1=cd,v0=zz") == (12, ["ab", "cd"])


def test_wh_u11_signature_uses_current_time_by_default(monkeypatch):
    monkeypatch.setattr(wh.time, "time", lambda: 4242.9)
    assert wh.sign("k", b"b").startswith("t=4242,v1=")


def test_wh_u12_tolerance_zero_disables_the_age_check():
    header = wh.sign("k", b"b", timestamp=1)
    assert wh.verify("k", header, b"b", tolerance=0, now=10 ** 9)


# ── WH-U13..16 retries ───────────────────────────────────────

def test_wh_u13_backoff_schedule_six_attempts():
    assert wh.MAX_ATTEMPTS == 6
    assert [wh.next_delay(n) for n in range(1, 7)] == [60, 300, 1800, 7200, 21600, None]
    assert wh.next_delay(0) is None and wh.next_delay(99) is None


def test_wh_u14_backoff_grows():
    delays = [wh.next_delay(n) for n in range(1, wh.MAX_ATTEMPTS)]
    assert delays == sorted(delays) and len(set(delays)) == len(delays)


@pytest.mark.parametrize("code, ok", [(200, True), (201, True), (204, True), (299, True), (301, False),
                                      (302, False), (400, False), (410, False), (500, False), (None, False)])
def test_wh_u15_only_2xx_counts_as_delivered(code, ok):
    assert wh.is_success(code) is ok


def test_wh_u16_should_disable_threshold():
    assert not wh.should_disable(wh.DISABLE_AFTER_FAILURES - 1)
    assert wh.should_disable(wh.DISABLE_AFTER_FAILURES)
    assert wh.should_disable(3, threshold=3)


# ── WH-U17..20 payload ───────────────────────────────────────

def test_wh_u17_envelope_fields_and_serialisation():
    body = wh.build_body("e1", "deal.won", datetime(2026, 10, 13, 9, 30, 15, 123), "t1",
                         {"amount": Decimal("1200.50"), "close_date": datetime(2026, 10, 1).date()})
    doc = json.loads(body)
    assert doc == {"id": "e1", "type": "deal.won", "created_at": "2026-10-13T09:30:15Z", "workspace_id": "t1",
                   "data": {"amount": "1200.50", "close_date": "2026-10-01"}}


def test_wh_u18_body_is_compact_utf8():
    body = wh.build_body("e1", "contact.created", datetime(2026, 1, 1), "t", {"first_name": "Zoë"})
    assert "Zoë".encode() in body and b": " not in body


def test_wh_u19_oversized_payload_keeps_ids_and_flags_truncation():
    data = {"contact_id": "c1", "account_id": "a1", "notes": "x" * (wh.MAX_PAYLOAD_BYTES + 10), "n_id": {"x": 1}}
    body = wh.build_body("e1", "contact.updated", datetime(2026, 1, 1), "t", data)
    assert len(body) <= wh.MAX_PAYLOAD_BYTES
    assert json.loads(body)["data"] == {"contact_id": "c1", "account_id": "a1", "truncated": True}


def test_wh_u20_custom_cap():
    body = wh.build_body("e1", "contact.updated", datetime(2026, 1, 1), "t", {"x": "y" * 500}, max_bytes=300)
    assert json.loads(body)["data"] == {"truncated": True}


# ── WH-U21..33 URL / SSRF validation ─────────────────────────

def _resolver(*addresses):
    return lambda host, port: list(addresses)


def test_wh_u21_https_public_host_is_accepted_and_pinned():
    t = wh.validate_url("https://Hooks.Example.com/in?x=1", resolver=_resolver("93.184.216.34", "2606:2800::1"))
    assert (t.scheme, t.host, t.port, t.path, t.ip) == ("https", "hooks.example.com", 443, "/in?x=1", "93.184.216.34")


@pytest.mark.parametrize("url, message", [
    ("http://example.com/x", "https://"),
    ("ftp://example.com/x", "https://"),
    ("javascript:alert(1)", "https://"),
    ("https://user:pw@example.com/", "user name or password"),
    ("https://user@example.com/", "user name or password"),
    ("https:///nohost", "no host"),
    ("https://example.com:0/", "port"),
    ("https://example.com:99999/", "not valid"),
    ("https://exa mple.com/", "spaces"),
    ("https://example.com/\nX-Injected: 1", "spaces"),
    ("https://localhost/x", "public host"),
    ("https://api.localhost/x", "public host"),
    ("https://db.internal/x", "public host"),
    ("https://printer.local/x", "public host"),
    ("https://metadata.google.internal/computeMetadata/v1/", "not allowed"),
    ("", "Enter the endpoint URL"),
    ("https://example.com/" + "a" * wh.MAX_URL_LENGTH, "longer than"),
])
def test_wh_u22_syntax_rules_in_production_mode(url, message):
    with pytest.raises(wh.WebhookUrlError) as exc:
        wh.validate_url(url, allow_insecure=False, resolver=_resolver("93.184.216.34"))
    assert message in str(exc.value)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.18.0.1", "192.168.1.10", "169.254.169.254",
                                "100.64.0.1", "0.0.0.0", "::1", "fc00::1", "fe80::1", "::ffff:10.0.0.1",
                                "224.0.0.1", "255.255.255.255", "fd00:ec2::254"])
def test_wh_u23_internal_ip_literals_blocked_in_production_mode(ip):
    host = f"[{ip}]" if ":" in ip else ip
    with pytest.raises(wh.WebhookUrlError):
        wh.validate_url(f"https://{host}/hook", resolver=_no_dns)


@pytest.mark.parametrize("addresses", [["10.0.0.5"], ["93.184.216.34", "127.0.0.1"], ["169.254.169.254"]])
def test_wh_u24_host_resolving_to_any_internal_address_is_blocked(addresses):
    with pytest.raises(wh.WebhookUrlError) as exc:
        wh.validate_url("https://rebind.example.com/", resolver=_resolver(*addresses))
    assert "rebind.example.com" in str(exc.value)


def test_wh_u25_unresolvable_host_is_rejected():
    def fail(host, port):
        raise wh.WebhookUrlError(f"Could not resolve {host}")
    with pytest.raises(wh.WebhookUrlError, match="resolve"):
        wh.validate_url("https://nope.example.com/", resolver=fail)
    with pytest.raises(wh.WebhookUrlError, match="resolve"):
        wh.validate_url("https://empty.example.com/", resolver=_resolver())


def test_wh_u26_socket_resolver_wraps_dns_errors(monkeypatch):
    def gai(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(wh.socket, "getaddrinfo", gai)
    with pytest.raises(wh.WebhookUrlError, match="Could not resolve"):
        wh.socket_resolver("nope.example.com", 443)


def test_wh_u27_socket_resolver_dedupes(monkeypatch):
    monkeypatch.setattr(wh.socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("93.184.216.34", 443)), (10, 1, 6, "", ("2606::1", 443, 0, 0))])
    assert wh.socket_resolver("example.com", 443) == ["93.184.216.34", "2606::1"]


def test_wh_u28_development_mode_allows_http_and_private_receivers():
    t = wh.validate_url("http://172.18.0.1:9100/hook", allow_insecure=True, resolver=_no_dns)
    assert (t.scheme, t.port, t.ip) == ("http", 9100, "172.18.0.1")
    t = wh.validate_url("http://localhost:8080/x", allow_insecure=True, resolver=_resolver("127.0.0.1"))
    assert t.ip == "127.0.0.1"


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/",
                                 "http://0.0.0.0:8000/", "http://224.0.0.1/", "http://metadata/x",
                                 "http://100.100.100.200/", "http://[::ffff:169.254.169.254]/"])
def test_wh_u29_metadata_link_local_multicast_blocked_even_in_development(url):
    with pytest.raises(wh.WebhookUrlError):
        wh.validate_url(url, allow_insecure=True, resolver=_resolver("169.254.169.254"))


def test_wh_u30_development_mode_still_rejects_other_schemes_and_credentials():
    for url in ("ftp://172.18.0.1/", "file:///etc/passwd", "gopher://x/", "http://a:b@172.18.0.1/"):
        with pytest.raises(wh.WebhookUrlError):
            wh.validate_url(url, allow_insecure=True, resolver=_no_dns)


@pytest.mark.parametrize("address, reason_part", [
    ("8.8.8.8", None), ("2606:4700::1111", None), ("169.254.169.254", "metadata"),
    ("168.63.129.16", "metadata"), ("10.0.0.1", "private"), ("not-an-ip", "valid"),
])
def test_wh_u31_ip_block_reason(address, reason_part):
    reason = wh.ip_block_reason(address)
    assert (reason is None) if reason_part is None else (reason_part in reason)


def test_wh_u32_parse_url_does_not_resolve_and_defaults_path_and_port():
    t = wh.parse_url("https://example.com")
    assert (t.path, t.port, t.ip) == ("/", 443, None)
    assert wh.parse_url("http://example.com", allow_insecure=True).port == 80


def test_wh_u33_validate_without_resolver_checks_syntax_only():
    assert wh.validate_url("https://example.com/x", resolver=None).ip is None


# ── WH-U34..37 one HTTP attempt (service, socket call mocked) ─

@pytest.fixture
def svc(monkeypatch):
    from app.services import webhooks as service
    monkeypatch.setattr(service, "allow_insecure", lambda: False)
    return service


def _job(**over):
    job = {"delivery_id": "d1", "url": "https://93.184.216.34/hook", "secret": "whsec_test",
           "payload": '{"id":"e1","type":"contact.created"}', "event_type": "contact.created", "event_id": "e1"}
    job.update(over)
    return job


def test_wh_u34_send_signs_and_reports_success(svc, monkeypatch):
    seen = {}

    def fake_post(target, body, headers, timeout=10):
        seen.update(target=target, body=body, headers=headers, timeout=timeout)
        return 204, ""
    monkeypatch.setattr(svc, "post", fake_post)
    result = svc._send(_job())
    assert result["code"] == 204 and result["error"] is None and result["duration_ms"] >= 0
    h = seen["headers"]
    assert seen["target"].ip == "93.184.216.34" and seen["timeout"] == wh.REQUEST_TIMEOUT_SECONDS
    assert h["Content-Type"] == "application/json" and h["X-Outreach-Event"] == "contact.created"
    assert h["X-Outreach-Event-Id"] == "e1" and h["X-Outreach-Delivery"] == "d1"
    assert wh.verify("whsec_test", h[wh.SIGNATURE_HEADER], seen["body"])
    assert seen["body"] == _job()["payload"].encode()


def test_wh_u35_send_non_2xx_and_redirects_are_failures(svc, monkeypatch):
    monkeypatch.setattr(svc, "post", lambda *a, **k: (302, "moved"))
    r = svc._send(_job())
    assert r["code"] == 302 and r["error"].startswith("HTTP 302")
    monkeypatch.setattr(svc, "post", lambda *a, **k: (500, "x" * 2000))
    assert len(svc._send(_job())["error"]) < 400


def test_wh_u36_send_blocks_internal_targets_without_connecting(svc, monkeypatch):
    def must_not_post(*a, **k):
        raise AssertionError("connected to a blocked address")
    monkeypatch.setattr(svc, "post", must_not_post)
    for url in ("https://127.0.0.1/x", "http://93.184.216.34/x", "https://169.254.169.254/latest"):
        r = svc._send(_job(url=url))
        assert r["code"] is None and r["error"].startswith("Blocked:")


@pytest.mark.parametrize("exc, text", [(socket.timeout("t"), "Timed out"), (ConnectionRefusedError("refused"), "Connection failed")])
def test_wh_u37_send_network_errors_are_reported(svc, monkeypatch, exc, text):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(svc, "post", boom)
    r = svc._send(_job())
    assert r["code"] is None and text in r["error"]


# ── WH-U38..42 applying a result (record_result) ─────────────

def _delivery(**over):
    d = SimpleNamespace(attempts=0, last_attempt_at=None, response_code=None, duration_ms=None, status="PENDING",
                        delivered_at=None, last_error=None, next_attempt_at=None, is_test=False)
    d.__dict__.update(over)
    return d


def _endpoint(**over):
    ep = SimpleNamespace(consecutive_failures=0, last_success_at=None, last_failure_at=None, active=True,
                         endpoint_id="ep1", tenant_id="t1", url="https://x.example.com")
    ep.__dict__.update(over)
    return ep


def test_wh_u38_success_marks_delivered_and_resets_failures(svc):
    d, ep = _delivery(attempts=2, last_error="HTTP 500"), _endpoint(consecutive_failures=7)
    svc.record_result(None, d, ep, {"code": 200, "error": None, "duration_ms": 12})
    assert (d.status, d.attempts, d.response_code, d.last_error, d.next_attempt_at) == ("SUCCEEDED", 3, 200, None, None)
    assert d.delivered_at and ep.consecutive_failures == 0 and ep.last_success_at


def test_wh_u39_failure_schedules_the_next_attempt_with_backoff(svc):
    d, ep = _delivery(), _endpoint()
    before = utcnow()
    svc.record_result(None, d, ep, {"code": 503, "error": "HTTP 503", "duration_ms": 5})
    assert d.status == "PENDING" and d.attempts == 1 and d.last_error == "HTTP 503"
    assert before + timedelta(seconds=59) <= d.next_attempt_at <= utcnow() + timedelta(seconds=61)
    assert ep.consecutive_failures == 1 and ep.last_failure_at


def test_wh_u40_last_attempt_fails_for_good(svc):
    d = _delivery(attempts=wh.MAX_ATTEMPTS - 1)
    svc.record_result(None, d, _endpoint(), {"code": None, "error": "Timed out", "duration_ms": 10000})
    assert d.status == "FAILED" and d.attempts == wh.MAX_ATTEMPTS and d.next_attempt_at is None


def test_wh_u41_test_delivery_is_one_attempt_and_leaves_the_endpoint_alone(svc):
    d, ep = _delivery(is_test=True), _endpoint(consecutive_failures=3)
    svc.record_result(None, d, ep, {"code": 500, "error": "HTTP 500", "duration_ms": 1})
    assert d.status == "FAILED" and ep.consecutive_failures == 3
    d2 = _delivery(is_test=True)
    svc.record_result(None, d2, ep, {"code": 200, "error": None, "duration_ms": 1})
    assert d2.status == "SUCCEEDED" and ep.consecutive_failures == 3 and ep.last_success_at is None


def test_wh_u42_threshold_failure_disables_the_endpoint(svc, monkeypatch):
    calls = []
    monkeypatch.setattr(svc, "disable_endpoint", lambda db, ep, reason, notify_admins: calls.append((reason, notify_admins)))
    ep = _endpoint(consecutive_failures=wh.DISABLE_AFTER_FAILURES - 1)
    svc.record_result(None, _delivery(), ep, {"code": 500, "error": "HTTP 500", "duration_ms": 1})
    assert len(calls) == 1 and calls[0][1] is True and str(wh.DISABLE_AFTER_FAILURES) in calls[0][0]
    ep2 = _endpoint(consecutive_failures=wh.DISABLE_AFTER_FAILURES + 5, active=False)
    svc.record_result(None, _delivery(), ep2, {"code": 500, "error": "HTTP 500", "duration_ms": 1})
    assert len(calls) == 1  # already off: not disabled / notified again


# ── WH-U43..46 service guards that never reach the database ──

def test_wh_u43_allow_insecure_only_outside_production(monkeypatch):
    from app.core.config import settings
    from app.services import webhooks as service
    for env, expected in (("development", True), ("test", True), ("local", True), ("production", False),
                          ("staging", False), ("", False)):
        monkeypatch.setattr(settings, "ENVIRONMENT", env)
        assert service.allow_insecure() is expected


def test_wh_u44_emit_event_rejects_unknown_types():
    from app.services import webhooks as service
    with pytest.raises(ValueError):
        service.emit_event(None, "t1", "contact.exploded", {})


def test_wh_u45_imports_and_unknown_objects_raise_no_events():
    from app.services import webhooks as service
    # db=None: these return before any lookup
    service.on_record_changes(None, "t1", "CONTACT", "c1", {"a": 1}, {"a": 2}, "IMPORT")
    service.on_record_changes(None, "t1", "COMPANY", "c1", {"a": 1}, {"a": 2}, "UI")


def test_wh_u46_campaign_states_other_than_paused_raise_no_event():
    from app.services import webhooks as service
    for frm, to in (("DRAFT", "ACTIVE"), ("PAUSED", "ACTIVE"), ("ACTIVE", "COMPLETED"), ("PAUSED", "PAUSED")):
        service.campaign_state_changed(None, "c1", frm, to)
