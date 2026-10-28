"""A4: the typed (Mapped[]) inbox / deliverability models emit exactly the DDL of the classic Column models.

snapshots/a4_inbox_ddl.txt was generated from the classic models before the conversion: CREATE TABLE /
CREATE INDEX for MySQL plus every column's nullability, defaults and on-update values. No database needed.
Snapshot re-rendered for alembic/versions/0032_server_defaults.py (server DEFAULTs only).
Re-rendered again when conversations.last_message_at lost its ORM-side onupdate (agent follow; the column never
had a server-side ON UPDATE, so the CREATE TABLE is unchanged and no migration is needed).
"""
from pathlib import Path

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  (registers every table, applies the table defaults)
from app.models import (conversation, domain_reputation, imap_sync_state, inbox_warmup_event, inbox_warmup_metric,
                        provider_reputation, sending_inbox)

SNAPSHOT = Path(__file__).with_name("snapshots") / "a4_inbox_ddl.txt"
MODULES = (conversation, domain_reputation, imap_sync_state, inbox_warmup_event, inbox_warmup_metric,
           provider_reputation, sending_inbox)


def _arg(d):
    if d is None:
        return None
    a = d.arg
    if callable(a):
        return "callable:" + getattr(a, "__name__", "?")
    return str(getattr(a, "text", a))


def _models():
    out = []
    for mod in MODULES:
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
                "pk" if col.primary_key else "", "idx" if col.index else "", "unique" if col.unique else "",
                "default=" + repr(_arg(col.default)), "onupdate=" + repr(_arg(col.onupdate)),
                "server_default=" + repr(_arg(col.server_default)),
                "server_onupdate=" + repr(_arg(col.server_onupdate))]))
        for rel in sorted(c.__mapper__.relationships, key=lambda r: r.key):
            lines.append(f"rel {rel.key} -> {rel.mapper.class_.__name__} uselist={rel.uselist} "
                         f"back_populates={rel.back_populates} cascade={sorted(rel.cascade)}")
    return "\n".join(lines) + "\n"


def test_inbox_models_ddl_unchanged():
    assert render_ddl() == SNAPSHOT.read_text()
