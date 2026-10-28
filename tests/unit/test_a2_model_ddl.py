"""
A2 (campaigns): the campaign, sequence, template, draft and subject-variant models moved to SQLAlchemy 2
``Mapped[]`` with the same DDL. tests/unit/snapshots/a2_campaign_ddl.txt was rendered from the classic
``Column`` models before the conversion (CREATE TABLE / CREATE INDEX for MySQL plus every column's
nullability, defaults and on-update values). The only intended difference is the column that migration
0027_campaign_edit adds (``campaigns.wait_business_days``, F-04 edit after launch). No database needed.
"""
from pathlib import Path

from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  (registers every table, applies the table defaults)
from app.models import campaign, campaign_draft, email_sequence, email_template, subject_variant

SNAPSHOT = Path(__file__).with_name("snapshots") / "a2_campaign_ddl.txt"
MODULES = (campaign, campaign_draft, email_sequence, email_template, subject_variant)
# Added by alembic/versions/0027_campaign_edit.py (documented addition, not part of the snapshot)
ADDED_COLUMNS = {("campaigns", "wait_business_days")}


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
        added = {col for table, col in ADDED_COLUMNS if table == t.name}
        lines.append(f"-- {c.__name__}")
        ddl = str(CreateTable(t).compile(dialect=dialect)).strip()
        ddl = "\n".join(line for line in ddl.splitlines() if not any(f"\t{a} " in line for a in added))
        lines.append(ddl)
        for ix in sorted(t.indexes, key=lambda i: i.name):
            lines.append(str(CreateIndex(ix).compile(dialect=dialect)).strip())
        lines.append(f"kwargs {sorted(t.kwargs.items())}")
        for col in t.columns:
            if col.name in added:
                continue
            lines.append(" ".join([
                "col", col.name, col.type.__class__.__name__, "null" if col.nullable else "notnull",
                "pk" if col.primary_key else "", "idx" if col.index else "",
                "default=" + repr(_arg(col.default)), "onupdate=" + repr(_arg(col.onupdate)),
                "server_default=" + repr(_arg(col.server_default)),
                "server_onupdate=" + repr(_arg(col.server_onupdate))]))
    return "\n".join(lines) + "\n"


def test_campaign_models_ddl_unchanged():
    assert render_ddl() == SNAPSHOT.read_text()


def test_added_column_is_the_documented_one():
    col = campaign.Campaign.__table__.c.wait_business_days
    assert not col.nullable
    assert _arg(col.server_default) == "0"


def test_campaign_models_are_typed():
    for c in _models():
        hints = getattr(c, "__annotations__", {})
        assert hints, c.__name__
        assert all("Mapped" in str(h) or getattr(h, "__origin__", None) is Mapped for h in hints.values()), c.__name__
