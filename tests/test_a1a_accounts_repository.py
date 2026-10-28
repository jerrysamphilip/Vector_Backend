"""
A1 repository (MySQL): SqlAlchemyAccountRepository filters, owner scope, paging, sort, tenant isolation,
the company filter tree on custom properties, and the backfill ledger reads.
"""
import uuid

import pytest

from app.domain.accounts import AccountCriteria, parse_company_filter
from app.domain.contacts import VisibilityScope
from app.domain.paging import Paging, Sort, SortDirection
from app.models.account import Account
from app.models.account_backfill import AccountBackfillItem, AccountBackfillRun
from app.models.prospect import Prospect
from app.repositories.accounts import SqlAlchemyAccountRepository
from tests.conftest import SessionLocal, register_tenant, sql


@pytest.fixture(scope="module")
def seeded(client):
    a = register_tenant(client, "arpa")
    b = register_tenant(client, "arpb")
    owner = sql("SELECT user_id FROM users WHERE tenant_id = :t", t=a["tenant_id"])
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        accounts = [
            Account(tenant_id=a["tenant_id"], name=f"Alpha {tag}", domain=f"alpha{tag}.com", industry="SaaS",
                    owner_id=owner, custom_fields={"tier": "Gold", "seats": 50}),
            Account(tenant_id=a["tenant_id"], name=f"Beta {tag}", industry="Banking", owner_id=None,
                    custom_fields={"tier": "Silver", "seats": 5}),
            Account(tenant_id=a["tenant_id"], name=f"Gamma {tag}", industry="SaaS", owner_id=None),
            Account(tenant_id=b["tenant_id"], name=f"Alpha {tag}", industry="SaaS"),
        ]
        db.add_all(accounts)
        db.flush()
        # Gamma has one contact owned by the tenant owner; Beta has none
        db.add(Prospect(tenant_id=a["tenant_id"], email=f"c{tag}@gamma{tag}.com", owner_id=owner,
                        account_id=accounts[2].account_id, first_name="G"))
        db.add(Prospect(tenant_id=a["tenant_id"], email=f"free{tag}@gmail.com", owner_id=owner,
                        company_name=f"Unlinked {tag}", first_name="U"))
        db.commit()
        ids = [x.account_id for x in accounts]
    return {"a": a["tenant_id"], "b": b["tenant_id"], "owner": owner, "tag": tag, "ids": ids}


def _names(page):
    return [a.name.split(" ")[0] for a, _ in page.items]


def test_search_tenant_isolation_sort_paging_and_counts(seeded):
    with SessionLocal() as db:
        repo = SqlAlchemyAccountRepository(db)
        c = AccountCriteria(tenant_id=seeded["a"], q=seeded["tag"])
        page = repo.search(c, Sort("name"), Paging(1, 2))
        assert page.total == 3 and _names(page) == ["Alpha", "Beta"]
        assert _names(repo.search(c, Sort("name"), Paging(2, 2))) == ["Gamma"]
        by_contacts = repo.search(c, Sort("contacts", SortDirection.DESC), Paging(1, 10))
        assert by_contacts.items[0][0].name.startswith("Gamma") and by_contacts.items[0][1] == 1
        other = repo.search(AccountCriteria(tenant_id=seeded["b"], q=seeded["tag"]), Sort("name"), Paging())
        assert other.total == 1


def test_owner_scope_and_filters(seeded):
    with SessionLocal() as db:
        repo = SqlAlchemyAccountRepository(db)
        nobody = VisibilityScope(frozenset({"someone-else"}))
        hidden = repo.search(AccountCriteria(tenant_id=seeded["a"], q=seeded["tag"], scope=nobody), Sort("name"),
                             Paging())
        assert hidden.total == 0
        mine = VisibilityScope(frozenset({seeded["owner"]}))
        # Alpha (owned) and Gamma (holds a visible contact); Beta is unowned without contacts
        seen = repo.search(AccountCriteria(tenant_id=seeded["a"], q=seeded["tag"], scope=mine), Sort("name"),
                           Paging())
        assert _names(seen) == ["Alpha", "Gamma"]
        unassigned = repo.search(AccountCriteria(tenant_id=seeded["a"], q=seeded["tag"], unassigned=True),
                                 Sort("name"), Paging())
        assert _names(unassigned) == ["Beta", "Gamma"]
        saas = repo.search(AccountCriteria(tenant_id=seeded["a"], q=seeded["tag"], industry="SaaS"), Sort("name"),
                           Paging())
        assert _names(saas) == ["Alpha", "Gamma"]


@pytest.mark.parametrize("spec,expected", [
    ({"field": "custom.tier", "operator": "is", "value": "gold"}, ["Alpha"]),
    ({"field": "custom.seats", "operator": "gt", "value": 10}, ["Alpha"]),
    ({"field": "custom.tier", "operator": "is_empty"}, ["Gamma"]),
    ({"op": "OR", "conditions": [{"field": "industry", "operator": "is", "value": "Banking"},
                                 {"field": "custom.seats", "operator": "gte", "value": 50}]}, ["Alpha", "Beta"]),
])
def test_company_filter_tree(seeded, spec, expected):
    with SessionLocal() as db:
        repo = SqlAlchemyAccountRepository(db)
        c = AccountCriteria(tenant_id=seeded["a"], q=seeded["tag"], tree=parse_company_filter(spec, "u"))
        assert _names(repo.search(c, Sort("name"), Paging())) == expected
        assert repo.count(c) == len(expected)


def test_backfill_reads_and_ledger(seeded):
    with SessionLocal() as db:
        repo = SqlAlchemyAccountRepository(db)
        rows = [r for r in repo.unlinked_contacts(seeded["a"], VisibilityScope()) if seeded["tag"] in (r[1] or "")]
        assert [r[2] for r in rows] == [f"Unlinked {seeded['tag']}"]
        assert repo.unlinked_contacts(seeded["b"], VisibilityScope()) == []
        live, _ = repo.company_index_rows(seeded["a"])
        assert {i for i, _, _ in live} >= set(seeded["ids"][:3]) and seeded["ids"][3] not in {i for i, _, _ in live}
        run = AccountBackfillRun(tenant_id=seeded["a"], started_by=seeded["owner"], status="COMPLETED")
        repo.add(seeded["a"], run)
        repo.flush()
        repo.add(seeded["a"], AccountBackfillItem(run_id=run.run_id, tenant_id=seeded["a"], kind="ACCOUNT_CREATED",
                                                  account_id=seeded["ids"][1]))
        repo.flush()
        assert repo.run(seeded["b"], run.run_id) is None
        assert [i.kind for i in repo.run_items(run.run_id, ("ACCOUNT_CREATED",), 0, 10)] == ["ACCOUNT_CREATED"]
        assert repo.runs(seeded["a"], {"nobody"}, 10) == []
        assert repo.account_in_use(seeded["ids"][2]) is True and repo.account_in_use(seeded["ids"][1]) is False
        db.rollback()


def test_name_and_domain_checks_are_per_tenant(seeded):
    with SessionLocal() as db:
        repo = SqlAlchemyAccountRepository(db)
        name = f"Alpha {seeded['tag']}"
        assert repo.name_taken(seeded["a"], name) and not repo.name_taken(seeded["a"], name, except_id=seeded["ids"][0])
        assert repo.domain_taken(seeded["a"], f"alpha{seeded['tag']}.com")
        assert not repo.domain_taken(seeded["b"], f"alpha{seeded['tag']}.com")
        assert repo.live(seeded["b"], seeded["ids"][0]) is None
        assert repo.live_by(seeded["a"], name=name).account_id == seeded["ids"][0]

