"""A1 (ADR 0009): pure company rules in app/domain/accounts.py -- sort, owner criteria, the company filter tree
(C-16), field propagation (C-20) and the backfill planner (C-14). No fakes, no database."""
import pytest

from app.domain.accounts import (
    BackfillContact,
    CompanyIndex,
    account_sort,
    batches,
    name_from_domain,
    owner_criteria,
    parse_company_filter,
    plan_backfill,
    propagation_values,
)
from app.domain.contacts import FilterError, FilterGroup
from app.domain.paging import SortDirection


def test_account_sort_accepts_known_fields_only():
    sort = account_sort("contacts", "desc")
    assert sort.field == "contacts" and sort.direction is SortDirection.DESC
    with pytest.raises(ValueError):
        account_sort("email", "asc")


def test_owner_criteria():
    assert owner_criteria("me", "u1") == ("u1", False)
    assert owner_criteria("unassigned", "u1") == (None, True)
    assert owner_criteria("u2", "u1") == ("u2", False)
    assert owner_criteria(None, "u1") == (None, False)


def test_company_filter_tree_with_custom_and_company_prefix():
    tree = parse_company_filter({"op": "OR", "conditions": [
        {"field": "industry", "operator": "is", "value": "SaaS"},
        {"field": "company.owner_id", "operator": "is", "value": "me"},
        {"field": "custom.tier", "operator": "gt", "value": 3},
    ]}, "u1")
    assert isinstance(tree, FilterGroup) and tree.op == "OR"
    industry, owner, tier = tree.children
    assert (industry.kind, industry.name, industry.is_text) == ("company", "industry", True)
    assert owner.value == "u1"
    assert (tier.kind, tier.name, tier.numeric) == ("custom", "tier", True)
    assert parse_company_filter({}, "u1") is None
    assert parse_company_filter({"conditions": []}, "u1") is None


@pytest.mark.parametrize("spec", [
    {"field": "email", "operator": "is", "value": "x"},           # contact field
    {"field": "custom.Bad Key", "operator": "is", "value": "x"},
    {"field": "name", "operator": "explode", "value": "x"},
    ["not", "an", "object"],
])
def test_company_filter_rejects_bad_conditions(spec):
    with pytest.raises(FilterError):
        parse_company_filter(spec, "u1")


def test_company_filter_revenue_needs_the_amounts_right():
    spec = {"field": "annual_revenue", "operator": "gt", "value": 10}
    assert parse_company_filter(spec, "u1", lambda: True).is_text is False
    with pytest.raises(FilterError):
        parse_company_filter(spec, "u1", lambda: False)


def test_company_filter_depth_and_count_limits():
    deep: dict = {"field": "name", "operator": "is", "value": "x"}
    for _ in range(7):
        deep = {"conditions": [deep]}
    with pytest.raises(FilterError):
        parse_company_filter(deep, "u1")
    many = {"conditions": [{"field": "name", "operator": "is", "value": str(i)} for i in range(51)]}
    with pytest.raises(FilterError):
        parse_company_filter(many, "u1")


@pytest.mark.parametrize("field,old,new,current,expected", [
    ("name", "Acme", "Acme Inc", "Something else", (True, "Acme Inc")),   # the name always follows
    ("name", "Acme", "Acme Inc", "Acme Inc", (False, None)),              # already the new value
    ("industry", "SaaS", "Fintech", "SaaS", (True, "Fintech")),           # inherited value follows
    ("industry", "SaaS", "Fintech", None, (True, "Fintech")),             # empty is filled
    ("industry", "SaaS", "Fintech", "Banking", (False, None)),            # set on purpose: kept
    ("emp_band", "1-10", None, "1-10", (True, None)),                     # cleared on the company
    ("emp_band", "1-10", "1-10", "", (False, None)),                      # no change
])
def test_propagation_values(field, old, new, current, expected):
    assert propagation_values(field, old, new, current) == expected


def test_name_from_domain():
    assert name_from_domain("acme-labs.com") == "Acme Labs"
    assert name_from_domain("x_y.io") == "X Y"


def _c(pid, email=None, company=None, domain=None, industry=None):
    return BackfillContact(pid, email, company, domain, industry)


def test_backfill_plan_links_creates_and_sets_domain():
    index = CompanyIndex.build(
        live=[("a1", "Acme", "acme.com"), ("a2", "Globex", None)],
        deleted=[("Old Co", "old.com")])
    plan = plan_backfill([
        _c("p1", "x@acme.com", "Acme", "acme.com"),
        _c("p2", "y@acme.com", "ACME Corp", "acme.com"),       # same domain, different name -> mismatch
        _c("p3", "z@globex.com", "Globex", "globex.com"),     # domain-less Globex gets the domain
        _c("p4", "w@initech.com", None, "initech.com"),       # new company named from the domain
        _c("p5", "v@gmail.com", "Initech", None),             # personal address, by name -> the planned company
        _c("p6", "u@old.com", "Old Co", "old.com"),           # domain held by the recycle bin
        _c("p7", "t@gmail.com", None, None),                  # nothing to go on
    ], index)
    by_action = {(g.action, g.name): g for g in plan.groups}
    acme = by_action[("LINK", "Acme")]
    assert acme.account_id == "a1" and len(acme.contacts) == 2 and acme.name_mismatches == 1
    globex = by_action[("LINK", "Globex")]
    assert globex.account_id == "a2" and globex.set_domain == "globex.com"
    initech = by_action[("CREATE", "Initech")]
    assert initech.domain == "initech.com" and [c.prospect_id for c in initech.contacts] == ["p4", "p5"]
    conflict = by_action[("CONFLICT", "Old Co")]
    assert conflict.reason == "DOMAIN_IN_RECYCLE_BIN"
    stats = plan.stats()
    assert stats == {"contacts_considered": 7, "contacts_to_link": 5, "companies_to_create": 1,
                     "existing_companies_used": 2, "domains_to_set": 1, "company_names_to_fill": 1,
                     "name_mismatches": 1, "contacts_in_conflict": 1, "contacts_skipped": 1}
    reasons = {c["reason"] for c in plan.conflicts()}
    assert reasons == {"DOMAIN_IN_RECYCLE_BIN", "NAME_MISMATCH"}
    assert plan.samples()[0]["contacts"] == 2 and plan.samples()[0]["example_emails"] == ["x@acme.com", "y@acme.com"]


def test_backfill_plan_avoids_taken_names_and_recycle_bin_names():
    index = CompanyIndex.build(live=[("a1", "Acme", "acme.com")], deleted=[("Gone", None)])
    plan = plan_backfill([
        _c("p1", "x@acme.io", "Acme", "acme.io"),     # name taken by another domain's company
        _c("p2", "y@hotmail.com", "Gone", None),      # name held by the recycle bin
    ], index)
    create, conflict = plan.groups
    assert (create.action, create.name, create.domain) == ("CREATE", "Acme (acme.io)", "acme.io")
    assert (conflict.action, conflict.reason) == ("CONFLICT", "NAME_IN_RECYCLE_BIN")


def test_domainless_company_is_claimed_by_one_domain_only():
    index = CompanyIndex.build(live=[("a1", "Acme", None)], deleted=[])
    plan = plan_backfill([_c("p1", "a@acme.com", "Acme", "acme.com"), _c("p2", "b@acme.de", "Acme", "acme.de")],
                         index)
    first, second = plan.groups
    assert first.set_domain == "acme.com" and first.account_id == "a1"
    assert second.action == "CREATE" and second.name == "Acme (acme.de)"


def test_batches_keep_groups_whole():
    index = CompanyIndex.build([], [])
    plan = plan_backfill([_c(f"p{i}", None, f"Co {i % 3}", None) for i in range(9)], index)
    cut = batches(plan.actionable(), size=4)
    assert [sum(len(g.contacts) for g in b) for b in cut] == [6, 3]
