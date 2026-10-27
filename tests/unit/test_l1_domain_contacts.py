"""
L1 domain layer (ADR 0009): the contacts query objects and pure rules in app/domain/contacts.py.
Plain values and functions: no fakes, no database.
"""
from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from app.domain import contacts as d
from app.domain.paging import SortDirection


def parse(spec, me="u-me", revenue=True):
    return d.parse_filter_tree(spec, me, lambda: revenue)


# ── Sorting, filters, visibility ───────────────────────────────────

def test_contact_sort_allowed_fields():
    s = d.contact_sort("score", "asc")
    assert s.field == "score" and s.direction is SortDirection.ASC and not s.descending
    assert d.contact_sort("created_at", "desc").descending
    with pytest.raises(ValueError):
        d.contact_sort("password_hash", "asc")
    with pytest.raises(ValueError):
        d.contact_sort("name", "sideways")
    assert d.CONTACT_SORT_FIELDS == {"name", "email", "company", "lifecycle_stage", "lead_status",
                                     "created_at", "updated_at", "score"}


def test_contact_filter_is_frozen_and_knows_when_searching():
    f = d.ContactFilter(q="  ")
    assert not f.searching and d.ContactFilter(q=" ann ").searching and not d.ContactFilter().searching
    with pytest.raises(FrozenInstanceError):
        f.q = "x"  # type: ignore[misc]


def test_visibility_scope_allows():
    everyone = d.VisibilityScope()
    assert everyone.everyone and everyone.allows(None) and everyone.allows("x")
    team = d.VisibilityScope(frozenset({"a", "b"}))
    assert team.allows("a") and not team.allows("c") and not team.allows(None)
    assert d.VisibilityScope(frozenset({"a"}), include_unassigned=True).allows(None)


def test_team_ids_walks_the_hierarchy():
    pairs = [("ceo", None), ("vp", "ceo"), ("rep1", "vp"), ("rep2", "vp"), ("other", "boss"), ("loop", "loop")]
    assert d.team_ids(pairs, "vp") == {"vp", "rep1", "rep2"}
    assert d.team_ids(pairs, "ceo") == {"ceo", "vp", "rep1", "rep2"}
    assert d.team_ids(pairs, "rep1") == {"rep1"}
    assert d.team_ids(pairs, "loop") == {"loop"}


@pytest.mark.parametrize("owner,expected", [
    ("me", ("u-me", False)), ("unassigned", (None, True)), ("team", (None, False)),
    ("u-2", ("u-2", False)), (None, (None, False)), ("", (None, False)),
])
def test_owner_criteria(owner, expected):
    assert d.owner_criteria(owner, "u-me") == expected


def test_search_terms_and_grades():
    assert d.search_terms(" Ann Lee ") == ("ann lee",)
    assert d.search_terms("+1 (737) 555-0188") == ("+1 (737) 555-0188", "17375550188")
    assert d.search_terms("zorblax66034") == ("zorblax66034",)
    assert d.search_terms("7375550188") == ("7375550188",)
    assert d.parse_grades("a, B,x") == ["A", "B"] and d.parse_grades("z") == []
    assert d.phone_digits("(737) 555") == "737555" and d.phone_digits(None) == ""


# ── AND / OR filter tree ───────────────────────────────────────────

def test_empty_specs_mean_everything():
    assert parse(None) is None and parse({}) is None
    assert parse({"op": "AND", "conditions": []}) is None
    assert parse({"op": "AND", "conditions": [{"op": "OR", "conditions": []}]}) is None


def test_tree_shape_and_normalised_values():
    tree = parse({"op": "or", "conditions": [
        {"field": "owner_id", "operator": "is", "value": "me"},
        {"op": "AND", "conditions": [
            {"field": "created_at", "operator": "after", "value": "2026-01-02T03:04:05Z"},
            {"field": "updated_at", "operator": "in_last_days", "value": "7"},
            {"field": "is_valid_email", "operator": "is", "value": "Yes"},
        ]},
    ]})
    assert isinstance(tree, d.FilterGroup) and tree.op == "OR"
    owner, inner = tree.children
    assert owner == d.FilterCondition("contact", "owner_id", "is", "u-me")
    assert isinstance(inner, d.FilterGroup) and inner.op == "AND"
    after, days, valid = inner.children
    assert after.value == datetime(2026, 1, 2, 3, 4, 5) and after.is_text is False
    assert days.value == 7
    assert valid.value is True


def test_condition_kinds():
    assert parse({"field": "tags", "operator": "in", "value": ["vip"]}).kind == "tags"
    assert parse({"field": "list", "operator": "is_not", "value": "l1"}).kind == "list"
    company = parse({"field": "company.annual_revenue", "operator": "gt", "value": 5})
    assert (company.kind, company.name, company.is_text) == ("company", "annual_revenue", False)
    text = parse({"field": "custom.size", "operator": "is", "value": "Big"})
    assert (text.kind, text.name, text.numeric) == ("custom", "size", False)
    assert parse({"field": "custom.seats", "operator": "gte", "value": 10}).numeric
    assert not parse({"field": "custom.renewal", "operator": "lt", "value": "2026-05-01"}).numeric
    # "after" on a text property compares as text, not as a date
    assert parse({"field": "first_name", "operator": "after", "value": "M"}).value == "M"


@pytest.mark.parametrize("spec,message", [
    ({"field": "email", "operator": "matches", "value": "x"}, "Unknown operator: matches"),
    ({"field": "password", "operator": "is", "value": "x"}, "Unknown property: password"),
    ({"operator": "is", "value": "x"}, "Unknown property: None"),
    ({"field": "custom.Bad-Key", "operator": "is", "value": "x"}, "Invalid custom field: Bad-Key"),
    ({"field": "company.secret", "operator": "is", "value": "x"}, "Unknown company property: company.secret"),
    ({"field": "tags", "operator": "starts_with", "value": "v"}, "Tags support is / is_not / is_empty / is_not_empty"),
    ({"field": "created_at", "operator": "before", "value": "soon"}, "Invalid date: soon"),
])
def test_filter_errors(spec, message):
    with pytest.raises(d.FilterError) as exc:
        parse(spec)
    assert str(exc.value) == message


def test_revenue_needs_the_amounts_right_and_is_asked_lazily():
    asked = []

    def allowed():
        asked.append(1)
        return False
    d.parse_filter_tree({"field": "email", "operator": "is", "value": "x"}, "me", allowed)
    assert asked == []
    with pytest.raises(d.FilterError, match="Your role cannot filter on revenue"):
        d.parse_filter_tree({"field": "company.annual_revenue", "operator": "gt", "value": 1}, "me", allowed)


def test_depth_and_condition_limits():
    deep = {"field": "email", "operator": "is", "value": "x"}
    for _ in range(d.MAX_FILTER_DEPTH + 1):
        deep = {"op": "AND", "conditions": [deep]}
    with pytest.raises(d.FilterError, match="nested too deeply"):
        parse(deep)
    many = {"op": "OR", "conditions": [{"field": "email", "operator": "is", "value": str(i)}
                                       for i in range(d.MAX_CONDITIONS + 1)]}
    with pytest.raises(d.FilterError, match=f"At most {d.MAX_CONDITIONS} conditions"):
        parse(many)


def test_errors_follow_condition_order():
    """The first bad condition is the one reported (the engine always checked them in order)."""
    spec = {"op": "AND", "conditions": [{"field": "nope", "operator": "is", "value": 1},
                                        {"field": "email", "operator": "bogus", "value": 1}]}
    with pytest.raises(d.FilterError, match="Unknown property: nope"):
        parse(spec)


def test_in_last_days_needs_a_number():
    with pytest.raises(ValueError):
        parse({"field": "created_at", "operator": "in_last_days", "value": "week"})


def test_parse_filter_param():
    assert d.parse_filter_param(None) is None and d.parse_filter_param("") is None
    assert d.parse_filter_param('{"op": "AND", "conditions": []}') == {"op": "AND", "conditions": []}
    with pytest.raises(d.FilterError, match="filters must be JSON"):
        d.parse_filter_param("{nope")
    with pytest.raises(d.FilterError, match="filters must be an object"):
        d.parse_filter_param("[1]")


def test_filter_fields_match_the_repository_columns():
    from app.repositories.contacts import COMPANY_COLUMNS, CONTACT_COLUMNS
    assert tuple(CONTACT_COLUMNS) == d.CONTACT_FILTER_FIELDS
    assert tuple(COMPANY_COLUMNS) == d.COMPANY_FILTER_FIELDS
