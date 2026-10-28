"""
A8 (ADR 0009): the sending models (email_messages, email_events) moved to SQLAlchemy 2 ``Mapped[]`` /
``mapped_column`` with the same DDL. The MySQL ``CREATE TABLE`` / ``CREATE INDEX`` output (including the
``email_messages.step_key`` generated column behind ``ux_email_messages_step_key``, the send_key unique key and
the reply-classification columns), the Python-side column options and the relationships are compared against a
snapshot taken before the conversion (tests/unit/snapshots/a8_sending_ddl.txt). No database.

Regenerate only for an intended schema change that ships with its migration:
    python tests/unit/test_a8_model_ddl.py --write
"""
import os
import sys

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

SNAPSHOT = os.path.join(os.path.dirname(__file__), "snapshots", "a8_sending_ddl.txt")
TABLES = ("email_messages", "email_events")
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


def test_a8_sending_models_ddl_unchanged():
    with open(SNAPSHOT, encoding="utf-8") as fh:
        expected = fh.read()
    assert render() == expected


def test_a8_step_key_is_a_virtual_generated_column_with_a_unique_index():
    import app.models  # noqa: F401
    from app.models.base import Base
    from app.models.email_message import STEP_KEY_SQL

    table = Base.metadata.tables["email_messages"]
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
    assert f"step_key VARCHAR(110) GENERATED ALWAYS AS ({STEP_KEY_SQL}) VIRTUAL" in ddl
    index = next(i for i in table.indexes if i.name == "ux_email_messages_step_key")
    assert index.unique and [c.name for c in index.columns] == ["step_key"]


def test_a8_sending_models_are_typed():
    from app.models.email_message import EmailEvent, EmailMessage

    for model in (EmailMessage, EmailEvent):
        hints = model.__annotations__
        columns = {c.key for c in model.__table__.columns}
        assert columns <= set(hints), (model.__name__, columns - set(hints))
        assert all("Mapped[" in str(hints[name]) for name in columns), model.__name__


if __name__ == "__main__" and "--write" in sys.argv:
    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        fh.write(render())
