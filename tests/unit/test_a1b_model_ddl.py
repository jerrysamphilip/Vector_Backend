"""
a1b (chunk A1, lists): prospect_lists / prospect_list_members moved to SQLAlchemy 2 ``Mapped[]`` with the
same DDL. tests/unit/snapshots/a1b_list_ddl.txt was taken before the conversion; the only intended change
is the unique list-name index ``uq_prospect_lists_tenant_name`` that ships with migration 0025 (C-21).
The new identity tables (contact_identifiers, duplicate_pairs, merge_logs, export_logs) are checked
against the DDL migration 0025 writes. No database: DDL is compiled, never executed.
"""
import os

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

SNAPSHOT = os.path.join(os.path.dirname(__file__), "snapshots", "a1b_list_ddl.txt")
NEW_INDEX = "CREATE UNIQUE INDEX uq_prospect_lists_tenant_name ON prospect_lists (tenant_id, list_name)"


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


def render(names=("prospect_lists", "prospect_list_members")) -> list:
    import app.models  # noqa: F401  (registers every table)
    import app.models.contact_identity  # noqa: F401
    import app.models.export_log  # noqa: F401
    from app.models.base import Base

    dialect = mysql.dialect()
    out = []
    for name in names:
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
    return out


def test_a1b_list_models_ddl_unchanged_apart_from_the_unique_name_index():
    parts = render()
    assert NEW_INDEX in parts
    with open(SNAPSHOT, encoding="utf-8") as fh:
        expected = fh.read()
    assert "\n\n".join(p for p in parts if p != NEW_INDEX) + "\n" == expected


def _squash(sql: str) -> str:
    return " ".join(sql.replace("(", " ( ").replace(")", " ) ").replace(",", " , ").split()).lower()


def test_a1b_identity_models_match_migration_0025():
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "..", "..", "alembic", "versions", "0025_contact_identity.py")
    spec = importlib.util.spec_from_file_location("m0025", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from app.models.base import Base

    render(())  # imports the models
    for name, ddl in module.TABLES.items():
        table = Base.metadata.tables[name]
        migration = _squash(ddl)
        for col in table.columns:
            assert f" {col.name} " in migration, (name, col.name)
        for index in table.indexes:
            assert index.name.lower() in migration, (name, index.name)
        for constraint in table.constraints:
            if constraint.name:
                assert constraint.name.lower() in migration, (name, constraint.name)
        assert ("on delete cascade" in migration) == any(
            fk.ondelete == "CASCADE" for fk in table.foreign_keys), name
