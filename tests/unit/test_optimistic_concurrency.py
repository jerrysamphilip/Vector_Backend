"""Optimistic concurrency helpers (app/core/concurrency.py): If-Match parsing and the 409 payload."""
import pytest

from app.core.concurrency import (STALE_MESSAGE, PreconditionRequired, parse_if_match, resolve_expected,
                                  stale_error, version_required)
from app.core.errors import BadRequestError


@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), ("  ", None), ("*", None),
    ("7", 7), (' "7" ', 7), ('W/"12"', 12), ('w/"3"', 3),
])
def test_parse_if_match(value, expected):
    assert parse_if_match(value) == expected


@pytest.mark.parametrize("value", ["abc", '"1.5"', "-1", '"7", "8"'])
def test_parse_if_match_rejects_garbage(value):
    with pytest.raises(BadRequestError):
        parse_if_match(value)


def test_resolve_expected():
    assert resolve_expected(None, None) is None
    assert resolve_expected(3, None) == 3
    assert resolve_expected(None, 4) == 4
    assert resolve_expected(5, 5) == 5
    with pytest.raises(BadRequestError):
        resolve_expected(5, 6)


def test_stale_error_payload():
    err = stale_error(9)
    assert err.status_code == 409 and err.code == "STALE_VERSION"
    assert err.detail == {"code": "STALE_VERSION", "current_version": 9, "message": STALE_MESSAGE}


def test_version_required_flag(monkeypatch):
    monkeypatch.delenv("OCC_REQUIRE_VERSION", raising=False)
    assert not version_required()
    monkeypatch.setenv("OCC_REQUIRE_VERSION", "true")
    assert version_required()
    assert PreconditionRequired("x").status_code == 428
