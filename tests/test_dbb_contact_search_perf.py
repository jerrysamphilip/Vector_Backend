"""Contact search timing at 100k contacts (chunk D7): p95 of the repository search < 1 s with the FULLTEXT
ngram pre-filter, compared with the previous LIKE-only scan on the same queries.

Runs only with PERF=1 against a database seeded by scripts/load/seed_load_data.py (100k contacts):

    docker exec vector-mysql-1 mysql -uroot -proot -e "DROP DATABASE IF EXISTS vtest_load_dbb; CREATE DATABASE vtest_load_dbb"
    MYSQL_HOST=127.0.0.1 MYSQL_PORT=3316 MYSQL_USER=root MYSQL_PASSWORD=root MYSQL_DATABASE=vtest_load_dbb \\
        OPENAI_API_KEY= python -m app.db.migrate && ... python scripts/load/seed_load_data.py
    PERF=1 ... python -m pytest -q -s tests/test_dbb_contact_search_perf.py

``-s`` prints the LIKE vs FULLTEXT table. Both paths must return identical pages and totals.
"""
from __future__ import annotations

import os
import statistics
import time
from typing import Callable

import pytest
from sqlalchemy import text

from app.core.database import SessionLocal
from app.domain.contacts import ContactCriteria, VisibilityScope, search_terms
from app.domain.paging import Paging, Sort, SortDirection
from app.repositories import contacts as contacts_repo
from app.repositories.contacts import SqlAlchemyContactRepository

pytestmark = pytest.mark.skipif(os.getenv("PERF") != "1", reason="timing test: set PERF=1 (seeded 100k database)")

MIN_CONTACTS = 100_000
RUNS = int(os.getenv("PERF_RUNS", "20"))
P95_BUDGET_S = 1.0
TENANT_NAME = "Load Test Co"  # scripts/load/seed_load_data.py


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]


@pytest.fixture(scope="module")
def seeded():
    with SessionLocal() as db:
        tenant_id = db.execute(text("SELECT tenant_id FROM tenants WHERE tenant_name = :n"), {"n": TENANT_NAME}).scalar()
        if tenant_id is None:
            pytest.skip("not a seeded load database (scripts/load/seed_load_data.py)")
        live = db.execute(text("SELECT COUNT(*) FROM prospects WHERE tenant_id = :t AND deleted_at IS NULL"),
                          {"t": tenant_id}).scalar()
        if live < MIN_CONTACTS:
            pytest.skip(f"needs {MIN_CONTACTS} contacts, found {live}")
        phone = db.execute(text("SELECT phone FROM prospects WHERE tenant_id = :t AND phone IS NOT NULL "
                                "AND phone <> '' ORDER BY prospect_id LIMIT 1"), {"t": tenant_id}).scalar()
        email = db.execute(text("SELECT email FROM prospects WHERE tenant_id = :t ORDER BY prospect_id LIMIT 1"),
                           {"t": tenant_id}).scalar()
        agent_ids = frozenset(r[0] for r in db.execute(text(
            "SELECT user_id FROM users WHERE tenant_id = :t AND email LIKE 'agent.n1@%'"), {"t": tenant_id}))
    return {"tenant_id": tenant_id, "phone": phone, "email": email, "agent_ids": agent_ids}


def _queries(seeded) -> list[tuple[str, str, VisibilityScope]]:
    everyone = VisibilityScope()
    agent = VisibilityScope(owner_ids=seeded["agent_ids"], include_unassigned=True)
    return [
        ("last name", "smith", everyone),
        ("first + last", "maria garcia", everyone),
        ("company word, agent scope", "apex", agent),
        ("e-mail prefix", seeded["email"].split("@")[0][:12] + "@", everyone),
        ("phone (formatted)", seeded["phone"], everyone),
        ("no match", "zqxjv", everyone),
    ]


def _time(fn: Callable[[], object]) -> tuple[list[float], object]:
    samples, result = [], None
    for _ in range(RUNS):
        start = time.perf_counter()
        result = fn()
        samples.append(time.perf_counter() - start)
    return samples, result


def _search(tenant_id: str, q: str, scope: VisibilityScope):
    criteria = ContactCriteria(tenant_id=tenant_id, visibility=scope, search=search_terms(q))
    with SessionLocal() as db:
        page = SqlAlchemyContactRepository(db).search(criteria, Sort("created_at", SortDirection.DESC), Paging(1, 50))
        return page.total, [p.prospect_id for p in page.items]


def test_dbb_perf_contact_search_p95_under_1s_at_100k(seeded, monkeypatch):
    rows, failures = [], []
    for label, q, scope in _queries(seeded):
        fulltext, ft_result = _time(lambda: _search(seeded["tenant_id"], q, scope))
        with monkeypatch.context() as m:  # the previous implementation: LIKE scan only
            m.setattr(contacts_repo, "fulltext_query", lambda _terms: None)
            like, like_result = _time(lambda: _search(seeded["tenant_id"], q, scope))
        assert ft_result == like_result, f"{label}: FULLTEXT and LIKE disagree for {q!r}"
        rows.append((label, q, ft_result[0], statistics.median(like), _p95(like),
                     statistics.median(fulltext), _p95(fulltext)))
        if _p95(fulltext) >= P95_BUDGET_S:
            failures.append(f"{label} ({q!r}): p95 {_p95(fulltext):.3f}s")
    print(f"\n{'query':28} {'q':24} {'hits':>6} {'LIKE p50':>9} {'LIKE p95':>9} {'FT p50':>8} {'FT p95':>8}")
    for label, q, hits, lp50, lp95, fp50, fp95 in rows:
        print(f"{label:28} {q[:24]:24} {hits:6d} {lp50:9.3f} {lp95:9.3f} {fp50:8.3f} {fp95:8.3f}")
    assert not failures, "contact search p95 >= 1 s at 100k: " + "; ".join(failures)
