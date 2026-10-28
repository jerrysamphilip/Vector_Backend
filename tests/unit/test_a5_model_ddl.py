"""
A5 (ADR 0009): the identity models (users, tenants, invitations, refresh / magic-login / password-reset
tokens) moved to SQLAlchemy 2 ``Mapped[]`` / ``mapped_column`` with the same DDL. The MySQL
``CREATE TABLE`` / ``CREATE INDEX`` output (including the ``users.active_email`` generated column and its
unique index ``uq_users_active_email``), the Python-side column options and the relationships are compared
against a snapshot taken before the conversion (tests/unit/snapshots/a5_identity_ddl.txt). No database.

Regenerate only for an intended schema change that ships with its migration:
    python tests/unit/test_a5_model_ddl.py --write
"""
import os
import sys

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

SNAPSHOT = os.path.join(os.path.dirname(__file__), "snapshots", "a5_identity_ddl.txt")
TABLES = (
    "users", "tenants", "invitations", "refresh_tokens", "magic_login_tokens", "password_reset_tokens",
)


def _default(value):
    if value is None:
        return None
    if getattr(value, "is_callable", False):
        return "<callable>"
    if getattr(value, "is_clause_element", False) or getattr(value, "is_sequence", False):
        return str(getattr(value, "arg", value))
    return repr(getattr(value, "arg", value))


def _server_default(value):
    if value is None:
        return None
    if hasattr(value, "sqltext"):  # Computed
        return f"Computed({value.sqltext}, persisted={value.persisted})"
    arg = getattr(value, "arg", value)
    return str(getattr(arg, "text", arg))


def _relationships(table_name: str) -> list[str]:
    from app.models.base import Base

    mapper = next(m for m in Base.registry.mappers if m.local_table.name == table_name)
    return [
        f"{table_name} rel {rel.key}: target={rel.mapper.class_.__name__} lazy={rel.lazy} "
        f"back_populates={rel.back_populates} uselist={rel.uselist}"
        for rel in sorted(mapper.relationships, key=lambda r: r.key)
    ]


def render() -> str:
    import app.models  # noqa: F401  (registers every table)
    from app.models.base import Base

    dialect = mysql.dialect()
    out = []
    for name in TABLES:
        table = Base.metadata.tables[name]
        out.append(str(CreateTable(table).compile(dialect=dialect)).strip())
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            out.append(str(CreateIndex(index).compile(dialect=dialect)).strip())
        out.append(f"{name} kwargs {sorted(table.kwargs.items())}")
        for col in table.columns:
            out.append(
                f"{name}.{col.key}: type={col.type!r} nullable={col.nullable} pk={col.primary_key} "
                f"index={col.index} unique={col.unique} autoincrement={col.autoincrement!r} "
                f"default={_default(col.default)} onupdate={_default(col.onupdate)} "
                f"server_default={_server_default(col.server_default)} "
                f"server_onupdate={_server_default(col.server_onupdate)} computed={col.computed is not None}")
        out.extend(_relationships(name))
    return "\n\n".join(out) + "\n"


def test_a5_identity_models_ddl_unchanged():
    with open(SNAPSHOT, encoding="utf-8") as fh:
        expected = fh.read()
    assert render() == expected


def test_a5_active_email_generated_column_and_unique_index():
    import app.models  # noqa: F401
    from app.models.base import Base

    users = Base.metadata.tables["users"]
    ddl = str(CreateTable(users).compile(dialect=mysql.dialect()))
    assert "active_email VARCHAR(255) GENERATED ALWAYS AS (CASE WHEN status IN ('ACTIVE','INVITED') " \
           "THEN email ELSE NULL END) STORED" in ddl
    index = next(i for i in users.indexes if i.name == "uq_users_active_email")
    assert index.unique and [c.name for c in index.columns] == ["active_email"]


def test_a5_identity_models_are_typed():
    from app.models.invitation import Invitation
    from app.models.magic_login_token import MagicLoginToken
    from app.models.password_reset_token import PasswordResetToken
    from app.models.refresh_token import RefreshToken
    from app.models.tenant import Tenant
    from app.models.user import User

    for model in (User, Tenant, Invitation, RefreshToken, MagicLoginToken, PasswordResetToken):
        hints = model.__annotations__
        columns = {c.key for c in model.__table__.columns}
        assert columns <= set(hints), (model.__name__, columns - set(hints))
        assert all("Mapped[" in str(hints[name]) for name in columns), model.__name__


if __name__ == "__main__" and "--write" in sys.argv:
    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        fh.write(render())
