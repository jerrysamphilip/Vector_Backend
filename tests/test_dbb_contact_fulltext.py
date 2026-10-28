"""Contact search on the FULLTEXT ngram index (chunk D7, migration 0030) against MySQL: the MATCH pre-filter
returns exactly what the previous LIKE-only search returned (names, e-mails, phone digits, accents,
punctuation and boolean-mode operators, short terms), keeps tenant and owner scope, and the index exists with
the token size and stopword setting the repository assumes."""
import random
import uuid

import pytest
from sqlalchemy import text

from app.core.database import SessionLocal
from app.domain.contacts import ContactCriteria, VisibilityScope, search_terms
from app.domain.paging import Paging, Sort
from app.models.prospect import Prospect
from app.repositories import contacts as contacts_repo
from app.repositories.contacts import FULLTEXT_TOKEN_SIZE, SqlAlchemyContactRepository
from tests.conftest import register_tenant, sql

TAG = uuid.uuid4().hex[:8]

PEOPLE = [
    # first, last, email local part, company, title, phone
    ("Maria", "García", "maria.garcia", "Apex Labs", "CTO", "+1 (737) 555-0188"),
    ("Zoë", "O'Neil", "zoe+news", "Café Müller", "Head of Sales", "0049 30 1234567"),
    ("Li", "Wei", "li_wei", "李小龙 Ltd", "VP", "86-10-6552-9988"),
    ("Ann", "Smith-Jones", "a.smith", "Straße & Söhne", "Owner", None),
    ("Bob", "Baker", "bob", "The A-Team", "C.E.O.", "555 0100"),
    ("Al*x", "Ma(x)", "alx", "\"Quoted\" Co", "~tilde <lt> @at", "+44 20 7946 0958"),
    ("Ïñès", "d'Arc", "ines", "a_b%c\\d", "Dev", "737.555.0199"),
]

QUERIES = [
    "maria", "MARIA", "garcia", "garcía", "maria garcia", "ria gar", "aria.gar", "maria.garcia@", "apex labs",
    "zoe", "zoë", "o'neil", "neil", "zoe+news", "+news", "café", "cafe", "muller", "müller",
    "li", "li_wei", "_wei", "李小", "小龙", "wei", "smith-jones", "smith", "-jones", "straße", "strasse",
    "a-team", "the a", "c.e.o.", "ceo", "al*x", "ma(x)", '"quoted"', "quoted", "~tilde", "<lt>", "@at",
    "ines", "ïñès", "d'arc", "a_b%c\\d", "%", "_", "\\", "b%c",
    "737 555 0188", "(737) 555-0188", "7375550188", "555-0188", "0188", "+1", "555", "86-10-6552-9988",
    "+44 20 7946 0958", "737.555.0199", "a", "m", "j s", "ma", "an", "-", "+", '"', "*", "()", "@",
    "nomatch-zqxj", "zz",
]


@pytest.fixture(scope="module")
def world(client):
    one, two = register_tenant(client, "dbbf"), register_tenant(client, "dbbg")
    owner = sql("SELECT user_id FROM users WHERE email = :e", e=one["email"])
    with SessionLocal() as db:
        ids = []
        for i, (first, last, local, company, title, phone) in enumerate(PEOPLE):
            for tenant, who in ((one["tenant_id"], owner if i % 2 == 0 else None), (two["tenant_id"], None)):
                p = Prospect(tenant_id=tenant, first_name=first, last_name=last, email=f"{local}.{TAG}@ex{i}.io",
                             company_name=company, designation=title, phone=phone, owner_id=who)
                db.add(p)
                db.flush()
                if tenant == one["tenant_id"]:
                    ids.append(p.prospect_id)
        db.commit()  # the FULLTEXT index sees committed rows only
    return {"t1": one["tenant_id"], "t2": two["tenant_id"], "owner": owner, "ids": ids}


def _search(tenant_id, q, scope=VisibilityScope()):
    criteria = ContactCriteria(tenant_id=tenant_id, visibility=scope, search=search_terms(q))
    with SessionLocal() as db:
        page = SqlAlchemyContactRepository(db).search(criteria, Sort.parse("name"), Paging(1, 100))
        return page.total, [p.prospect_id for p in page.items]


def _like_only(monkeypatch, fn):
    with monkeypatch.context() as m:
        m.setattr(contacts_repo, "fulltext_query", lambda _terms: None)
        return fn()


def test_dbb_ft_01_server_matches_the_repository_assumptions():
    assert sql("SELECT @@ngram_token_size") == FULLTEXT_TOKEN_SIZE
    assert sql("SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() "
               "AND table_name = 'prospects' AND index_name = 'ft_prospects_search_text' "
               "AND index_type = 'FULLTEXT'") == 1


def test_dbb_ft_02_index_was_built_without_stopwords(world):
    # With the InnoDB stopword list every bigram containing "a" is dropped: "ma" would find nothing
    found = sql("SELECT COUNT(*) FROM prospects WHERE tenant_id = :t AND MATCH (search_text) "
                "AGAINST ('+\"ma\"' IN BOOLEAN MODE)", t=world["t1"])
    assert found >= 1


@pytest.mark.parametrize("q", QUERIES)
def test_dbb_ft_03_fulltext_equals_like(world, monkeypatch, q):
    expected = _like_only(monkeypatch, lambda: _search(world["t1"], q))
    assert _search(world["t1"], q) == expected


def test_dbb_ft_04_known_hits(world):
    ids = world["ids"]
    assert _search(world["t1"], "maria garcia")[1] == [ids[0]]
    assert _search(world["t1"], "zoë")[1] == [ids[1]]           # accent-insensitive, as before
    assert _search(world["t1"], "(737) 555-0188")[1] == [ids[0]]  # phone digits, formatting ignored
    assert _search(world["t1"], "al*x")[1] == [ids[5]]          # operators are literal text
    assert _search(world["t1"], "nomatch-zqxj") == (0, [])


def test_dbb_ft_05_random_substrings_equal_like(world, monkeypatch):
    rng = random.Random(7)
    with SessionLocal() as db:
        texts = [t for (t,) in db.execute(text("SELECT search_text FROM prospects WHERE tenant_id = :t"),
                                          {"t": world["t1"]})]
    for _ in range(60):
        source = rng.choice(texts)
        start = rng.randrange(len(source))
        q = source[start:start + rng.randint(1, 9)]
        if not q.strip():
            continue
        expected = _like_only(monkeypatch, lambda: _search(world["t1"], q))
        assert _search(world["t1"], q) == expected, q
        assert expected[0] >= 1, q


def test_dbb_ft_06_tenant_and_owner_scope_kept(world):
    t1_total, t1_ids = _search(world["t1"], "maria")
    assert t1_total == 1 and set(t1_ids) <= set(world["ids"])
    mine = VisibilityScope(owner_ids=frozenset({world["owner"]}))
    assert _search(world["t1"], "zoe", mine) == (0, [])  # Zoë is unassigned in tenant one
    with_unassigned = VisibilityScope(owner_ids=frozenset({world["owner"]}), include_unassigned=True)
    assert _search(world["t1"], "zoe", with_unassigned)[0] == 1
    assert _search(world["t2"], "maria")[0] == 1 and _search(world["t2"], "maria")[1][0] not in world["ids"]


def test_dbb_ft_07_global_search_uses_the_same_condition(world, monkeypatch):
    def run():
        with SessionLocal() as db:
            rows = SqlAlchemyContactRepository(db).search_contacts(world["t1"], VisibilityScope(), "smith", 10)
            return sorted(p.prospect_id for p in rows)
    assert run() == _like_only(monkeypatch, run) == [world["ids"][3]]


def test_dbb_ft_08_match_is_in_the_sql():
    with SessionLocal() as db:
        explain = db.execute(text(
            "EXPLAIN SELECT prospect_id FROM prospects WHERE MATCH (search_text) AGAINST ('+\"smith\"' IN BOOLEAN MODE)"
        )).mappings().first()
    assert explain["type"] == "fulltext" and explain["key"] == "ft_prospects_search_text"
