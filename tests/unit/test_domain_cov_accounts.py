"""Coverage of the company filter values and backfill planner edges (app/domain/accounts.py)."""
from datetime import datetime

import pytest

from app.domain.accounts import BackfillContact, BackfillGroup, CompanyIndex, batches, parse_company_filter, plan_backfill
from app.domain.contacts import FilterError


def test_revenue_before_after_compares_as_dates_but_text_fields_keep_the_raw_value():
    revenue = parse_company_filter({"field": "annual_revenue", "operator": "after", "value": "2024-01-02T03:04:05Z"}, "u1")
    assert revenue.is_text is False
    assert revenue.value == datetime(2024, 1, 2, 3, 4, 5)
    name = parse_company_filter({"field": "name", "operator": "before", "value": "2024-01-02"}, "u1")
    assert name.is_text is True and name.value == "2024-01-02"


def test_in_last_days_is_an_int_and_rejects_non_numbers():
    cond = parse_company_filter({"field": "custom.signed", "operator": "in_last_days", "value": "30"}, "u1")
    assert cond.value == 30 and cond.kind == "custom"
    for bad in ("soon", None):
        with pytest.raises(FilterError, match="in_last_days"):
            parse_company_filter({"field": "industry", "operator": "in_last_days", "value": bad}, "u1")


def test_free_mail_contacts_with_the_same_company_name_share_one_group():
    plan = plan_backfill([
        BackfillContact("p1", "a@gmail.com", "Acme", None),
        BackfillContact("p2", "b@gmail.com", " acme ", None),
    ], CompanyIndex())
    assert len(plan.groups) == 1
    group = plan.groups[0]
    assert group.action == "CREATE" and [c.prospect_id for c in group.contacts] == ["p1", "p2"]
    assert plan.stats()["companies_to_create"] == 1 and plan.stats()["contacts_to_link"] == 2


def test_batches_ending_exactly_on_the_size_leave_no_trailing_batch():
    groups = [BackfillGroup("CREATE", f"c{i}", contacts=[BackfillContact(f"p{i}", None, None, None)])
              for i in range(4)]
    out = batches(groups, size=2)
    assert out == [groups[:2], groups[2:]]


def test_free_mail_contact_links_to_the_live_company_with_its_name():
    index = CompanyIndex.build([("acc1", "Acme Inc", "acme.com")], [])
    plan = plan_backfill([BackfillContact("p1", "a@gmail.com", "ACME INC", None)], index)
    (group,) = plan.groups
    assert (group.action, group.account_id, group.name, group.domain) == ("LINK", "acc1", "Acme Inc", "acme.com")
    assert group.name_mismatches == 0 and plan.stats()["existing_companies_used"] == 1
