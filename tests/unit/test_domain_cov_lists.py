"""Coverage of list name / filter / label rules (app/domain/lists.py)."""
import pytest

from app.domain.lists import ListRuleError, clean_name, require_conditions, require_name, user_label


def test_clean_name_none_blank_and_trimmed():
    assert clean_name(None) is None
    assert clean_name("   ") is None
    assert clean_name("  Leads ") == "Leads"
    assert clean_name(42) == "42"
    with pytest.raises(ListRuleError):
        require_name(None)


def test_require_conditions_accepts_a_group_with_conditions():
    assert require_conditions({"op": "AND", "conditions": [{"field": "email", "operator": "is_not_empty"}]}) is None
    for bad in (None, {}, ["x"], {"conditions": []}):
        with pytest.raises(ListRuleError):
            require_conditions(bad)


def test_user_label_joins_and_trims():
    assert user_label("Ada", "Lovelace") == "Ada Lovelace"
    assert user_label("Ada", "") == "Ada"
