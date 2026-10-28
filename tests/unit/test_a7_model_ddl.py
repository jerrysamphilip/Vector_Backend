"""A7: the automation models moved to SQLAlchemy 2 ``Mapped[]`` with the same DDL.

tests/unit/snapshots/a7_automation_ddl.txt was rendered from the classic ``Column`` models before the
conversion: MySQL CREATE TABLE / CREATE INDEX plus every column's Python-side options. No database.

Regenerate only for an intended schema change that ships with its migration:
    python tests/unit/test_a7_model_ddl.py --write
"""
import os
import sys

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  (registers every table, applies the table defaults)
from app.models.automation_rule import AutomationRule
from app.models.automation_rule_run import AutomationRuleRun

SNAPSHOT = os.path.join(os.path.dirname(__file__), "snapshots", "a7_automation_ddl.txt")
MODELS = (AutomationRule, AutomationRuleRun)


def _arg(d):
    if d is None:
        return None
    a = d.arg
    if callable(a):
        return "<callable>"
    return str(getattr(a, "text", a))


def render_ddl() -> str:
    dialect = mysql.dialect()
    lines = []
    for model in MODELS:
        t = model.__table__
        lines.append(f"-- {model.__name__}")
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


def test_automation_models_ddl_unchanged():
    with open(SNAPSHOT, encoding="utf-8") as fh:
        assert render_ddl() == fh.read()


if __name__ == "__main__" and "--write" in sys.argv:
    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        fh.write(render_ddl())
