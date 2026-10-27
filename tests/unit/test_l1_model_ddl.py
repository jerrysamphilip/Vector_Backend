"""
L1 (ADR 0009): the contact-side models moved to SQLAlchemy 2 ``Mapped[]`` / ``mapped_column`` with the
same DDL. The MySQL ``CREATE TABLE`` / ``CREATE INDEX`` output and the Python-side column options
(defaults, onupdate, nullability) are compared against a snapshot taken before the conversion
(tests/unit/snapshots/l1_contact_ddl.txt). No database: DDL is compiled, never executed.

Regenerate only for an intended schema change that ships with its migration:
    python tests/unit/test_l1_model_ddl.py --write
"""
import os
import sys

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

SNAPSHOT = os.path.join(os.path.dirname(__file__), "snapshots", "l1_contact_ddl.txt")
TABLES = (
    "prospects", "global_unsubscribes", "property_changes", "crm_tasks", "saved_views", "import_jobs",
    "contact_companies", "note_mentions", "contact_activities", "contact_field_definitions",
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
        for col in table.columns:
            out.append(
                f"{name}.{col.key}: type={col.type!r} nullable={col.nullable} pk={col.primary_key} "
                f"index={col.index} unique={col.unique} autoincrement={col.autoincrement!r} "
                f"default={_default(col.default)} onupdate={_default(col.onupdate)} "
                f"server_default={_server_default(col.server_default)} "
                f"server_onupdate={_server_default(col.server_onupdate)} computed={col.computed is not None}")
    return "\n\n".join(out) + "\n"


def test_l1_contact_models_ddl_unchanged():
    with open(SNAPSHOT, encoding="utf-8") as fh:
        expected = fh.read()
    assert render() == expected


if __name__ == "__main__" and "--write" in sys.argv:
    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        fh.write(render())
