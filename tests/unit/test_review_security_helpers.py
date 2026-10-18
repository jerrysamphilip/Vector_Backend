"""Custom-report filter validation (422, never 500) and Google account linking rules (I-12)."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import Column, Date, Integer, MetaData, String, Table

from app.core.config import settings
from app.routers import auth_router
from app.routers import custom_reports_router as cr

_t = Table("unit_report", MetaData(), Column("name", String(20)), Column("seats", Integer), Column("day", Date))
SPEC = {"fields": {"name": ("Name", _t.c.name), "seats": ("Seats", _t.c.seats), "day": ("Day", _t.c.day)}}
USER = SimpleNamespace(user_id="u1")


def _err(f):
    with pytest.raises(HTTPException) as exc:
        cr._filter_condition(SPEC, f, USER)
    return exc.value


@pytest.mark.parametrize("value", [{"$gt": 1}, None, [{"a": 1}]])
def test_dict_or_missing_values_are_422(value):
    e = _err({"field": "name", "operator": "is", "value": value})
    assert e.status_code == 422 and "Name" in e.detail


def test_non_numeric_value_on_numeric_column_is_422():
    e = _err({"field": "seats", "operator": "is", "value": "lots"})
    assert e.status_code == 422 and "not a number" in e.detail
    assert _err({"field": "seats", "operator": "is", "value": True}).status_code == 422
    cr._filter_condition(SPEC, {"field": "seats", "operator": "is", "value": "12"}, USER)


def test_bad_date_is_422_and_iso_date_passes():
    assert "not a date" in _err({"field": "day", "operator": "is", "value": "tomorrow"}).detail
    cr._filter_condition(SPEC, {"field": "day", "operator": "is_not", "value": "2026-10-18"}, USER)


def test_list_only_with_is_or_is_not():
    assert "list of values" in _err({"field": "name", "operator": "contains", "value": ["a"]}).detail
    cr._filter_condition(SPEC, {"field": "name", "operator": "is", "value": ["a", 1]}, USER)
    cr._filter_condition(SPEC, {"field": "name", "operator": "is_empty", "value": {"ignored": 1}}, USER)


def test_unknown_or_unhashable_field_is_400():
    assert _err({"field": {"x": 1}, "operator": "is", "value": "a"}).status_code == 400
    assert _err({"field": "name", "operator": ["is"], "value": "a"}).status_code == 400


def test_contains_escapes_wildcards():
    sql = str(cr._filter_condition(SPEC, {"field": "name", "operator": "contains", "value": "5%"}, USER)
              .compile(compile_kwargs={"literal_binds": True}))
    assert "5\\%" in sql and "ESCAPE" in sql


# ── Google linking ──

class _Audit:
    calls: list = []

    def __init__(self, db):
        pass

    def log_action(self, *args):
        _Audit.calls.append(args)


def _user(**kw):
    return SimpleNamespace(**{"user_id": "u1", "tenant_id": "t1", "google_id": None, "auth_provider": "local", **kw})


def test_link_refused_without_allowed_domain(monkeypatch):
    monkeypatch.setattr(settings, "SSO_LINK_ALLOWED_DOMAINS", "")
    with pytest.raises(HTTPException) as exc:
        auth_router._link_google_account(None, _user(), "g1", "pat@acme.example")
    assert exc.value.status_code == 409 and "password" in exc.value.detail


def test_link_allowed_for_listed_domain_is_audited(monkeypatch):
    monkeypatch.setattr(settings, "SSO_LINK_ALLOWED_DOMAINS", " Acme.example ,@other.example")
    monkeypatch.setattr(auth_router, "AuditService", _Audit)
    _Audit.calls = []
    user = _user()
    auth_router._link_google_account(None, user, "g1", "pat@ACME.example")
    assert (user.google_id, user.auth_provider) == ("g1", "google")
    assert _Audit.calls == [("t1", "u1", "LINK", "google_sso", "u1")]
    assert settings.sso_link_allowed_domains == frozenset({"acme.example", "other.example"})


def test_account_linked_to_another_google_identity_is_never_relinked(monkeypatch):
    monkeypatch.setattr(settings, "SSO_LINK_ALLOWED_DOMAINS", "acme.example")
    with pytest.raises(HTTPException):
        auth_router._link_google_account(None, _user(google_id="g-old"), "g-new", "pat@acme.example")
