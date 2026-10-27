"""Edge cases of the pure domain modules (app/domain), which the unit layer keeps fully covered."""
from datetime import date, timedelta
from uuid import UUID

import pytest

from app.domain.campaigns import copy_end_date, copy_start_date
from app.domain.contacts import as_list
from app.domain.inbox import OwnerScope, ReplyLabel
from app.domain.reports import OptionalWindow
from app.domain.sales import parse_entity_id

TODAY = date(2030, 6, 15)


def test_copy_start_date_rules():
    with pytest.raises(ValueError):
        copy_start_date(TODAY - timedelta(days=1), None, TODAY)
    assert copy_start_date(TODAY, None, TODAY) == TODAY
    assert copy_start_date(None, TODAY + timedelta(days=3), TODAY) == TODAY + timedelta(days=3)
    assert copy_start_date(None, TODAY - timedelta(days=3), TODAY) is None


def test_copy_end_date_rules():
    start = TODAY + timedelta(days=2)
    with pytest.raises(ValueError):
        copy_end_date(TODAY, None, start, TODAY)
    assert copy_end_date(start, None, start, TODAY) == start
    assert copy_end_date(None, start + timedelta(days=5), start, TODAY) == start + timedelta(days=5)
    assert copy_end_date(None, TODAY, start, TODAY) is None


def test_as_list():
    assert as_list([1, 2]) == [1, 2]
    assert as_list(None) == [] and as_list("") == []
    assert as_list("x") == ["x"]


def test_reply_label_parse_keeps_a_label_and_owner_scope():
    assert ReplyLabel.parse(ReplyLabel.POSITIVE) is ReplyLabel.POSITIVE
    assert OwnerScope().unrestricted is True
    assert OwnerScope(owner_ids=frozenset({"u1"})).unrestricted is False


def test_optional_window_end_exclusive():
    assert OptionalWindow(end=TODAY).end_exclusive == TODAY + timedelta(days=1)
    assert OptionalWindow().end_exclusive is None


def test_parse_entity_id():
    raw = "123E4567-E89B-12D3-A456-426614174000"
    assert parse_entity_id(raw) == UUID(raw)
    assert parse_entity_id("not-a-uuid") is None
    assert parse_entity_id("x" * 36) is None


def test_duplicate_overrides_enrols_only_with_contacts():
    from app.domain.campaigns import DuplicateOverrides

    assert DuplicateOverrides().enrols is False
    assert DuplicateOverrides(list_ids=("l1",)).enrols is True
