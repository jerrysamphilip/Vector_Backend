"""0032_server_defaults: every column the revision gives a DEFAULT is declared with the same server_default on
its model (so create_all() on an empty database builds it), and the model keeps its Python default too."""
import importlib.util
from pathlib import Path

import pytest

import app.models  # noqa: F401  (registers every table)
from app.models import Base

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0032_server_defaults.py"


def _migration():
    spec = importlib.util.spec_from_file_location("m0032_unit", MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DEFAULTS = _migration().DEFAULTS


def test_sdef_revision_chain():
    mod = _migration()
    assert mod.revision == "0032_server_defaults"
    assert mod.down_revision == "0031_property_safety"


def test_sdef_list_has_no_duplicates():
    keys = [(t, c) for t, c, _, _ in DEFAULTS]
    assert len(keys) == len(set(keys)) == 23


@pytest.mark.parametrize("table,column,literal,shown", DEFAULTS, ids=[f"{t}.{c}" for t, c, _, _ in DEFAULTS])
def test_sdef_model_declares_the_server_default(table, column, literal, shown):
    col = Base.metadata.tables[table].c[column]
    assert col.server_default is not None, f"{table}.{column}"
    declared = str(getattr(col.server_default.arg, "text", col.server_default.arg))
    assert declared == literal.strip("'")
    assert col.default is not None, f"{table}.{column} lost its Python default"
