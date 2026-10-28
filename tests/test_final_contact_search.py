"""Contact search against MySQL after 0034_drop_contact_fulltext: the plain ``search_text LIKE '%term%'`` scan.

Kept from the D7 (FULLTEXT) suite: literal matching of names, e-mails, accents, punctuation, ``%`` / ``_`` /
``\\``, phone formats, tenant and owner scope, and the global search. Added: the live regression where
contacts committed in one workspace while another workspace was committing inserts never became searchable
(the FULLTEXT pre-filter dropped them); every committed contact must be found through the API."""
import random
import string
import uuid

import pytest
from sqlalchemy import insert

from app.core.database import SessionLocal, engine
from app.core.ids import new_id
from app.domain.contacts import ContactCriteria, VisibilityScope, search_terms
from app.domain.paging import Paging, Sort
from app.models.prospect import Prospect
from app.repositories.contacts import SqlAlchemyContactRepository
from tests.conftest import register_tenant, sql

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

# query -> indexes into PEOPLE that tenant one must return (scope: everyone)
KNOWN = [
    ("maria garcia", [0]), ("MARIA", [0]), ("garcía", [0]), ("garcia", [0]), ("maria.garcia@", [0]),
    ("zoë", [1]), ("zoe", [1]), ("o'neil", [1]), ("zoe+news", [1]), ("cafe", [1]), ("müller", [1]),
    ("muller", [1]), ("li_wei", [2]), ("李小", [2]), ("smith-jones", [3]), ("smith", [3]), ("a-team", [4]),
    ("c.e.o.", [4]), ("ceo", []), ("al*x", [5]), ("ma(x)", [5]), ('"quoted"', [5]), ("<lt>", [5]),
    ("~tilde", [5]), ("ïñès", [6]), ("d'arc", [6]),
    # LIKE metacharacters typed by the user are literal (unescaped, "%" and "_" would match every contact)
    ("%", [6]), ("_", [2, 6]), ("\\", [6]), ("b%c", [6]), ("a_b%c\\d", [6]),
    # phone digits, formatting ignored
    ("(737) 555-0188", [0]), ("7375550188", [0]), ("555-0188", [0]), ("737.555.0199", [6]),
    ("737 555", [0, 6]), ("86-10-6552-9988", [2]), ("+44 20 7946 0958", [5]), ("555 0100", [4]),
    ("nomatch-zqxj", []),
]


@pytest.fixture(scope="module")
def world(client):
    one, two = register_tenant(client, "fcsa"), register_tenant(client, "fcsb")
    owner = sql("SELECT user_id FROM users WHERE email = :e", e=one["email"])
    ids: dict[str, list[str]] = {one["tenant_id"]: [], two["tenant_id"]: []}
    with SessionLocal() as db:
        for i, (first, last, local, company, title, phone) in enumerate(PEOPLE):
            for tenant, who in ((one["tenant_id"], owner if i % 2 == 0 else None), (two["tenant_id"], None)):
                p = Prospect(tenant_id=tenant, first_name=first, last_name=last, email=f"{local}@ex{i}.test",
                             company_name=company, designation=title, phone=phone, owner_id=who)
                db.add(p)
                db.flush()
                ids[tenant].append(p.prospect_id)
        db.commit()
    return {"one": one, "two": two, "t1": one["tenant_id"], "t2": two["tenant_id"], "owner": owner,
            "ids": ids[one["tenant_id"]], "ids2": ids[two["tenant_id"]]}


def _search(tenant_id, q, scope=VisibilityScope()):
    criteria = ContactCriteria(tenant_id=tenant_id, visibility=scope, search=search_terms(q))
    with SessionLocal() as db:
        page = SqlAlchemyContactRepository(db).search(criteria, Sort.parse("name"), Paging(1, 100))
        return page.total, sorted(p.prospect_id for p in page.items)


def test_final_cs_01_no_fulltext_index_left():
    assert sql("SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() "
               "AND table_name = 'prospects' AND index_type = 'FULLTEXT'") == 0


@pytest.mark.parametrize("q,expected", KNOWN)
def test_final_cs_02_known_hits(world, q, expected):
    want = sorted(world["ids"][i] for i in expected)
    assert _search(world["t1"], q) == (len(want), want), q


def test_final_cs_03_api_list_search(client, world):
    h = world["one"]["headers"]
    r = client.get("/contacts", headers=h, params={"q": "(737) 555-0188"})
    assert r.status_code == 200, r.text
    assert [c["prospect_id"] for c in r.json()["items"]] == [world["ids"][0]]
    assert client.get("/contacts", headers=h, params={"q": "%"}).json()["total"] == 1


def test_final_cs_04_tenant_and_owner_scope_kept(world):
    assert _search(world["t1"], "maria") == (1, [world["ids"][0]])
    assert _search(world["t2"], "maria") == (1, [world["ids2"][0]])
    mine = VisibilityScope(owner_ids=frozenset({world["owner"]}))
    assert _search(world["t1"], "zoe", mine) == (0, [])  # Zoë is unassigned in tenant one
    with_unassigned = VisibilityScope(owner_ids=frozenset({world["owner"]}), include_unassigned=True)
    assert _search(world["t1"], "zoe", with_unassigned) == (1, [world["ids"][1]])


def test_final_cs_05_global_search_uses_the_same_condition(client, world):
    r = client.get("/search", headers=world["one"]["headers"], params={"q": "smith"})
    assert r.status_code == 200, r.text
    assert [c["prospect_id"] for c in r.json()["contacts"]] == [world["ids"][3]]
    with SessionLocal() as db:
        rows = SqlAlchemyContactRepository(db).search_contacts(world["t1"], VisibilityScope(), "b%c", 10)
    assert [p.prospect_id for p in rows] == [world["ids"][6]]


# ── live regression: contacts committed while another workspace commits inserts ──

def _bulk_rows(tenant_id: str, rng: random.Random, count: int) -> list[dict]:
    def words(n: int, width: int) -> str:
        return " ".join("".join(rng.choices(string.ascii_lowercase, k=8)) for _ in range(n))[:width]
    return [{"prospect_id": new_id(), "tenant_id": tenant_id, "email": f"bulk{i}-{uuid.uuid4().hex[:10]}@noise.test",
             "first_name": words(11, 100), "last_name": words(11, 100), "company_name": words(28, 255),
             "designation": words(16, 150)} for i in range(count)]


def test_final_cs_06_contacts_committed_during_other_tenant_writes_are_all_found(client, world):
    """Tenant one adds contacts in one transaction (as an import does) while tenant two commits several
    thousand inserts; once tenant one commits, every contact is found by the list and the global search.
    With the FULLTEXT pre-filter (0030) some of them never matched (live regression, contact search)."""
    noisy = register_tenant(client, "fcsn")
    marker = "zqk" + "".join(random.Random(11).choices(string.ascii_lowercase, k=5))
    rng = random.Random(5)
    created = []
    try:
        with SessionLocal() as db:
            for batch in range(4):
                for i in range(4):
                    p = Prospect(tenant_id=world["t1"], first_name="Ann", last_name=f"Adler{batch}{i}",
                                 email=f"{marker}.{batch}.{i}@alpha-zz.example", company_name="Alpha Corp")
                    db.add(p)
                    db.flush()
                    created.append(p.prospect_id)
                with engine.begin() as other:  # another workspace commits while this transaction is open
                    other.execute(insert(Prospect.__table__), _bulk_rows(noisy["tenant_id"], rng, 1500))
            db.commit()
        h = world["one"]["headers"]
        listed = client.get("/contacts", headers=h, params={"q": marker, "page_size": 50})
        assert listed.status_code == 200, listed.text
        assert sorted(c["prospect_id"] for c in listed.json()["items"]) == sorted(created)
        assert listed.json()["total"] == len(created)
        found = client.get("/search", headers=h, params={"q": marker, "limit": 25}).json()["contacts"]
        assert sorted(c["prospect_id"] for c in found) == sorted(created)
    finally:
        sql("DELETE FROM prospects WHERE tenant_id = :t", t=noisy["tenant_id"])
