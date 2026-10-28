"""
A1 (ADR 0009): app/models/account.py moved to SQLAlchemy 2 ``Mapped[]`` / ``mapped_column`` with the same DDL.

The MySQL ``CREATE TABLE`` / ``CREATE INDEX`` output and the Python-side column options of ``accounts`` are
compared against tests/unit/snapshots/a1a_account_ddl.txt, taken from the classic ``Column`` model before the
conversion. The only intended schema change is ``accounts.custom_fields`` (company custom properties, C-16,
migration 0024_company_properties); it is stripped before the comparison and checked on its own.
No database: DDL is compiled, never executed.

Regenerate only for an intended schema change that ships with its migration:
    python tests/unit/test_a1a_model_ddl.py --write
"""
import os
import sys

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

SNAPSHOT = os.path.join(os.path.dirname(__file__), "snapshots", "a1a_account_ddl.txt")
TABLES = ("accounts",)
ADDED = {"accounts": ("custom_fields",)}  # 0024_company_properties


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
    if hasattr(value, "sqltext"):
        return f"Computed({value.sqltext}, persisted={value.persisted})"
    arg = getattr(value, "arg", value)
    return str(getattr(arg, "text", arg))


def render(skip_added: bool = True) -> str:
    import app.models  # noqa: F401  (registers every table)
    from app.models.base import Base

    dialect = mysql.dialect()
    out = []
    for name in TABLES:
        table = Base.metadata.tables[name]
        skip = ADDED.get(name, ()) if skip_added else ()
        ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
        lines = [ln for ln in ddl.splitlines() if not any(ln.strip().startswith(f"{c} ") for c in skip)]
        out.append("\n".join(lines))
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            out.append(str(CreateIndex(index).compile(dialect=dialect)).strip())
        for col in table.columns:
            if col.key in skip:
                continue
            out.append(
                f"{name}.{col.key}: type={col.type!r} nullable={col.nullable} pk={col.primary_key} "
                f"index={col.index} unique={col.unique} autoincrement={col.autoincrement!r} "
                f"default={_default(col.default)} onupdate={_default(col.onupdate)} "
                f"server_default={_server_default(col.server_default)} "
                f"server_onupdate={_server_default(col.server_onupdate)} computed={col.computed is not None}")
    return "\n\n".join(out) + "\n"


def test_a1a_account_model_ddl_unchanged():
    with open(SNAPSHOT, encoding="utf-8") as fh:
        expected = fh.read()
    assert render() == expected


def test_a1a_account_custom_fields_column():
    import app.models  # noqa: F401
    from app.models.base import Base

    col = Base.metadata.tables["accounts"].c.custom_fields
    assert col.nullable is True and col.server_default is None and repr(col.type) == "JSON()"


if __name__ == "__main__" and "--write" in sys.argv:
    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        fh.write(render())
