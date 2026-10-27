"""A3: the typed (Mapped[]) sales models emit exactly the DDL of the classic Column models they replaced.

a3_sales_ddl_snapshot.txt was generated from app/models/sales.py and sales_extra.py before the
conversion: CREATE TABLE / CREATE INDEX for MySQL plus every column's nullability, defaults and
on-update values. No database needed.
"""
from pathlib import Path

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  (registers every table, applies the table defaults)
from app.models import sales, sales_extra

SNAPSHOT = Path(__file__).with_name("a3_sales_ddl_snapshot.txt")


def _arg(d):
    if d is None:
        return None
    a = d.arg
    if callable(a):
        return "callable:" + getattr(a, "__name__", "?")
    return str(getattr(a, "text", a))


def _models():
    out = []
    for mod in (sales, sales_extra):
        for name in dir(mod):
            c = getattr(mod, name)
            if isinstance(c, type) and hasattr(c, "__table__") and c.__module__ == mod.__name__:
                out.append(c)
    return sorted(out, key=lambda c: c.__tablename__)


def render_ddl() -> str:
    dialect = mysql.dialect()
    lines = []
    for c in _models():
        t = c.__table__
        lines.append(f"-- {c.__name__}")
        lines.append(str(CreateTable(t).compile(dialect=dialect)).strip())
        for ix in sorted(t.indexes, key=lambda i: i.name):
            lines.append(str(CreateIndex(ix).compile(dialect=dialect)).strip())
        lines.append(f"kwargs {sorted(t.kwargs.items())}")
        for col in t.columns:
            lines.append(" ".join([
                "col", col.name, col.type.__class__.__name__, "null" if col.nullable else "notnull",
                "pk" if col.primary_key else "", "idx" if col.index else "",
                "default=" + repr(_arg(col.default)), "onupdate=" + repr(_arg(col.onupdate)),
                "server_default=" + repr(_arg(col.server_default)),
                "server_onupdate=" + repr(_arg(col.server_onupdate))]))
    return "\n".join(lines) + "\n"


def test_sales_models_ddl_unchanged():
    assert render_ddl() == SNAPSHOT.read_text()


def test_sales_models_are_typed():
    from sqlalchemy.orm import Mapped
    for c in _models():
        hints = getattr(c, "__annotations__", {})
        assert hints, c.__name__
        assert all(str(h).startswith("Mapped[") or getattr(h, "__origin__", None) is Mapped or "Mapped" in str(h)
                   for h in hints.values()), c.__name__
