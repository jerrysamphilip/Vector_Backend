"""contact_field_definitions: is_active, is_system, max_length (C-17 custom-property safety)

Revision ID: 0031_property_safety
Revises: 0030_contact_fulltext
Create Date: 2026-10-28

Custom properties (app/services/contact_field_service.py) are deactivated instead of deleted: an inactive
property is hidden from forms and import mapping, but the values stay on the records and are still exported.
System properties cannot be deactivated. Text properties may carry a character limit.

  contact_field_definitions.is_active  TINYINT(1) NOT NULL DEFAULT 1
  contact_field_definitions.is_system  TINYINT(1) NOT NULL DEFAULT 0
  contact_field_definitions.max_length INT NULL

Two new data types, BOOLEAN and LONG_TEXT, need no column (field_type is VARCHAR(20)); if a CHECK constraint
on field_type exists (one that lists the types and does not know LONG_TEXT yet) it is replaced by one that
lists app/models/contact_field.FIELD_TYPES. Options keep their stored form: plain-string options are read as
{value, label} by app/domain/custom_properties.py, so no row is rewritten.

Idempotent: each column is added only when missing (information_schema), so on a fresh build, where the
baseline's create_all already made them, the revision changes nothing. Existing rows get is_active = 1,
is_system = 0, max_length NULL: exactly how they behave today. No data is changed or deleted.

Downgrade drops the three columns. Properties deactivated by then become visible again (their values were
never removed); a widened field_type CHECK is left as it is (it only allows more).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0031_property_safety"
down_revision: Union[str, Sequence[str], None] = "0030_contact_fulltext"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "contact_field_definitions"
COLUMNS = (
    ("is_active", "TINYINT(1) NOT NULL DEFAULT 1"),
    ("is_system", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("max_length", "INT NULL"),
)
FIELD_TYPES = ("TEXT", "NUMBER", "DATE", "SELECT", "RADIO", "MULTI_CHECKBOX", "PHONE", "URL",
               "BOOLEAN", "LONG_TEXT")


def _has_column(column: str) -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"),
        {"t": TABLE, "c": column}).scalar())


def _field_type_checks() -> list:
    """(name, clause) of CHECK constraints on the table that test field_type."""
    rows = op.get_bind().execute(text(
        "SELECT cc.constraint_name, cc.check_clause FROM information_schema.check_constraints cc "
        "JOIN information_schema.table_constraints tc ON tc.constraint_schema = cc.constraint_schema "
        "AND tc.constraint_name = cc.constraint_name "
        "WHERE tc.table_schema = DATABASE() AND tc.table_name = :t AND tc.constraint_type = 'CHECK'"),
        {"t": TABLE}).fetchall()
    return [(name, clause) for name, clause in rows if "field_type" in (clause or "")]


def _widen_field_type_check() -> None:
    for name, clause in _field_type_checks():
        if "LONG_TEXT" in clause:
            continue
        allowed = ", ".join(f"'{t}'" for t in FIELD_TYPES)
        op.execute(f"ALTER TABLE {TABLE} DROP CHECK `{name}`")
        op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT `{name}` CHECK (field_type IN ({allowed}))")


def upgrade() -> None:
    for column, ddl in COLUMNS:
        if not _has_column(column):
            op.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} {ddl}")
    _widen_field_type_check()


def downgrade() -> None:
    for column, _ in reversed(COLUMNS):
        if _has_column(column):
            op.execute(f"ALTER TABLE {TABLE} DROP COLUMN {column}")
