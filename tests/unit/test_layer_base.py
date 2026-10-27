"""
ADR 0009 layer building blocks: paging/sort parameter objects (app.domain.paging) and the base
repository / unit of work (app.repositories.base). The SQLAlchemy classes run on a fake session:
no database in the unit layer.
"""
from dataclasses import FrozenInstanceError

import pytest

from app.domain.paging import Page, Paging, Sort, SortDirection
from app.repositories.base import Repository, SqlAlchemyRepository, SqlAlchemyUnitOfWork, UnitOfWork


# ── domain: paging / sort ──────────────────────────────────────────────────


def test_paging_offset_and_limit():
    assert Paging().offset == 0 and Paging().limit == 50
    assert Paging(page=3, page_size=20).offset == 40
    assert Paging(page=3, page_size=20).limit == 20


@pytest.mark.parametrize("page,size", [(0, 10), (-1, 10), (1, 0), (1, Paging.MAX_PAGE_SIZE + 1)])
def test_paging_rejects_out_of_range(page, size):
    with pytest.raises(ValueError):
        Paging(page=page, page_size=size)


def test_paging_is_immutable():
    paging = Paging()
    with pytest.raises(FrozenInstanceError):
        paging.page = 2  # type: ignore[misc]


def test_page_counts():
    page = Page(items=[1, 2], total=45, paging=Paging(page=2, page_size=20))
    assert page.pages == 3 and page.has_next
    last = Page(items=[], total=0, paging=Paging())
    assert last.pages == 1 and not last.has_next


def test_sort_parse():
    assert Sort.parse("name") == Sort("name", SortDirection.ASC)
    desc = Sort.parse(" -created_at ")
    assert desc.field == "created_at" and desc.descending
    assert Sort.parse("score", allowed=frozenset({"score"})).field == "score"
    with pytest.raises(ValueError):
        Sort.parse("password_hash", allowed=frozenset({"score"}))
    with pytest.raises(ValueError):
        Sort.parse("-")


# ── repositories: fake session ─────────────────────────────────────────────


class Thing:
    def __init__(self, id, tenant_id=None):
        self.id = id
        self.tenant_id = tenant_id


class FakeSession:
    def __init__(self, rows=()):
        self.rows = {row.id: row for row in rows}
        self.added, self.deleted, self.calls = [], [], []

    def get(self, model, ident):
        assert model is Thing
        return self.rows.get(ident)

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")

    def close(self):
        self.calls.append("close")


def test_repository_get_is_tenant_scoped():
    session = FakeSession([Thing(1, "t-a")])
    repo: Repository[Thing, int] = SqlAlchemyRepository(session, Thing)
    assert repo.get("t-a", 1).id == 1
    assert repo.get("t-b", 1) is None  # another tenant's row reads as missing
    assert repo.get("t-a", 2) is None


def test_repository_add_sets_or_checks_tenant():
    session = FakeSession()
    repo = SqlAlchemyRepository(session, Thing)
    thing = repo.add("t-a", Thing(5))
    assert thing.tenant_id == "t-a" and session.added == [thing]
    with pytest.raises(ValueError):
        repo.add("t-a", Thing(6, "t-b"))
    assert session.added == [thing]


def test_repository_delete_only_own_tenant():
    row = Thing(1, "t-a")
    session = FakeSession([row])
    repo = SqlAlchemyRepository(session, Thing)
    assert repo.delete("t-b", 1) is False and session.deleted == []
    assert repo.delete("t-a", 1) is True and session.deleted == [row]
    assert session.calls == []  # repositories never commit


def test_unit_of_work_commit_and_close():
    session = FakeSession()
    uow: UnitOfWork = SqlAlchemyUnitOfWork(lambda: session)
    with uow as active:
        assert active.session is session
        active.commit()
    assert session.calls == ["commit", "close"]
    with pytest.raises(RuntimeError):
        _ = active.session  # outside the with block


def test_unit_of_work_rolls_back_on_error():
    session = FakeSession()
    with pytest.raises(KeyError):
        with SqlAlchemyUnitOfWork(lambda: session):
            raise KeyError("boom")
    assert session.calls == ["rollback", "close"]


def test_unit_of_work_borrowed_session_is_not_closed():
    session = FakeSession()
    with SqlAlchemyUnitOfWork.for_session(session) as uow:
        uow.commit()
    assert session.calls == ["commit"]  # get_db closes the request session
    with pytest.raises(ValueError):
        with SqlAlchemyUnitOfWork.for_session(session) as uow:
            raise ValueError
    assert session.calls == ["commit", "rollback"]


def test_unit_of_work_is_not_reentrant():
    uow = SqlAlchemyUnitOfWork(FakeSession)
    with uow:
        with pytest.raises(RuntimeError):
            uow.__enter__()
