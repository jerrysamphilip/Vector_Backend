"""F017: one ACTIVE / INVITED account per email across workspaces (model, migration 0019, lookups, checks)."""
import importlib.util
import logging
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core.errors import ConflictError
from app.models.user import User
from app.services import invitation_service, user_admin_service, user_lookup

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _u(user_id, status, created=None):
    return SimpleNamespace(user_id=user_id, status=status, created_at=created)


def test_sec_pick_login_user_prefers_live_account_deterministically():
    old = datetime(2025, 1, 1)
    new = datetime(2026, 1, 1)
    rows = [_u("a", "INACTIVE", old), _u("b", "ACTIVE", new), _u("c", "INVITED", old)]
    assert user_lookup.pick_login_user(rows).user_id == "b"
    assert user_lookup.pick_login_user(list(reversed(rows))).user_id == "b"
    assert user_lookup.pick_login_user([_u("x", "INVITED", new), _u("y", "INACTIVE", old)]).user_id == "x"
    # Two ACTIVE rows (data from before the index): the oldest, then the lowest id
    assert user_lookup.pick_login_user([_u("z", "ACTIVE", new), _u("y", "ACTIVE", old)]).user_id == "y"
    assert user_lookup.pick_login_user([_u("z", "ACTIVE", None), _u("y", "ACTIVE", None)]).user_id == "y"
    assert user_lookup.pick_login_user([_u("s", "SUSPENDED", new)]).user_id == "s"
    assert user_lookup.pick_login_user([]) is None


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []

    def filter(self, *criteria):
        self.filters.extend(criteria)
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _Db:
    def __init__(self, rows):
        self.q = _Query(rows)

    def query(self, *entities):
        return self.q


def test_sec_find_user_for_login_and_conflict_lookup():
    db = _Db([_u("a", "INACTIVE"), _u("b", "ACTIVE")])
    assert user_lookup.find_user_for_login(db, "x@example.com").user_id == "b"
    assert user_lookup.find_user_for_login(db, "") is None
    db = _Db([_u("b", "ACTIVE")])
    assert user_lookup.live_account_conflict(db, "x@example.com", exclude_user_id="a").user_id == "b"
    assert len(db.q.filters) == 3  # email, live status, not the excluded account
    assert user_lookup.live_account_conflict(_Db([]), "x@example.com") is None
    assert user_lookup.live_account_conflict(_Db([_u("b", "ACTIVE")]), "") is None


def test_sec_model_declares_generated_column_and_unique_index():
    column = User.__table__.c.active_email
    assert column.computed is not None and column.computed.persisted is True
    assert "'ACTIVE','INVITED'" in str(column.computed.sqltext)
    indexes = {ix.name: ix for ix in User.__table__.indexes}
    assert indexes["uq_users_active_email"].unique
    assert [c.name for c in indexes["uq_users_active_email"].columns] == ["active_email"]


def test_sec_require_no_other_live_account():
    with pytest.raises(ConflictError):
        invitation_service.require_no_other_live_account(_Db([_u("b", "ACTIVE")]), "x@example.com", "a")
    invitation_service.require_no_other_live_account(_Db([]), "x@example.com", "a")


def test_sec_reactivate_refused_when_email_live_elsewhere(monkeypatch):
    target = SimpleNamespace(user_id="t", email="x@example.com", status="INACTIVE", role="AGENT")
    actor = SimpleNamespace(user_id="me", role="SUPER_ADMIN", email="boss@example.com")
    monkeypatch.setattr(user_admin_service, "get_member", lambda db, a, uid: target)
    db = _Db([_u("other", "ACTIVE")])
    with pytest.raises(ConflictError):
        user_admin_service.reactivate(db, actor, "t")
    assert target.status == "INACTIVE"


def _load_migration():
    path = os.path.join(ROOT, "alembic", "versions", "0019_unique_active_email.py")
    spec = importlib.util.spec_from_file_location("mig_0019", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Op:
    def __init__(self):
        self.sql = []

    def execute(self, sql):
        self.sql.append(sql)


def test_sec_migration_chain_and_expression():
    mig = _load_migration()
    assert mig.revision == "0019_unique_active_email"
    assert mig.down_revision == "0018_datetime6"
    from app.models.user import ACTIVE_EMAIL_EXPRESSION
    assert mig.EXPRESSION == ACTIVE_EMAIL_EXPRESSION


def test_sec_migration_adds_column_and_index(monkeypatch):
    mig = _load_migration()
    op = _Op()
    monkeypatch.setattr(mig, "op", op)
    monkeypatch.setattr(mig, "_has_column", lambda: False)
    monkeypatch.setattr(mig, "_has_index", lambda: False)
    monkeypatch.setattr(mig, "_duplicates", lambda: (0, 0))
    mig.upgrade()
    assert "GENERATED ALWAYS AS" in op.sql[0] and "STORED" in op.sql[0]
    assert op.sql[1] == "CREATE UNIQUE INDEX uq_users_active_email ON users (active_email)"


def test_sec_migration_skips_index_on_duplicates(monkeypatch, caplog):
    mig = _load_migration()
    op = _Op()
    monkeypatch.setattr(mig, "op", op)
    monkeypatch.setattr(mig, "_has_column", lambda: True)
    monkeypatch.setattr(mig, "_has_index", lambda: False)
    monkeypatch.setattr(mig, "_duplicates", lambda: (2, 5))
    with caplog.at_level(logging.WARNING):
        mig.upgrade()
    assert op.sql == []
    assert "2 email address(es)" in caplog.text and "5 accounts" in caplog.text and "NOT created" in caplog.text


def test_sec_migration_is_idempotent_and_downgrades(monkeypatch):
    mig = _load_migration()
    op = _Op()
    monkeypatch.setattr(mig, "op", op)
    monkeypatch.setattr(mig, "_has_column", lambda: True)
    monkeypatch.setattr(mig, "_has_index", lambda: True)
    mig.upgrade()
    assert op.sql == []
    mig.downgrade()
    assert op.sql == ["DROP INDEX uq_users_active_email ON users", "ALTER TABLE users DROP COLUMN active_email"]
